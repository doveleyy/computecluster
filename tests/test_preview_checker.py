import io
import json
import os
import subprocess
import sys
import threading
import zipfile
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_app_previews import guest_config, initial

from app.app_contracts import ContractError
from app.app_previews import CHECKS
from app.app_publication import sha256, unpack
from ops import preview_checker
from ops.preview_checker import PORTABLE_CHECKS, RunningTarget, check_running
from ops.preview_guest import bundle, create_guest
from ops.preview_runtime import GuestRuntime


@contextmanager
def probes(base_path):
    calls = []
    faults = {}

    class Direct(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(("direct", self.path, dict(self.headers)))
            if self.path in {"/health", "/ready"}:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"informational non-JSON body")
            elif self.path == "/version.json":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"service":"notes","version":"1.0.0"}')
            elif self.path == "/":
                trusted = (
                    faults.get("trusts-forwarded")
                    and self.headers.get("X-Forwarded-Proto") == "https"
                    and self.headers.get("Tailscale-User-Login")
                    == "preview@example.test"
                )
                self.send_response(200 if trusted else 401)
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass

    class Routed(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(("routed", self.path, dict(self.headers)))
            if self.headers.get("X-Preview-Bridge") != "synthetic-bridge":
                self.send_response(401)
                self.end_headers()
            elif faults.get("routed-missing"):
                self.send_response(404)
                self.end_headers()
            elif self.path == base_path:
                self.send_response(307)
                self.send_header("Location", base_path + "/")
                self.end_headers()
            elif self.path == base_path + "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                reference = "/outside" if faults.get("escaped-asset") else "style.css"
                self.wfile.write(f'<link href="{reference}">'.encode())
            elif self.path == base_path + "/style.css":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"body {}")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass

    servers = [
        ThreadingHTTPServer(("127.0.0.1", 0), handler) for handler in (Direct, Routed)
    ]
    threads = [threading.Thread(target=server.serve_forever) for server in servers]
    for thread in threads:
        thread.start()
    try:
        yield (
            RunningTarget(
                f"http://127.0.0.1:{servers[0].server_port}",
                f"http://127.0.0.1:{servers[1].server_port}",
                base_path,
                {
                    "X-Preview-Bridge": "synthetic-bridge",
                    "Tailscale-User-Login": "preview@example.test",
                },
            ),
            calls,
            faults,
        )
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()


def publication():
    request, archive = initial()
    files = unpack(archive)
    original = b" \n" + files["manifest.json"] + b"\n"
    files["manifest.json"] = original
    proof = json.loads(files["publication.json"])
    proof["manifest_sha256"] = sha256(original)
    proof["assets"]["manifest.json"] = sha256(original)
    files["publication.json"] = json.dumps(proof).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    archive_bytes = output.getvalue()
    request = request.model_copy(
        update={
            "manifest_sha256": sha256(original),
            "bundle_sha256": sha256(archive_bytes),
        }
    )
    return request, bundle(request, archive_bytes)


def successful_report(digest):
    return {
        "checker_version": "0.1.1",
        "contract_version": 1,
        "mode": "final",
        "manifest_sha256": digest,
        "findings": [
            {"check": name, "passed": True, "detail": "Bounded finding"}
            for name in sorted(PORTABLE_CHECKS)
        ],
    }


def test_real_pinned_zipapp_checks_original_bytes_and_internal_root(
    tmp_path, monkeypatch
):
    request, release = publication()
    original = release.files["manifest.json"]
    assert original != release.manifest.model_dump_json().encode()
    commands = []
    popen = subprocess.Popen

    def observe(command, **kwargs):
        commands.append(command)
        assert "synthetic-bridge" not in repr(command)
        assert "preview@example.test" not in repr(command)
        assert Path(command[command.index("--manifest") + 1]).read_bytes() == original
        return popen(command, **kwargs)

    monkeypatch.setattr(preview_checker.subprocess, "Popen", observe)
    with probes(f"/previews/{request.attempt_id}/notes") as (target, calls, _):
        report = check_running(original, request.manifest_sha256, target, tmp_path)
    assert report.manifest_sha256 == sha256(original)
    assert report.checks() == dict.fromkeys(PORTABLE_CHECKS, True)
    assert len(commands) == 1
    assert "--template" not in commands[0]
    assert set(path for role, path, _ in calls if role == "direct") == {
        "/",
        "/health",
        "/ready",
        "/version.json",
    }
    assert all(
        "X-Preview-Bridge" not in headers
        for role, _, headers in calls
        if role == "direct"
    )
    assert all(
        headers.get("X-Preview-Bridge") == "synthetic-bridge"
        for role, _, headers in calls
        if role == "routed"
    )
    assert list(tmp_path.iterdir()) == []


def test_run_checker_ignores_hostile_pythonpath(tmp_path, monkeypatch):
    request, release = publication()
    original = release.files["manifest.json"]
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(original)
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    (hostile / "hashlib.py").write_text(
        "class _Forged:\n"
        "    def hexdigest(self):\n"
        "        return 'ff' * 32\n"
        "def sha256(data=b''):\n"
        "    return _Forged()\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(hostile))
    artifact = preview_checker.checker_artifact()

    with probes(f"/previews/{request.attempt_id}/notes") as (target, _, _):
        target_bytes = target.to_bytes()
        status, payload = preview_checker.run_checker(artifact, manifest, target_bytes)
        assert status == 0
        assert json.loads(payload)["manifest_sha256"] == sha256(original)

        # Same hostile directory, same subprocess, no "-I": the shadowed
        # hashlib forges the checker's self-reported digest. This is what
        # run_checker would be exposed to without isolated mode.
        unisolated = subprocess.run(
            [
                sys.executable,
                str(artifact),
                "--manifest",
                str(manifest),
                "--target-stdin",
                "--json",
            ],
            input=target_bytes,
            capture_output=True,
            env={**os.environ, "PYTHONPATH": str(hostile)},
            check=True,
        )
        assert json.loads(unisolated.stdout)["manifest_sha256"] == "ff" * 32


def test_real_zipapp_refuses_asset_outside_preview_prefix(tmp_path):
    request, release = publication()
    with probes(f"/previews/{request.attempt_id}/notes") as (target, _, faults):
        faults["escaped-asset"] = True
        with pytest.raises(ContractError, match="did not prove"):
            check_running(
                release.files["manifest.json"],
                request.manifest_sha256,
                target,
                tmp_path,
            )


@pytest.mark.parametrize("fault", ["artifact", "pin", "artifact-link", "pin-link"])
def test_changed_or_linked_pin_refuses_before_execution(tmp_path, monkeypatch, fault):
    source = preview_checker.ASSET_DIRECTORY
    assets = tmp_path / "assets"
    assets.mkdir()
    for name in ("hp-app-check.pyz", "hp-app-check.sha256"):
        (assets / name).write_bytes((source / name).read_bytes())
    selected = assets / (
        "hp-app-check.pyz" if fault.startswith("artifact") else "hp-app-check.sha256"
    )
    if fault.endswith("link"):
        selected.unlink()
        selected.symlink_to(source / selected.name)
    else:
        selected.write_bytes(selected.read_bytes() + b"changed")
    monkeypatch.setattr(preview_checker, "ASSET_DIRECTORY", assets)

    def forbidden(*args):
        raise AssertionError("Changed checker bytes must never execute")

    monkeypatch.setattr(preview_checker, "run_checker", forbidden)
    request, release = publication()
    with pytest.raises(ContractError, match="does not match its pin"):
        check_running(
            release.files["manifest.json"],
            request.manifest_sha256,
            RunningTarget("http://127.0.0.1:1", "http://127.0.0.1:2", "/notes", {}),
            tmp_path,
        )


@pytest.mark.parametrize(
    "fault",
    [
        "version",
        "contract-version",
        "contract-bool",
        "mode",
        "sha",
        "duplicate",
        "missing",
        "extra",
        "passed-number",
        "passed-string",
        "failed",
        "detail",
        "unknown-field",
        "duplicate-json-key",
        "exit-code",
    ],
)
def test_report_mutations_cannot_authorize_final_preview(tmp_path, monkeypatch, fault):
    request, release = publication()
    report = successful_report(request.manifest_sha256)
    status = 0
    if fault in {"version", "contract-version", "contract-bool", "mode", "sha"}:
        field, value = {
            "version": ("checker_version", "0.2.0"),
            "contract-version": ("contract_version", 2),
            "contract-bool": ("contract_version", True),
            "mode": ("mode", "template"),
            "sha": ("manifest_sha256", "0" * 64),
        }[fault]
        report[field] = value
    elif fault == "duplicate":
        report["findings"][0] = report["findings"][1]
    elif fault == "missing":
        report["findings"].pop()
    elif fault == "extra":
        report["findings"].append({"check": "unexpected", "passed": True, "detail": ""})
    elif fault in {"passed-number", "passed-string", "failed"}:
        report["findings"][0]["passed"] = {
            "passed-number": 1,
            "passed-string": "true",
            "failed": False,
        }[fault]
    elif fault == "detail":
        report["findings"][0]["detail"] = "x" * 513
    elif fault == "unknown-field":
        report["secret"] = "never include this input in an error"
    elif fault == "exit-code":
        status = 1
    payload = json.dumps(report).encode()
    if fault == "duplicate-json-key":
        payload = b'{"mode":"final",' + payload[1:]
    monkeypatch.setattr(preview_checker, "run_checker", lambda *args: (status, payload))
    with pytest.raises(ContractError, match="did not prove") as failed:
        check_running(
            release.files["manifest.json"],
            request.manifest_sha256,
            RunningTarget("http://127.0.0.1:1", "http://127.0.0.1:2", "/notes", {}),
            tmp_path,
        )
    assert "never include" not in str(failed.value)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("fault", ["stdout", "stderr", "timeout"])
def test_checker_process_limits_bound_output_and_hide_errors(
    tmp_path, monkeypatch, fault
):
    script = tmp_path / "process.py"
    script.write_text(
        "import sys,time\n"
        + {
            "stdout": "sys.stdout.write('x'*65537)",
            "stderr": "sys.stderr.write('sensitive-error'*1000)",
            "timeout": "time.sleep(10)",
        }[fault]
    )
    monkeypatch.setattr(preview_checker, "CHECKER_TIMEOUT", 0.2)
    with pytest.raises(ContractError, match="process limits") as failed:
        preview_checker.run_checker(script, tmp_path / "manifest.json", b"{}")
    assert "sensitive-error" not in str(failed.value)


@pytest.mark.parametrize(
    "fault",
    [None, "peer", "stopped", "image", "missing-container", "trusts-forwarded"],
)
def test_portable_pass_requires_actual_container_and_distinct_peer_evidence(
    tmp_path, monkeypatch, fault
):
    runtime = GuestRuntime(guest_config(tmp_path))
    request, release = publication()
    directory = runtime.root / str(request.attempt_id)
    (directory / "core").mkdir(parents=True)
    (directory / "core/bridge-token").write_text("synthetic-bridge")
    image_id = "sha256:" + "a" * 64
    commands = []

    def docker(*args, **kwargs):
        commands.append(args)
        if args[:2] == ("container", "inspect"):
            containers = [
                {"State": {"Running": True}, "Image": image_id} for _ in range(3)
            ]
            if fault == "stopped":
                containers[2]["State"]["Running"] = False
            if fault == "image":
                containers[0]["Image"] = "sha256:" + "b" * 64
            if fault == "missing-container":
                containers.pop()
            return json.dumps(containers)
        return "200" if fault == "peer" else "401"

    checked = []
    run_checker = preview_checker.run_checker

    def observe(artifact, manifest, target):
        checked.append(manifest.read_bytes())
        return run_checker(artifact, manifest, target)

    monkeypatch.setattr(runtime, "docker", docker)
    monkeypatch.setattr(preview_checker, "run_checker", observe)
    expected = {
        "peer": "impersonate",
        "trusts-forwarded": "forged proxy context",
    }.get(fault, "selected image")
    with probes(f"/previews/{request.attempt_id}/notes") as (target, _, faults):
        faults[fault] = True
        monkeypatch.setattr(
            runtime,
            "endpoint",
            lambda attempt, role, port: target.direct_origin
            if role == "app"
            else target.routed_origin,
        )
        if fault is None:
            ready = runtime.verify(request, release, image_id)
            assert ready.state == "ready"
            assert ready.checks == dict.fromkeys(CHECKS, True)
            assert (
                ready.preview_url
                == runtime.config.public_origin + target.base_path + "/"
            )
            assert checked == [release.files["manifest.json"]]
        else:
            if fault == "trusts-forwarded":
                portable = check_running(
                    release.files["manifest.json"],
                    request.manifest_sha256,
                    target,
                    tmp_path,
                )
                assert portable.checks() == dict.fromkeys(PORTABLE_CHECKS, True)
                checked.clear()
            with pytest.raises(ContractError, match=expected):
                runtime.verify(request, release, image_id)
        assert request.state == "installing"
    peer = [command for command in commands if command[0] == "run"]
    if fault == "trusts-forwarded":
        assert checked == []
    if fault in {None, "peer"}:
        assert len(peer) == 1
        assert peer[0][peer[0].index("--network") + 1] == runtime.name(
            request.attempt_id, "net"
        )
        script = peer[0][-1]
        assert "Tailscale-User-Login" in script
        assert ":8000/previews/" in script
        assert "X-Preview-Bridge" not in script
    else:
        assert peer == []


def test_failed_checker_names_failing_checks_in_guest_result(tmp_path, monkeypatch):
    app = create_guest(guest_config(tmp_path))
    runtime = app.state.runtime
    request, archive = initial()
    core = runtime.root / str(request.attempt_id) / "core"
    core.mkdir(parents=True)
    (core / "bridge-token").write_text("synthetic-bridge")

    def docker(*args, **kwargs):
        raise AssertionError("Docker must not run after the checker refuses")

    monkeypatch.setattr(runtime, "owned_attempts", set)
    monkeypatch.setattr(runtime, "cleanup", lambda attempt: None)
    monkeypatch.setattr(runtime, "docker", docker)
    monkeypatch.setattr(
        runtime,
        "install",
        lambda initial, release: runtime.verify(initial, release, "sha256:" + "a" * 64),
    )
    with probes(f"/previews/{request.attempt_id}/notes") as (target, _, faults):
        faults["routed-missing"] = True
        monkeypatch.setattr(
            runtime,
            "endpoint",
            lambda attempt, role, port: target.direct_origin
            if role == "app"
            else target.routed_origin,
        )
        with TestClient(app, base_url="https://preview.example") as client:
            response = client.put(
                f"/control/attempts/{request.attempt_id}",
                content=archive,
                headers={
                    "Authorization": "Bearer " + "t" * 32,
                    "X-App-ID": request.app_id,
                    "X-Release-SHA256": request.manifest_sha256,
                    "X-Bundle-SHA256": request.bundle_sha256,
                },
            )
            assert response.status_code == 200
    result = app.state.journal.get(request.attempt_id)
    assert result.state == "failed"
    assert result.error is not None
    assert "routed-access (Routed request returned HTTP 404)" in result.error
    assert "base-path" in result.error
    assert "health" not in result.error
    assert "synthetic-bridge" not in result.error


def test_checker_failure_summary_redacts_header_values_and_is_bounded(
    tmp_path, monkeypatch
):
    request, release = publication()
    report = successful_report(request.manifest_sha256)
    for finding in report["findings"]:
        finding["passed"] = False
        finding["detail"] = "Sent X-Preview-Bridge synthetic-bridge " + "x" * 400
    monkeypatch.setattr(
        preview_checker, "run_checker", lambda *args: (1, json.dumps(report).encode())
    )
    with pytest.raises(ContractError, match="routed-access") as failed:
        check_running(
            release.files["manifest.json"],
            request.manifest_sha256,
            RunningTarget(
                "http://127.0.0.1:1",
                "http://127.0.0.1:2",
                "/notes",
                {"X-Preview-Bridge": "synthetic-bridge"},
            ),
            tmp_path,
        )
    message = str(failed.value)
    assert "synthetic-bridge" not in message
    assert "[redacted]" in message
    assert len(message) <= 1024

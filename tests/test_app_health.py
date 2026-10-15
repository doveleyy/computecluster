import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.app_health import collect_application_health, probe_application
from app.app_registry import ProbeTarget


@contextmanager
def application_server(
    ready_body: bytes = b"ready text",
    ready_status: int = 200,
    version_body: bytes | None = None,
    delay: float = 0,
) -> Iterator[tuple[ProbeTarget, list[str], list[int]]]:
    calls: list[str] = []
    active = [0, 0]
    lock = threading.Lock()
    version_body = (
        version_body
        if version_body is not None
        else json.dumps({"service": "notes", "version": "1.2.3"}).encode()
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            with lock:
                calls.append(self.path)
                active[0] += 1
                active[1] = max(active)
            try:
                time.sleep(delay)
                ready = self.path == "/ready"
                body = ready_body if ready else version_body
                self.send_response(ready_status if ready else 200)
                if ready_status == 302 and ready:
                    self.send_header("Location", "/redirect-must-not-be-read")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with lock:
                    active[0] -= 1

        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    target = ProbeTarget(
        "notes",
        "Notes",
        "/notes",
        server.server_port,
        "notes",
        "/ready",
        "/version",
        "v1-status",
    )
    try:
        yield target, calls, active
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_real_v1_readiness_uses_status_only_and_checks_version():
    with application_server(ready_body=b"not JSON") as (target, calls, _):
        result = probe_application(target)
        assert result["state"] == "ONLINE"
        assert result["version"] == "1.2.3"
        assert calls == ["/ready", "/version"]
        assert set(result) == {
            "key",
            "name",
            "path",
            "state",
            "detail",
            "version",
            "latency_ms",
        }


def test_real_legacy_readiness_still_requires_json_and_service_alias():
    version = b'{"service":"habit-tracker","version":"0.8.1"}'
    with application_server(b'{"status":"ready"}', version_body=version) as (
        target,
        _,
        _,
    ):
        target = replace(target, service="habit-tracker", readiness="legacy-json")
        assert probe_application(target)["state"] == "ONLINE"
    with application_server(b'{"status":"starting"}') as (target, _, _):
        target = replace(target, readiness="legacy-json")
        assert probe_application(target)["state"] == "DEGRADED"
    with application_server(b"ready") as (target, calls, _):
        target = replace(target, readiness="legacy-json")
        assert probe_application(target)["state"] == "OFFLINE"
        assert calls == ["/ready"]


@pytest.mark.parametrize("status", [204, 302, 503])
def test_real_readiness_requires_200_and_never_follows_redirects(status):
    with application_server(ready_status=status) as (target, calls, _):
        result = probe_application(target)
        assert result["state"] == "DEGRADED"
        assert calls == ["/ready", "/version"]


@pytest.mark.parametrize(
    "version",
    [
        b'{"service":"wrong","version":"1"}',
        b'{"service":"notes","version":""}',
        b'{"service":"notes","version":1}',
        b"[]",
        b"not JSON",
        b"\xff",
        b"x" * 1025,
    ],
)
def test_real_wrong_or_invalid_versions_are_degraded(version):
    with application_server(version_body=version) as (target, calls, _):
        result = probe_application(target)
        assert result["state"] == "DEGRADED"
        assert result["version"] is None
        assert calls == ["/ready", "/version"]


def test_real_response_body_is_bounded_and_timeout_is_one_second():
    with application_server(ready_body=b"x" * 1025) as (target, calls, _):
        assert probe_application(target)["state"] == "OFFLINE"
        assert calls == ["/ready"]
    with application_server(delay=1.2) as (target, calls, _):
        started = time.monotonic()
        result = probe_application(target)
        assert result["state"] == "OFFLINE"
        assert result["latency_ms"] is None
        assert time.monotonic() - started < 1.15
        assert calls == ["/ready"]


def test_collect_real_http_limits_concurrency_and_preserves_order():
    with application_server(delay=0.03) as (target, calls, active):
        targets = tuple(replace(target, key=f"notes-{index}") for index in range(9))
        results = collect_application_health(targets)
        assert [result["key"] for result in results] == [
            target.key for target in targets
        ]
        assert all(result["state"] == "ONLINE" for result in results)
        assert len(calls) == 18
        assert 1 < active[1] <= 4


def test_empty_monitoring_does_no_work(monkeypatch):
    def forbidden_pool(*args, **kwargs):
        raise AssertionError("Empty monitoring cannot create workers")

    monkeypatch.setattr("app.app_health.ThreadPoolExecutor", forbidden_pool)
    assert collect_application_health(()) == []

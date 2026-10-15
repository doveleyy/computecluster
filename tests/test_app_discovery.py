import asyncio
import io
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import httpx
import pytest
import yaml
from test_app_registry import document, manifest, registration

from app import migrations
from app.app_contracts import (
    ContractError,
    PublishedSource,
    parse_document,
)
from app.app_discovery import MAX_VERIFY_ATTEMPTS, RETRY_SECONDS, ReleaseDiscovery
from app.app_github import GitHubPublications
from app.app_publication import sha256, unpack, verify_package
from app.app_registry import AppRegistry
from app.database import Database


@pytest.mark.parametrize(
    "case",
    json.loads(
        (
            Path(__file__).resolve().parents[1] / "docs/apps/release-conformance.json"
        ).read_bytes()
    )["cases"],
    ids=lambda case: case["id"],
)
def test_publication_validator_obeys_public_conformance_corpus(tmp_path, case):
    root = Path(__file__).resolve().parents[1]
    example = root / "examples/apps/publication"
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps(case["manifest"]))
    checked = subprocess.run(
        [sys.executable, "-I", str(example / "manifest.py"), str(source)],
        capture_output=True,
        check=False,
    )
    assert (checked.returncode == 0) is case["valid"], case["id"]


def test_publication_carries_public_schema_and_identical_checker():
    root = Path(__file__).resolve().parents[1]
    example = root / "examples/apps/publication"
    assert not (example / "contract.py").exists()
    assert (example / "manifest.schema.json").read_bytes() == (
        root / "docs/apps/release-schema.json"
    ).read_bytes()
    for name in ("hp-app-check.pyz", "hp-app-check.sha256"):
        assert (example / "vendor" / name).read_bytes() == (
            root / "ops/assets" / name
        ).read_bytes()


def package():
    value = manifest()
    value["workloads"] = []
    payload = document(value)
    proof = {
        "publication_version": 1,
        **{
            key: registration()["source"][key]
            for key in ("repository", "branch", "workflow")
        },
        "source_commit": value["source_commit"],
        "run_id": 42,
        "run_attempt": 1,
        "manifest_sha256": sha256(payload),
        "assets": {"manifest.json": sha256(payload)},
    }
    files = {"manifest.json": payload, "publication.json": document(proof)}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return files, output.getvalue(), proof


class GitHubFixture:
    def __init__(self):
        self.files, self.archive, self.proof = package()
        self.pending = False
        self.cached = False
        self.outage = False
        self.forge = {}
        self.calls = []
        self.strict_artifact_accept = False

    def respond(self, request):
        self.calls.append(request.url.path)
        if self.outage:
            return httpx.Response(503)
        path = request.url.path
        base = "/repos/example/notes"
        release = {
            "id": 7,
            "draft": False,
            "prerelease": False,
            "assets": [
                {"id": index, "name": name, "size": len(data), "state": "uploaded"}
                for index, (name, data) in enumerate(self.files.items(), 1)
            ],
        }
        if path == base + "/releases":
            return httpx.Response(
                304 if self.cached else 200, headers={"etag": '"one"'}, json=[release]
            )
        if path == base + "/releases/7":
            return httpx.Response(200, json=release)
        if path.startswith(base + "/releases/assets/"):
            return httpx.Response(
                200, content=list(self.files.values())[int(path.rsplit("/", 1)[1]) - 1]
            )
        if path.endswith("/attempts/1"):
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "run_attempt": 1,
                    "repository": {"full_name": "example/notes"},
                    "head_repository": {"full_name": "example/notes"},
                    "event": "push",
                    "head_branch": "main",
                    "path": ".github/workflows/publish.yml",
                    "head_sha": "a" * 40,
                    "status": "in_progress" if self.pending else "completed",
                    "conclusion": "success",
                    **self.forge,
                },
            )
        if path.endswith("/runs/42/artifacts"):
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "artifacts": [
                        {
                            "id": 9,
                            "name": "home-platform-publication-1",
                            "expired": False,
                            "digest": "sha256:" + sha256(self.archive),
                            "size_in_bytes": len(self.archive),
                            "workflow_run": {"id": 42, "head_sha": "a" * 40},
                        }
                    ],
                },
            )
        if path.endswith("/artifacts/9/zip"):
            if (
                self.strict_artifact_accept
                and request.headers.get("Accept") != "application/vnd.github+json"
            ):
                return httpx.Response(415)
            return httpx.Response(200, content=self.archive)
        raise AssertionError(path)


def test_actions_archive_request_uses_api_media_type(tmp_path):
    fixture = GitHubFixture()
    fixture.strict_artifact_accept = True
    source = parse_document(PublishedSource, document(registration()["source"]))

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fixture.respond)
        ) as client:
            publication = await GitHubPublications(client).verify(source, 7)
            assert publication.archive == fixture.archive
            assert (
                publication.publication.manifest_sha256
                == fixture.proof["manifest_sha256"]
            )

    asyncio.run(run())


def test_discovery_retries_304_preserves_immutable_bytes_and_installed_state(tmp_path):
    database = Database(tmp_path / "core.db")
    database.initialize()
    registry = AppRegistry(database)
    registry.register(document(registration()))
    fixture = GitHubFixture()

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fixture.respond)
        ) as client:
            discovery = ReleaseDiscovery(database, GitHubPublications(client))
            fixture.pending = True
            first = await discovery.discover("notes")
            assert first.waiting == (7,) and first.latest_sha256 is None
            fixture.pending = False
            fixture.cached = True
            second = await discovery.discover("notes")
            assert second.latest_sha256 == fixture.proof["manifest_sha256"]
            assert second.waiting == ()
            fixture.outage = True
            third = await discovery.discover("notes")
            assert third.latest_sha256 == second.latest_sha256
            assert third.last_success == second.last_success
            assert third.error
        with database.connect() as connection:
            assert (
                connection.execute(
                    "SELECT archive FROM app_eligible_releases"
                ).fetchone()[0]
                == fixture.archive
            )
            assert (
                connection.execute("SELECT COUNT(*) FROM app_releases").fetchone()[0]
                == 0
            )
        assert registry.summaries()[0].installation == "legacy-scripts"

    asyncio.run(run())


def discover_with(tmp_path, forge):
    database = Database(tmp_path / "core.db")
    database.initialize()
    AppRegistry(database).register(document(registration()))
    fixture = GitHubFixture()
    fixture.forge = forge

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fixture.respond)
        ) as client:
            await ReleaseDiscovery(database, GitHubPublications(client)).discover(
                "notes"
            )

    asyncio.run(run())
    with database.connect() as connection:
        return tuple(
            connection.execute(
                "SELECT commit_title, committed_at FROM app_eligible_releases"
            ).fetchone()
        )


def test_verification_records_the_head_commit_title_and_time_as_plain_text(
    tmp_path,
):
    facts = discover_with(
        tmp_path,
        {
            "display_title": "Display title is only a fallback",
            "head_commit": {
                "message": "  Add <img src=x onerror=alert(1)>\u202e export\n\nBody",
                "timestamp": "2026-10-14T09:12:30+08:00",
            },
        },
    )
    assert facts == (
        "Add <img src=x onerror=alert(1)> export",
        "2026-10-14T01:12:30Z",
    )


@pytest.mark.parametrize(
    ("forge", "facts"),
    [
        ({}, (None, None)),
        ({"head_commit": None}, (None, None)),
        ({"display_title": "Fallback title"}, ("Fallback title", None)),
        (
            {"head_commit": {"message": 7, "timestamp": "yesterday"}},
            (None, None),
        ),
        (
            {"head_commit": {"message": "\n \n", "timestamp": "2026-10-14T09:12:30"}},
            (None, None),
        ),
        (
            {"head_commit": {"message": "x" * 500}},
            ("x" * 199 + "…", None),
        ),
    ],
)
def test_verification_tolerates_missing_or_malformed_commit_facts(
    tmp_path, forge, facts
):
    assert discover_with(tmp_path, forge) == facts


def test_migration_23_keeps_schema_22_releases_and_their_immutability(
    tmp_path, monkeypatch
):
    database = Database(tmp_path / "old.db")
    with monkeypatch.context() as before:
        before.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:22])
        database.initialize()
    AppRegistry(database).register(document(registration()))
    _, archive, proof = package()
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO app_eligible_releases (app_id, manifest_sha256, "
            "release_id, artifact_id, artifact_sha256, archive) "
            "VALUES ('notes', ?, 7, 9, ?, ?)",
            (proof["manifest_sha256"], sha256(archive), archive),
        )
    database.initialize()
    with database.connect() as connection:
        row = connection.execute(
            "SELECT manifest_sha256, archive, commit_title, committed_at "
            "FROM app_eligible_releases"
        ).fetchone()
        assert tuple(row) == (proof["manifest_sha256"], archive, None, None)
        assert (
            connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[
                0
            ]
            == 24
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE app_eligible_releases SET commit_title = 'forged'"
            )


@pytest.mark.parametrize(
    "forge",
    [
        {"event": "pull_request"},
        {"head_branch": "other"},
        {"path": "evil.yml"},
        {"head_sha": "b" * 40},
        {"run_attempt": 2},
        {"conclusion": "failure"},
        {"head_repository": {"full_name": "attacker/notes"}},
    ],
)
def test_forged_authority_is_rejected(forge):
    fixture = GitHubFixture()
    fixture.forge = forge

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fixture.respond)
        ) as client:
            with pytest.raises(ContractError):
                await GitHubPublications(client).verify(
                    parse_document(PublishedSource, document(registration()["source"])),
                    7,
                )

    asyncio.run(run())


def test_changed_release_asset_cannot_replace_workflow_bytes():
    fixture = GitHubFixture()
    fixture.files["manifest.json"] += b" "

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fixture.respond)
        ) as client:
            with pytest.raises(ContractError, match="differs"):
                await GitHubPublications(client).verify(
                    parse_document(PublishedSource, document(registration()["source"])),
                    7,
                )

    asyncio.run(run())


def test_package_rejects_digest_and_zip_path_escape():
    _, archive, _ = package()
    source = parse_document(PublishedSource, document(registration()["source"]))
    with pytest.raises(ContractError, match="digest"):
        verify_package(archive, source, 9, "0" * 64)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as value:
        value.writestr("../outside", "bad")
    with pytest.raises(ContractError, match="unsafe"):
        unpack(output.getvalue())


def test_malformed_deflate_does_not_stop_later_discovery(tmp_path):
    database = Database(tmp_path / "core.db")
    database.initialize()
    AppRegistry(database).register(document(registration()))
    fixture = GitHubFixture()
    valid_archive = fixture.archive
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", fixture.files["manifest.json"])
    malformed = bytearray(output.getvalue())
    malformed[30 + len("manifest.json")] = 0xFF
    fixture.archive = bytes(malformed)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fixture.respond)
        ) as client:
            clock = [0.0]
            discovery = ReleaseDiscovery(
                database, GitHubPublications(client), lambda: clock[0]
            )
            failed = await discovery.discover("notes")
            assert failed.error == "A release could not be verified"
            assert failed.waiting == (7,)
            assert failed.latest_sha256 is None
            fixture.archive = valid_archive
            fixture.cached = True
            clock[0] = RETRY_SECONDS
            recovered = await discovery.discover("notes")
            assert recovered.error is None
            assert recovered.waiting == ()
            assert recovered.latest_sha256 == fixture.proof["manifest_sha256"]

    asyncio.run(run())


def test_download_redirect_drops_credentials():
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                302, headers={"location": "https://objects.githubusercontent.com/file"}
            )
        return httpx.Response(200, content=b"asset")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await GitHubPublications(client, "fixture-token")._get(
                "/repos/example/notes/releases/assets/1", binary=True
            )
            assert result[1] == b"asset"

    asyncio.run(run())
    assert calls[0].headers["authorization"] == "Bearer fixture-token"
    assert "authorization" not in calls[1].headers


def test_failing_release_retries_with_bounded_backoff(tmp_path):
    database = Database(tmp_path / "core.db")
    database.initialize()
    AppRegistry(database).register(document(registration()))
    fixture = GitHubFixture()
    fixture.forge = {"conclusion": "failure"}

    def verifications():
        return fixture.calls.count("/repos/example/notes/releases/7")

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fixture.respond)
        ) as client:
            clock = [0.0]
            discovery = ReleaseDiscovery(
                database, GitHubPublications(client), lambda: clock[0]
            )
            for _ in range(6):
                result = await discovery.discover("notes")
            assert verifications() == 1
            assert result.waiting == (7,) and result.error
            clock[0] = RETRY_SECONDS - 1
            await discovery.discover("notes")
            assert verifications() == 1
            for day in range(1, 11):
                clock[0] = day * 86400.0
                result = await discovery.discover("notes")
            assert verifications() == MAX_VERIFY_ATTEMPTS
            assert result.waiting == () and result.abandoned == (7,)

    asyncio.run(run())


def test_provider_failure_during_verification_is_not_a_release_defect(tmp_path):
    database = Database(tmp_path / "core.db")
    database.initialize()
    AppRegistry(database).register(document(registration()))
    fixture = GitHubFixture()
    original = fixture.respond

    def respond(request):
        if request.url.path.endswith("/attempts/1"):
            return httpx.Response(502)
        return original(request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            discovery = ReleaseDiscovery(database, GitHubPublications(client))
            for _ in range(2):
                result = await discovery.discover("notes")
                assert result.error == "Release provider is unavailable"
                assert result.last_success is None
            assert fixture.calls.count("/repos/example/notes/releases/7") == 2
            assert result.abandoned == ()

    asyncio.run(run())


def test_full_store_is_not_a_release_defect(tmp_path, monkeypatch):
    from app import app_discovery

    database = Database(tmp_path / "core.db")
    database.initialize()
    AppRegistry(database).register(document(registration()))
    fixture = GitHubFixture()
    monkeypatch.setattr(app_discovery, "MAX_STORE_BYTES", 0)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fixture.respond)
        ) as client:
            clock = [0.0]
            discovery = ReleaseDiscovery(
                database, GitHubPublications(client), lambda: clock[0]
            )
            for day in range(11):
                clock[0] = day * 86400.0
                result = await discovery.discover("notes")
                assert result.error == "Retained release store is full"
                assert result.last_success is None
            assert result.abandoned == () and result.latest_sha256 is None
            monkeypatch.setattr(app_discovery, "MAX_STORE_BYTES", 10**9)
            result = await discovery.discover("notes")
            assert result.latest_sha256 == fixture.proof["manifest_sha256"]
            assert result.error is None and result.abandoned == ()

    asyncio.run(run())


def test_invalid_registration_does_not_stop_discovery_of_other_apps(
    tmp_path, monkeypatch, caplog
):
    from app import app_discovery

    database = Database(tmp_path / "core.db")
    database.initialize()
    registry = AppRegistry(database)
    registry.register(document(registration()))
    registry.register(document(registration("broken", "/apps/broken")))
    decode = app_discovery.decode_registration

    def decode_or_fail(app_id, *args):
        if app_id == "broken":
            raise ContractError("Invalid stored registration")
        return decode(app_id, *args)

    monkeypatch.setattr(app_discovery, "decode_registration", decode_or_fail)
    fixture = GitHubFixture()

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fixture.respond)
        ) as client:
            return await ReleaseDiscovery(database, GitHubPublications(client)).all()

    results = asyncio.run(run())
    assert [item.app_id for item in results] == ["notes"]
    assert results[0].latest_sha256 == fixture.proof["manifest_sha256"]
    assert "broken" in caplog.text


def test_publication_template_pins_actions_and_runs_proven_steps():
    example = Path(__file__).resolve().parents[1] / "examples/apps/publication"
    jobs = yaml.safe_load((example / "publish.yml").read_text())["jobs"]
    steps = [step for job in jobs.values() for step in job["steps"]]
    uses = [step["uses"] for step in steps if "uses" in step]
    assert all(re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", item) for item in uses)
    assert set(uses) == {
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
        "docker/setup-qemu-action@99012661954931238ded8c8b007157a8430204e1",
        "docker/setup-buildx-action@f87e5991a6d7451dcb8d9637bfbc97413f497069",
        "docker/login-action@dbcb813823bdd20940b903addbd779551569679f",
        "docker/build-push-action@c3c9e263c25d99ce0380d002d59b67737d91b0dc",
        "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9",
    }
    runs = {
        name: [step.get("run", "") for step in job["steps"]]
        for name, job in jobs.items()
    }
    package = "python publishing/package-workload.py"
    assert (
        "python publishing/manifest.py --template manifest.template.json"
        in runs["check"]
    )
    assert package in runs["check"]
    assemble = 'python publishing/publish.py --image "$IMAGE"'
    assert runs["publish"].index(package) < runs["publish"].index(assemble)


def packaging_project(tmp_path, **workload):
    example = Path(__file__).resolve().parents[1] / "examples/apps/publication"
    (tmp_path / "publishing").mkdir()
    for name in ("package-workload.py", "publish.py", "manifest.py"):
        (tmp_path / "publishing" / name).write_bytes((example / name).read_bytes())
    shutil.copytree(example / "vendor", tmp_path / "publishing/vendor")
    template = manifest()
    template["workloads"][0].update(
        {"asset": "report.zip", "entrypoint": "submit.hp", **workload}
    )
    (tmp_path / "manifest.template.json").write_text(json.dumps(template))
    return tmp_path / "workload" / template["workloads"][0]["id"]


def package_workloads(tmp_path):
    return subprocess.run(
        [sys.executable, "publishing/package-workload.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )


def test_packaging_script_builds_reproducible_declared_workloads(tmp_path):
    source = packaging_project(tmp_path)
    (source / "lib").mkdir(parents=True)
    (source / "submit.hp").write_text("#!/bin/sh\n")
    (source / "lib" / "report.py").write_text("print('report')\n")
    built = []
    for _ in range(2):
        assert package_workloads(tmp_path).returncode == 0
        built.append((tmp_path / "workloads" / "report.zip").read_bytes())
    assert built[0] == built[1]
    with zipfile.ZipFile(io.BytesIO(built[0])) as archive:
        modes = {item.filename: item.external_attr >> 16 for item in archive.infolist()}
    assert modes == {"lib/report.py": 0o100644, "submit.hp": 0o100755}


def test_packaging_script_refuses_asset_outside_workloads(tmp_path):
    source = packaging_project(tmp_path, asset="../escaped.zip")
    source.mkdir(parents=True)
    (source / "submit.hp").write_text("#!/bin/sh\n")
    assert package_workloads(tmp_path).returncode != 0
    assert not (tmp_path / "escaped.zip").exists()


@pytest.mark.parametrize("create_source", [False, True])
def test_packaging_script_refuses_missing_or_empty_source(tmp_path, create_source):
    source = packaging_project(tmp_path)
    if create_source:
        (source / "lib").mkdir(parents=True)
    result = package_workloads(tmp_path)
    assert result.returncode != 0
    assert "has no files" in result.stderr
    assert not (tmp_path / "workloads" / "report.zip").exists()

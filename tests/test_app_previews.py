import asyncio
import gzip
import io
import json
import threading
import zipfile
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_app_discovery import GitHubFixture, package
from test_app_registry import document, registration

from app import migrations
from app.accounts import AccountStore
from app.app_compute import AppCompute
from app.app_contracts import ContractError, parse_document
from app.app_discovery import ReleaseDiscovery
from app.app_github import GitHubPublications
from app.app_previews import CHECKS, PreviewResult, PreviewService
from app.app_publication import MAX_PACKAGE_BYTES, sha256
from app.app_registry import AppRegistry, RegistrationConflict
from app.database import Database
from app.repository import JobRepository
from contracts.models import JobType
from ops.preview_core import ingress
from ops.preview_guest import GuestJournal, bundle, create_guest
from ops.preview_runtime import (
    ATTEMPT_LABEL,
    OWNER_LABEL,
    Fixtures,
    GuestConfig,
    GuestRuntime,
    reported_version,
)


def initial(app="notes"):
    _, archive, proof = package()
    return PreviewResult(
        attempt_id=uuid4(),
        app_id=app,
        manifest_sha256=proof["manifest_sha256"],
        bundle_sha256=sha256(archive),
        state="installing",
    ), archive


def test_guest_admission_is_idempotent_and_ready_keeps_app_slot(tmp_path):
    journal = GuestJournal(tmp_path / "guest.db")
    first, archive = initial()
    assert journal.admit(first, archive) == (first, True)
    assert journal.admit(first, archive) == (first, False)
    with pytest.raises(HTTPException) as busy:
        journal.admit(initial("other")[0], archive)
    assert busy.value.status_code == 409
    ready = first.model_copy(update={"state": "ready"})
    journal.save(ready)
    other, _ = initial("other")
    assert journal.admit(other, archive)[1]
    with pytest.raises(HTTPException):
        journal.admit(initial()[0], archive)
    journal.save(other.model_copy(update={"state": "retired"}))
    journal.save(first.model_copy(update={"state": "retired"}))
    replacement = initial()[0]
    assert journal.admit(replacement, archive)[1]
    with pytest.raises(HTTPException) as mismatch:
        journal.admit(
            replacement.model_copy(update={"bundle_sha256": "0" * 64}), archive
        )
    assert mismatch.value.status_code == 409
    restarted = GuestJournal(tmp_path / "guest.db")
    assert restarted.get(replacement.attempt_id) == replacement


def test_bundle_rehashes_app_release_and_assets():
    request, archive = initial()
    assert bundle(request, archive).manifest.app_id == "notes"
    for changes in (
        {"app_id": "other"},
        {"manifest_sha256": "0" * 64},
        {"bundle_sha256": "1" * 64},
    ):
        with pytest.raises(ContractError):
            bundle(request.model_copy(update=changes), archive)


def test_guest_two_live_slots_survive_restart_and_retirement_frees_one(tmp_path):
    journal = GuestJournal(tmp_path / "guest.db")
    first, archive = initial()
    second, _ = initial("other")
    third, _ = initial("third")
    for request in (first, second):
        journal.admit(request, archive)
        journal.save(request.model_copy(update={"state": "ready"}))
    journal = GuestJournal(tmp_path / "guest.db")
    with pytest.raises(HTTPException) as full:
        journal.admit(third, archive)
    assert full.value.status_code == 409
    assert len(journal.all()) == 2
    assert journal.admit(first, archive)[1] is False
    journal.save(first.model_copy(update={"state": "retired"}))
    assert journal.admit(third, archive) == (third, True)
    assert journal.get(second.attempt_id).state == "ready"


def install_without_docker(
    tmp_path, monkeypatch, app_id="notes", integrations=None, **manifest_changes
):
    secret = tmp_path / "synthetic-secret"
    secret.write_text("synthetic")
    fixture = Fixtures(
        secret_files={"provider-key": str(secret)}, integrations=integrations or {}
    )
    config = guest_config(tmp_path).model_copy(update={"apps": {app_id: fixture}})
    runtime = GuestRuntime(config)
    files, _, proof = package()
    manifest = json.loads(files["manifest.json"])
    manifest.update(manifest_changes, app_id=app_id)
    files["manifest.json"] = document(manifest)
    proof["manifest_sha256"] = sha256(files["manifest.json"])
    proof["assets"]["manifest.json"] = proof["manifest_sha256"]
    files["publication.json"] = document(proof)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive_file:
        for name, data in files.items():
            archive_file.writestr(name, data)
    archive = output.getvalue()
    request = PreviewResult(
        attempt_id=uuid4(),
        app_id=app_id,
        manifest_sha256=proof["manifest_sha256"],
        bundle_sha256=sha256(archive),
        state="installing",
    )
    release = bundle(request, archive)
    commands = []
    database = Database(runtime.root / str(request.attempt_id) / "core/core.db")
    worker_id = "preview-" + request.attempt_id.hex

    def docker(*args, **kwargs):
        commands.append(args)
        if args[:3] == ("image", "inspect", release.manifest.image):
            return json.dumps(
                [{"RepoDigests": [release.manifest.image], "Id": "sha256:" + "b" * 64}]
            )
        if "worker.main" in args:
            now = datetime.now(UTC)
            JobRepository(database).heartbeat(
                worker_id, [JobType.PYTHON_BATCH], now, now, None, None
            )
        return ""

    real_stat = Path.stat
    monkeypatch.setattr(
        Path,
        "stat",
        lambda path, **kwargs: SimpleNamespace(st_gid=123)
        if str(path) == "/var/run/docker.sock"
        else real_stat(path, **kwargs),
    )
    monkeypatch.setattr(runtime, "docker", docker)
    monkeypatch.setattr(runtime, "verify", lambda result, *args: result)
    runtime.install(request, release)
    return SimpleNamespace(
        runtime=runtime,
        release=release,
        commands=commands,
        database=database,
        worker_id=worker_id,
        request=request,
    )


@pytest.mark.parametrize("app_id", ["wishlist", "transport", "notes"])
def test_guest_install_has_only_selected_app_at_its_preview_route(
    tmp_path, monkeypatch, app_id
):
    installed = install_without_docker(tmp_path, monkeypatch, app_id)
    database = installed.database
    try:
        database.initialize()
        registry = AppRegistry(database)
        route = f"/previews/{installed.request.attempt_id}/{app_id}"
        managed = registry.managed_route(route + "/")
        assert managed is not None
        assert managed.registration.app_id == app_id
        assert managed.registration.route == route
        assert [app.key for app in registry.navigation()] == [
            "home",
            app_id,
            "jobs",
            "dashboard",
        ]
        assert [summary.app_id for summary in registry.summaries()] == [app_id]
        assert registry.managed_route("/" + app_id) is None
        with pytest.raises(RegistrationConflict, match="another definition"):
            registry.register(document(registration(app_id, route + "/changed")))
        before = database.path.read_bytes()
        commands = list(installed.commands)
        with pytest.raises(FileExistsError):
            installed.runtime.install(installed.request, installed.release)
        assert database.path.read_bytes() == before
        assert installed.commands == commands
        assert registry.managed_route(route + "/") == managed
    finally:
        database.close()


def test_upgraded_database_keeps_legacy_apps_and_refuses_preview_replacement(
    tmp_path, monkeypatch
):
    database = Database(tmp_path / "production-contract.db")
    try:
        with monkeypatch.context() as before:
            before.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:16])
            database.initialize()
        database.initialize()
        registry = AppRegistry(database)
        before = registry.summaries()
        assert {summary.app_id for summary in before} == {
            "habits",
            "wishlist",
            "transport",
            "sorter",
        }
        with pytest.raises(RegistrationConflict, match="another definition"):
            registry.register(
                document(registration("wishlist", f"/previews/{uuid4()}/wishlist"))
            )
        database.initialize()
        assert registry.summaries() == before
    finally:
        database.close()


def test_guest_install_keeps_other_attempt_registry(tmp_path, monkeypatch):
    first = install_without_docker(tmp_path, monkeypatch, "wishlist")
    try:
        before = AppRegistry(first.database).summaries()
        second = install_without_docker(tmp_path, monkeypatch, "transport")
        try:
            assert first.database.path != second.database.path
            assert AppRegistry(first.database).summaries() == before
            assert [
                item.app_id for item in AppRegistry(second.database).summaries()
            ] == ["transport"]
        finally:
            second.database.close()
    finally:
        first.database.close()


def maintenance(name, timeout_seconds):
    return {
        "name": name,
        "argv": ["python", "-m", "notes." + name],
        "interval_hours": 6,
        "timeout_seconds": timeout_seconds,
    }


@pytest.mark.parametrize(
    ("items", "admitted"),
    [
        ([], 300),
        ([maintenance("cleanup", 600)], 600),
        ([maintenance("cleanup", 60), maintenance("reindex", 900)], 900),
    ],
)
def test_guest_install_admits_release_maintenance_timeouts(
    tmp_path, monkeypatch, items, admitted
):
    installed = install_without_docker(
        tmp_path, monkeypatch, "wishlist", maintenance=items
    )
    try:
        managed = AppRegistry(installed.database).managed_app("wishlist")
        assert managed is not None
        resources = managed.registration.bindings.resources
        assert resources.maintenance_timeout_seconds == admitted
    finally:
        installed.database.close()


def test_guest_runtime_container_limits_and_persisted_worker_capacity(
    tmp_path, monkeypatch
):
    installed = install_without_docker(tmp_path, monkeypatch)
    release, database = installed.release, installed.database
    containers = [args for args in installed.commands if args[0] == "run"]
    assert len(containers) == 3
    for args in containers:
        assert "--publish" not in args
        assert args[args.index("--memory") + 1] == str(256 * 1024**2)
        assert args[args.index("--cpus") + 1] == (
            "0.5" if args[-1] == release.manifest.image else "1"
        )
    with database.connect() as connection:
        worker = connection.execute(
            "SELECT * FROM workers WHERE id = ?", (installed.worker_id,)
        ).fetchone()
        assert worker["enabled"] == 1
        assert worker["max_job_cpu"] == 1
        assert worker["max_job_memory_mb"] == 512
    database.close()


def test_preview_user_can_browse_home_and_shared_in_synthetic_storage(
    tmp_path, monkeypatch
):
    installed = install_without_docker(tmp_path, monkeypatch)
    core = next(args for args in installed.commands if "ops.preview_core" in args)
    storage = Path(
        next(
            value.split("=", 1)[1]
            for value in core
            if value.startswith("HOME_PLATFORM_STORAGE_DIR=")
        )
    )
    assert storage.is_relative_to(installed.runtime.root)
    user = AccountStore(installed.database).resolve_external_identity(
        "tailscale", installed.runtime.config.fixture_login
    )
    assert user is not None
    compute = AppCompute(
        SimpleNamespace(repository=SimpleNamespace(database=installed.database)),
        SimpleNamespace(
            storage_directory=storage,
            member_storage_enabled=True,
            member_storage_user_ids=frozenset(),
        ),
    )
    context = SimpleNamespace(
        user=user,
        app=SimpleNamespace(
            app_id="notes", registration=SimpleNamespace(capabilities=("files",))
        ),
    )
    assert compute.files(context, "Home", 100, 0) == {
        "items": [
            {
                "name": "Workspace",
                "path": "Home/Workspace",
                "kind": "directory",
                "size_bytes": None,
            }
        ]
    }
    assert compute.files(context, "Shared", 100, 0) == {"items": []}
    installed.database.close()


def test_unreachable_staging_says_so_and_the_same_attempt_completes_later(tmp_path):
    database = Database(tmp_path / "core.db")
    database.initialize()
    AppRegistry(database).register(document(registration()))
    fixture = GitHubFixture()
    staging_running = False
    attempts = []

    def respond(request):
        if not staging_running:
            raise httpx.ConnectError("refused", request=request)
        attempts.append(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(
            200,
            json={
                "attempt_id": attempts[-1],
                "app_id": request.headers["X-App-ID"],
                "manifest_sha256": request.headers["X-Release-SHA256"],
                "bundle_sha256": request.headers["X-Bundle-SHA256"],
                "state": "installing",
            },
        )

    async def run():
        nonlocal staging_running
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fixture.respond)
        ) as client:
            await ReleaseDiscovery(database, GitHubPublications(client)).discover(
                "notes"
            )
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            previews = PreviewService(
                database, client, "https://preview.example", "t" * 32
            )
            digest = fixture.proof["manifest_sha256"]
            with pytest.raises(ContractError, match="Staging is not running"):
                await previews.preview("notes", digest)
            with database.connect() as connection:
                pending = connection.execute(
                    "SELECT attempt_id, state FROM app_preview_attempts"
                ).fetchall()
            assert [tuple(row) for row in pending] == [(pending[0][0], "installing")]
            staging_running = True
            result = await previews.preview("notes", digest)
            assert attempts == [pending[0][0]]
            assert result.state == "installing"

    asyncio.run(run())
    database.close()


def test_core_preserves_uncertain_attempt_and_checks_every_response_key(tmp_path):
    database = Database(tmp_path / "core.db")
    database.initialize()
    registry = AppRegistry(database)
    registry.register(document(registration()))
    fixture = GitHubFixture()
    received = []
    response_mode = "lost"

    def respond(request):
        received.append(request)
        if response_mode == "lost":
            raise httpx.ReadTimeout("lost", request=request)
        result = {
            "attempt_id": request.url.path.rsplit("/", 1)[1],
            "app_id": request.headers["X-App-ID"],
            "manifest_sha256": request.headers["X-Release-SHA256"],
            "bundle_sha256": request.headers["X-Bundle-SHA256"],
            "state": "installing",
        }
        if response_mode == "mismatch":
            result["bundle_sha256"] = "0" * 64
        elif response_mode == "ready":
            result.update(
                state="ready",
                image_id="sha256:" + "b" * 64,
                preview_url=f"https://preview.example/previews/{result['attempt_id']}/notes/",
                checks=dict.fromkeys(CHECKS, True),
                version="1.4.0",
            )
        elif response_mode == "retired":
            result["state"] = "retired"
        return httpx.Response(200, json=result)

    async def run():
        nonlocal response_mode
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fixture.respond)
        ) as client:
            await ReleaseDiscovery(database, GitHubPublications(client)).discover(
                "notes"
            )
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            previews = PreviewService(
                database, client, "https://preview.example", "t" * 32
            )
            digest = fixture.proof["manifest_sha256"]
            with pytest.raises(ContractError, match="uncertain"):
                await previews.preview("notes", digest)
            response_mode = "mismatch"
            with pytest.raises(ContractError, match="does not match"):
                await previews.preview("notes", digest)
            assert received[0].url == received[1].url
            assert received[0].content == received[1].content == fixture.archive
            response_mode = "ready"
            ready = await previews.preview("notes", digest)
            assert (ready.state, ready.version) == ("ready", "1.4.0")
            with database.connect() as connection:
                stored = connection.execute(
                    "SELECT result_json FROM app_preview_attempts WHERE state = 'ready'"
                ).fetchone()[0]
            assert parse_document(PreviewResult, stored.encode()).version == "1.4.0"
            response_mode = "retired"
            assert (await previews.evict(ready.attempt_id)).state == "retired"
            response_mode = "installing"
            replacement = await previews.preview("notes", digest)
            assert replacement.attempt_id != ready.attempt_id
        with database.connect() as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM app_preview_attempts"
                ).fetchone()[0]
                == 2
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    ("response", "version"),
    [
        (httpx.Response(200, json={"service": "notes", "version": " 1.4.0 "}), "1.4.0"),
        (httpx.Response(200, json={"version": "x" * 64}), "x" * 64),
        (httpx.Response(200, json={"version": "x" * 65}), None),
        (httpx.Response(200, json={"version": "1.4\n<b>"}), None),
        (httpx.Response(200, json={"version": "1.4.0\u202e"}), None),
        (httpx.Response(200, json={"version": 14}), None),
        (httpx.Response(200, json=["1.4.0"]), None),
        (httpx.Response(200, content=b"1.4.0"), None),
        (httpx.Response(500, json={"version": "1.4.0"}), None),
    ],
)
def test_reported_version_is_short_printable_text_or_nothing(response, version):
    assert reported_version(response) == version


@pytest.mark.parametrize("version", ["", "x" * 65, "1.4\n", "1.4.0\u00e9"])
def test_core_refuses_staging_versions_outside_the_contract(version):
    request, _ = initial()
    with pytest.raises(ContractError):
        parse_document(
            PreviewResult,
            json.dumps(
                {**request.model_dump(mode="json"), "version": version}
            ).encode(),
        )


def test_guest_retirement_keeps_the_reported_version(tmp_path, monkeypatch):
    app = create_guest(guest_config(tmp_path))
    request, archive = initial()
    app.state.journal.admit(request, archive)
    app.state.journal.save(
        request.model_copy(update={"state": "ready", "version": "1.4.0"})
    )
    monkeypatch.setattr(app.state.runtime, "owned_attempts", set)
    monkeypatch.setattr(app.state.runtime, "cleanup", lambda attempt: None)
    with TestClient(app, base_url="https://preview.example") as client:
        retired = client.delete(
            f"/control/attempts/{request.attempt_id}",
            headers={
                "Authorization": "Bearer " + "t" * 32,
                "X-App-ID": request.app_id,
                "X-Release-SHA256": request.manifest_sha256,
                "X-Bundle-SHA256": request.bundle_sha256,
            },
        )
    assert retired.status_code == 200
    assert (retired.json()["state"], retired.json()["version"]) == ("retired", "1.4.0")


def test_manual_ingestion_is_not_preview_authority(tmp_path):
    database = Database(tmp_path / "core.db")
    database.initialize()
    registry = AppRegistry(database)
    registry.register(document(registration()))
    files, _, proof = package()
    registry.ingest_release("notes", files["manifest.json"])

    async def run():
        async with httpx.AsyncClient() as client:
            with pytest.raises(ContractError, match="workflow-verified"):
                await PreviewService(
                    database, client, "https://preview.example", "t" * 32
                ).preview("notes", proof["manifest_sha256"])

    asyncio.run(run())


def guest_config(tmp_path):
    token = tmp_path / "control-token"
    token.write_text("t" * 32)
    return GuestConfig(
        root_directory=str(tmp_path / "guest"),
        public_origin="https://preview.example",
        control_token_file=str(token),
        core_image="sha256:" + "0" * 64,
        worker_image="sha256:" + "1" * 64,
        fixture_login="preview@example.test",
        apps={},
    )


def endpoint_metadata(runtime, attempt):
    labels = {OWNER_LABEL: runtime.owner, ATTEMPT_LABEL: str(attempt)}
    container = {
        "Config": {"Labels": labels.copy()},
        "State": {"Running": True},
        "HostConfig": {"PortBindings": {}},
        "NetworkSettings": {
            "Networks": {
                runtime.name(attempt, "net"): {
                    "IPAddress": "172.18.0.2",
                    "NetworkID": "owned-network",
                }
            }
        },
    }
    network = {
        "Labels": labels.copy(),
        "Id": "owned-network",
        "Internal": True,
        "Driver": "bridge",
    }
    return container, network


def test_guest_endpoint_resolves_current_owned_address(tmp_path, monkeypatch):
    runtime = GuestRuntime(guest_config(tmp_path))
    attempt = uuid4()
    container, network = endpoint_metadata(runtime, attempt)
    monkeypatch.setattr(
        runtime,
        "docker",
        lambda kind, *args: json.dumps([container if kind == "container" else network]),
    )
    assert runtime.endpoint(attempt, "core", 8001) == "http://172.18.0.2:8001"
    container["NetworkSettings"]["Networks"][runtime.name(attempt, "net")][
        "IPAddress"
    ] = "172.18.0.4"
    assert runtime.endpoint(attempt, "core", 8001) == "http://172.18.0.4:8001"


@pytest.mark.parametrize(
    "fault",
    [
        "foreign-container",
        "foreign-network",
        "external-network",
        "extra-network",
        "wrong-network",
        "no-address",
        "loopback",
        "unspecified",
        "published-port",
        "stopped",
        "malformed",
    ],
)
def test_guest_endpoint_refuses_unowned_or_unsafe_targets(tmp_path, monkeypatch, fault):
    runtime = GuestRuntime(guest_config(tmp_path))
    attempt = uuid4()
    container, network = endpoint_metadata(runtime, attempt)
    attachments = container["NetworkSettings"]["Networks"]
    address = attachments[runtime.name(attempt, "net")]
    if fault == "foreign-container":
        container["Config"]["Labels"][ATTEMPT_LABEL] = str(uuid4())
    elif fault == "foreign-network":
        network["Labels"][OWNER_LABEL] = "other"
    elif fault == "external-network":
        network["Internal"] = False
    elif fault == "extra-network":
        attachments["other-network"] = address.copy()
    elif fault == "wrong-network":
        address["NetworkID"] = "other-network"
    elif fault in {"no-address", "loopback", "unspecified"}:
        address["IPAddress"] = {
            "no-address": "",
            "loopback": "127.0.0.1",
            "unspecified": "0.0.0.0",
        }[fault]
    elif fault == "published-port":
        container["HostConfig"]["PortBindings"] = {"8001/tcp": [{}]}
    elif fault == "stopped":
        container["State"]["Running"] = False
    else:
        container = {}
    monkeypatch.setattr(
        runtime,
        "docker",
        lambda kind, *args: json.dumps([container if kind == "container" else network]),
    )
    with pytest.raises(ContractError, match="not an owned internal container"):
        runtime.endpoint(attempt, "core", 8001)


def test_guest_authenticates_before_receiving_upload(tmp_path):
    app = create_guest(guest_config(tmp_path))
    consumed = False

    async def content():
        nonlocal consumed
        consumed = True
        yield b"untrusted"

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://preview.example"
        ) as client:
            response = await client.put(
                "/control/attempts/" + str(uuid4()), content=content()
            )
            assert response.status_code == 401

    asyncio.run(run())
    assert not consumed
    assert app.state.journal.all() == []


def test_missing_fixture_refuses_without_docker_or_files(tmp_path, monkeypatch):
    runtime = GuestRuntime(guest_config(tmp_path))

    def forbidden(*args, **kwargs):
        raise AssertionError("Docker must not run before fixture policy validation")

    monkeypatch.setattr(runtime, "docker", forbidden)
    request, archive = initial()
    with pytest.raises(ContractError, match="bindings are missing"):
        runtime.install(request, bundle(request, archive))
    assert not (runtime.root / str(request.attempt_id)).exists()


@pytest.mark.parametrize(
    "fault, update",
    [
        ("app_id", {"app_id": "other"}),
        ("manifest_sha256", {"manifest_sha256": "0" * 64}),
        ("bundle_sha256", {"bundle_sha256": "1" * 64}),
    ],
)
def test_guest_verify_refuses_attempt_mismatch_before_running_checker(
    tmp_path, monkeypatch, fault, update
):
    runtime = GuestRuntime(guest_config(tmp_path))
    request, archive = initial()
    release = bundle(request, archive)
    mismatched = request.model_copy(update=update)

    def forbidden_docker(*args, **kwargs):
        raise AssertionError("Docker must not run before the attempt binding guard")

    def forbidden_check(*args, **kwargs):
        raise AssertionError("Checker must not run before the attempt binding guard")

    monkeypatch.setattr(runtime, "docker", forbidden_docker)
    monkeypatch.setattr("ops.preview_runtime.check_running", forbidden_check)
    with pytest.raises(ContractError, match="does not match selected attempt"):
        runtime.verify(mismatched, release, "sha256:" + "a" * 64)


def test_guest_restart_adopts_ready_cleans_incomplete_and_orphan(tmp_path, monkeypatch):
    app = create_guest(guest_config(tmp_path))
    journal = app.state.journal
    ready, archive = initial()
    journal.admit(ready, archive)
    ready = ready.model_copy(
        update={"state": "ready", "image_id": "sha256:" + "a" * 64}
    )
    journal.save(ready)
    incomplete, _ = initial("other")
    journal.admit(incomplete, archive)
    orphan = uuid4()
    cleaned = []
    verified = []
    monkeypatch.setattr(app.state.runtime, "owned_attempts", lambda: {orphan})
    monkeypatch.setattr(app.state.runtime, "cleanup", cleaned.append)

    def verify(result, release, image_id):
        verified.append((result.attempt_id, release.manifest.app_id, image_id))
        return result

    monkeypatch.setattr(app.state.runtime, "verify", verify)
    with TestClient(app):
        assert journal.get(ready.attempt_id).state == "ready"
        assert journal.get(incomplete.attempt_id).state == "failed"
    assert set(cleaned) == {orphan, incomplete.attempt_id}
    assert verified == [(ready.attempt_id, "notes", ready.image_id)]


def test_guest_cleanup_uncertainty_keeps_installation_slot(tmp_path, monkeypatch):
    app = create_guest(guest_config(tmp_path))
    journal = app.state.journal
    request, archive = initial()
    journal.admit(request, archive)
    monkeypatch.setattr(app.state.runtime, "owned_attempts", set)

    def uncertain(attempt):
        raise ContractError("Docker unavailable")

    monkeypatch.setattr(app.state.runtime, "cleanup", uncertain)
    with TestClient(app):
        assert journal.get(request.attempt_id).state == "installing"
        with pytest.raises(HTTPException) as blocked:
            journal.admit(initial("other")[0], archive)
        assert blocked.value.status_code == 409


def test_guest_unexpected_install_error_releases_global_slot(tmp_path, monkeypatch):
    app = create_guest(guest_config(tmp_path))
    request, archive = initial()
    monkeypatch.setattr(app.state.runtime, "owned_attempts", set)
    monkeypatch.setattr(app.state.runtime, "cleanup", lambda attempt: None)

    def broken(*args):
        raise KeyError("unexpected")

    monkeypatch.setattr(app.state.runtime, "install", broken)
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
    assert app.state.journal.get(request.attempt_id).state == "failed"
    assert app.state.journal.admit(initial("other")[0], archive)[1]


def test_guest_install_error_stores_generic_message_not_exception_text(
    tmp_path, monkeypatch
):
    app = create_guest(guest_config(tmp_path))
    request, archive = initial()
    monkeypatch.setattr(app.state.runtime, "owned_attempts", set)
    monkeypatch.setattr(app.state.runtime, "cleanup", lambda attempt: None)
    sensitive = "leaked secret token abc123 at /etc/shadow"

    def broken(*args):
        raise OSError(sensitive)

    monkeypatch.setattr(app.state.runtime, "install", broken)
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
    stored = app.state.journal.get(request.attempt_id)
    assert stored.state == "failed"
    assert stored.error == "Guest installation or reconciliation failed"
    assert sensitive not in (stored.error or "")


def test_core_retires_attempt_that_staging_never_admitted(tmp_path):
    database = Database(tmp_path / "core.db")
    database.initialize()
    AppRegistry(database).register(document(registration()))
    fixture = GitHubFixture()

    def refuse(request):
        return httpx.Response(409 if request.method == "PUT" else 404)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fixture.respond)
        ) as client:
            await ReleaseDiscovery(database, GitHubPublications(client)).discover(
                "notes"
            )
        async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
            previews = PreviewService(
                database, client, "https://preview.example", "t" * 32
            )
            digest = fixture.proof["manifest_sha256"]
            with pytest.raises(ContractError, match="Staging is busy"):
                await previews.preview("notes", digest)
            with database.connect() as connection:
                attempt = connection.execute(
                    "SELECT attempt_id FROM app_preview_attempts"
                ).fetchone()[0]
            retired = await previews.evict(UUID(attempt))
            assert retired.state == "retired" and retired.manifest_sha256 == digest
            with pytest.raises(ContractError, match="Staging is busy"):
                await previews.preview("notes", digest)
        with database.connect() as connection:
            states = connection.execute(
                "SELECT state FROM app_preview_attempts ORDER BY rowid"
            ).fetchall()
        assert [row[0] for row in states] == ["retired", "installing"]

    asyncio.run(run())


def test_core_refuses_to_retire_confirmed_attempt_on_404(tmp_path):
    database = Database(tmp_path / "core.db")
    database.initialize()
    AppRegistry(database).register(document(registration()))
    fixture = GitHubFixture()

    def respond(request):
        if request.method == "DELETE":
            return httpx.Response(404)
        result = {
            "attempt_id": request.url.path.rsplit("/", 1)[1],
            "app_id": request.headers["X-App-ID"],
            "manifest_sha256": request.headers["X-Release-SHA256"],
            "bundle_sha256": request.headers["X-Bundle-SHA256"],
            "state": "ready",
            "image_id": "sha256:" + "b" * 64,
            "preview_url": (
                "https://preview.example/previews/"
                + request.url.path.rsplit("/", 1)[1]
                + "/notes/"
            ),
            "checks": dict.fromkeys(CHECKS, True),
        }
        return httpx.Response(200, json=result)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fixture.respond)
        ) as client:
            await ReleaseDiscovery(database, GitHubPublications(client)).discover(
                "notes"
            )
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            previews = PreviewService(
                database, client, "https://preview.example", "t" * 32
            )
            digest = fixture.proof["manifest_sha256"]
            ready = await previews.preview("notes", digest)
            assert ready.state == "ready"
            with pytest.raises(ContractError, match="did not confirm"):
                await previews.evict(ready.attempt_id)
        with database.connect() as connection:
            row = connection.execute(
                "SELECT state, result_json FROM app_preview_attempts "
                "WHERE attempt_id = ?",
                (str(ready.attempt_id),),
            ).fetchone()
        assert row["state"] == "ready"
        assert json.loads(row["result_json"])["state"] == "ready"

    asyncio.run(run())


def test_preview_ingress_forwards_app_headers_and_replaces_identity(
    tmp_path, monkeypatch
):
    app = create_guest(guest_config(tmp_path))
    journal = app.state.journal
    request, archive = initial()
    journal.admit(request, archive)
    journal.save(request.model_copy(update={"state": "ready"}))
    core = app.state.runtime.root / str(request.attempt_id) / "core"
    core.mkdir(parents=True)
    (core / "bridge-token").write_text("synthetic-bridge")
    received = []

    class Core(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, self.headers, body))
            self.send_response(201)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Core)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    monkeypatch.setattr(app.state.runtime, "owned_attempts", set)
    monkeypatch.setattr(app.state.runtime, "verify", lambda result, *args: result)
    monkeypatch.setattr(
        app.state.runtime,
        "endpoint",
        lambda attempt, role, port: f"http://127.0.0.1:{server.server_port}",
    )
    try:
        with TestClient(app, base_url="https://preview.example") as client:
            response = client.post(
                f"/previews/{request.attempt_id}/notes/upload",
                content=b"synthetic upload",
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-CSRF-Token": "app-csrf",
                    "X-Requested-With": "fetch",
                    "Cookie": "notes_csrf=app-csrf; home_platform_session=stolen",
                    "Tailscale-User-Login": "preview@example.test",
                    "Tailscale-User-Name": "Forged Name",
                    "X-Platform-User-Context": "forged",
                    "X-Forwarded-For": "203.0.113.9",
                    "X-API-Token": "forged-token",
                    "X-Preview-Bridge": "forged-bridge",
                },
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert response.status_code == 201
    [(path, headers, body)] = received
    assert path == f"/previews/{request.attempt_id}/notes/upload"
    assert body == b"synthetic upload"
    assert headers["X-CSRF-Token"] == "app-csrf"
    assert headers["X-Requested-With"] == "fetch"
    assert headers.get_all("Cookie") == ["notes_csrf=app-csrf"]
    assert headers.get_all("Tailscale-User-Login") == ["preview@example.test"]
    assert headers.get_all("X-Preview-Bridge") == ["synthetic-bridge"]
    for stripped in (
        "Tailscale-User-Name",
        "X-Platform-User-Context",
        "X-Forwarded-For",
        "X-API-Token",
    ):
        assert stripped not in headers


def test_preview_ingress_decodes_app_response_regardless_of_browser_accept_encoding(
    tmp_path, monkeypatch
):
    app = create_guest(guest_config(tmp_path))
    journal = app.state.journal
    request, archive = initial()
    journal.admit(request, archive)
    journal.save(request.model_copy(update={"state": "ready"}))
    core = app.state.runtime.root / str(request.attempt_id) / "core"
    core.mkdir(parents=True)
    (core / "bridge-token").write_text("synthetic-bridge")
    body = b"hello from the previewed app"

    class Core(BaseHTTPRequestHandler):
        def do_GET(self):
            # A real app may pick br/zstd when the client offers them, but this
            # preview hop's httpx has neither codec installed, so it would hand
            # the browser the still-encoded bytes below if it ever asked for them.
            if "br" in self.headers.get("Accept-Encoding", ""):
                payload, encoding = b"\x8b\x06\x80opaque-br-bytes", "br"
            else:
                payload, encoding = gzip.compress(body), "gzip"
            self.send_response(200)
            self.send_header("Content-Encoding", encoding)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Set-Cookie", "first=1")
            self.send_header("Set-Cookie", "second=2")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Core)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    monkeypatch.setattr(app.state.runtime, "owned_attempts", set)
    monkeypatch.setattr(app.state.runtime, "verify", lambda result, *args: result)
    monkeypatch.setattr(
        app.state.runtime,
        "endpoint",
        lambda attempt, role, port: f"http://127.0.0.1:{server.server_port}",
    )
    try:
        with TestClient(app, base_url="https://preview.example") as client:
            response = client.get(
                f"/previews/{request.attempt_id}/notes/page",
                headers={
                    "Accept-Encoding": "br, gzip, deflate",
                    "Tailscale-User-Login": "preview@example.test",
                },
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert response.status_code == 200
    assert response.content == body
    assert response.headers.get_list("content-length") == [str(len(body))]
    assert "content-encoding" not in response.headers
    assert response.headers.get_list("set-cookie") == ["first=1", "second=2"]


def test_preview_ingress_refuses_an_app_response_beyond_the_package_limit(
    tmp_path, monkeypatch
):
    app = create_guest(guest_config(tmp_path))
    request, archive = initial()
    app.state.journal.admit(request, archive)
    app.state.journal.save(request.model_copy(update={"state": "ready"}))
    core = app.state.runtime.root / str(request.attempt_id) / "core"
    core.mkdir(parents=True)
    (core / "bridge-token").write_text("synthetic-bridge")
    bomb = gzip.compress(b"\0" * (MAX_PACKAGE_BYTES + 1))

    def respond(_request):
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=bomb)

    transport = httpx.MockTransport(respond)
    client = httpx.AsyncClient
    monkeypatch.setattr(
        "ops.preview_guest.httpx.AsyncClient",
        lambda **kwargs: client(**kwargs, transport=transport),
    )
    monkeypatch.setattr(app.state.runtime, "owned_attempts", set)
    monkeypatch.setattr(app.state.runtime, "verify", lambda result, *args: result)
    monkeypatch.setattr(
        app.state.runtime, "endpoint", lambda *_args: "http://preview-core.invalid"
    )
    with TestClient(app, base_url="https://preview.example") as browser:
        response = browser.get(
            f"/previews/{request.attempt_id}/notes/page",
            headers={"Tailscale-User-Login": "preview@example.test"},
        )
    assert response.status_code == 502
    assert len(response.content) < 1024


def test_preview_core_ingress_sends_decoded_body_with_its_own_length(
    tmp_path, monkeypatch
):
    token = tmp_path / "bridge-token"
    token.write_text("synthetic-bridge")
    monkeypatch.setenv("PREVIEW_BRIDGE_TOKEN_FILE", str(token))
    body = b"<!doctype html>" + b"wishlist " * 200
    compressed = gzip.compress(body)

    def respond(request):
        return httpx.Response(
            200,
            headers=[
                ("Content-Encoding", "gzip"),
                ("Content-Length", str(len(compressed))),
                ("Set-Cookie", "first=1"),
                ("Set-Cookie", "second=2"),
            ],
            content=compressed,
        )

    monkeypatch.setattr(
        ingress.state,
        "client",
        httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        raising=False,
    )
    response = TestClient(ingress).get(
        "/wishlist/", headers={"X-Preview-Bridge": "synthetic-bridge"}
    )
    assert response.status_code == 200
    assert response.content == body
    assert response.headers.get_list("content-length") == [str(len(body))]
    assert "content-encoding" not in response.headers
    assert response.headers.get_list("set-cookie") == ["first=1", "second=2"]


def test_guest_serves_platform_bar_from_the_apps_own_ready_attempt(
    tmp_path, monkeypatch
):
    app = create_guest(guest_config(tmp_path))
    journal = app.state.journal
    root = app.state.runtime.root

    def attempt(app_id, state):
        request, archive = initial(app_id)
        journal.admit(request, archive)
        journal.save(request.model_copy(update={"state": state}))
        core = root / str(request.attempt_id) / "core"
        core.mkdir(parents=True)
        (core / "bridge-token").write_text(f"bridge-{request.attempt_id}")
        return request.attempt_id

    received = []
    endpoints = []

    class Core(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.path, self.headers["X-Preview-Bridge"]))
            payload = f"<nav>{self.headers['X-Preview-Bridge']}</nav>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Core)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    monkeypatch.setattr(app.state.runtime, "owned_attempts", set)

    def endpoint(attempt, role, port):
        endpoints.append((attempt, role, port))
        return f"http://127.0.0.1:{server.server_port}"

    monkeypatch.setattr(app.state.runtime, "endpoint", endpoint)
    login = {"Tailscale-User-Login": "preview@example.test"}
    try:
        with TestClient(app, base_url="https://preview.example") as client:
            attempt("wishlist", "retired")
            attempt("habits", "retired")
            attempt("notes", "failed")
            wishlist = attempt("wishlist", "ready")
            transport = attempt("transport", "ready")
            bar = client.get("/platform/bar?app=wishlist&extra=1", headers=login)
            other = client.get(
                "/platform/bar", params={"app": "transport"}, headers=login
            )
            missing = {
                app_id: client.get(
                    "/platform/bar", params={"app": app_id}, headers=login
                ).status_code
                for app_id in ("unknown", "habits", "notes", "../wishlist", "Wishlist")
            }
            anonymous = client.get("/platform/bar?app=wishlist")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert bar.status_code == 200
    assert bar.text == f"<nav>bridge-{wishlist}</nav>"
    assert bar.headers["content-security-policy"] == "frame-ancestors 'self'"
    assert bar.headers["x-frame-options"] == "SAMEORIGIN"
    assert other.status_code == 200
    assert other.text == f"<nav>bridge-{transport}</nav>"
    assert missing == dict.fromkeys(missing, 404)
    assert anonymous.status_code == 401
    assert received == [
        ("/platform/bar?app=wishlist", f"bridge-{wishlist}"),
        ("/platform/bar?app=transport", f"bridge-{transport}"),
    ]
    assert endpoints == [(wishlist, "core", 8001), (transport, "core", 8001)]

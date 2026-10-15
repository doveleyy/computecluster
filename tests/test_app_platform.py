import asyncio
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from test_app_registry import document, manifest, registration

from app import migrations
from app.accounts import AccountStore, UserCreate, encode_session
from app.app_contracts import (
    PLATFORM_VARIABLES,
    ContractError,
    Registration,
    parse_document,
)
from app.app_gateway import (
    Forward,
    UpstreamFailure,
    cookie_violation,
    create_upstream_client,
    forward_headers,
    internal_path,
    proxy,
    rewrite_location,
    screen_response,
)
from app.app_identity import AppContext, AppIdentity
from app.app_registry import AppRegistry, EnvironmentMismatch
from app.app_runtime import docker_run_argv, runtime_spec
from app.config import (
    DEFAULT_APP_API_URL,
    AppPlatformSettings,
    InstanceFacts,
    load_app_platform,
)
from app.dashboard_auth import SESSION_COOKIE
from app.database import Database
from app.identity import ADMIN_USER_ID
from app.main import create_app


def managed(
    app_id="notes",
    route="/apps/notes",
    access="linked-user",
    state="/private/test-state",
):
    value = registration(app_id, route)
    value["installation"]["owner"] = "platform"
    value["access"] = access
    value["bindings"]["state_directory"] = state
    return value


@pytest.fixture
def platform(tmp_path):
    database = Database(tmp_path / "platform.db")
    database.initialize()
    registry = AppRegistry(database)
    instance = InstanceFacts("acceptance", DEFAULT_APP_API_URL, "UTC")
    settings = AppPlatformSettings(instance, b"c" * 32)
    registry.publish_instance(instance)
    registry.register(document(managed(state=str(tmp_path / "state"))))
    registry.register(
        document(
            managed(
                "admin-notes",
                "/apps/admin-notes",
                "administrator",
                str(tmp_path / "admin-state"),
            )
        )
    )
    accounts = AccountStore(database)
    user = accounts.create(
        UserCreate(username="alice", password="a long test password")
    )
    accounts.link_external_identity(user.id, "tailscale", "alice@example.test", "Alice")
    clock = [1000]
    identity = AppIdentity(accounts, registry, settings, lambda: clock[0])
    for app_id in ("notes", "admin-notes"):
        registry.issue_credential(app_id, tmp_path / "credentials" / app_id)
    yield SimpleNamespace(
        database=database,
        registry=registry,
        settings=settings,
        accounts=accounts,
        user=user,
        identity=identity,
        clock=clock,
    )
    database.close()


def request(
    path="/apps/notes/", scheme="https", cookie=None, login="alice@example.test"
):
    headers = [(b"host", b"platform.test")]
    if login is not None:
        headers.append((b"tailscale-user-login", login.encode()))
    if cookie is not None:
        headers.append((b"cookie", f"{SESSION_COOKIE}={cookie}".encode()))
    return Request(
        {
            "type": "http",
            "scheme": scheme,
            "path": path,
            "query_string": b"",
            "headers": headers,
            "app": SimpleNamespace(
                state=SimpleNamespace(settings=SimpleNamespace(api_token="admin-key"))
            ),
        }
    )


def bearer(platform, app_id="notes"):
    credential = platform.registry.active_credential(app_id)
    return "Bearer " + credential.file_path.read_text().strip()


def assertion(platform, app_id="notes", cookie=None):
    app = platform.registry.managed_app(app_id)
    result = platform.identity.admit(app, request(app.route + "/", cookie=cookie))
    assert isinstance(result, str)
    return result


def resign(platform, token, **changes):
    payload = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    payload.update(changes)
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    message = b"v1." + raw
    signature = hmac.new(
        platform.settings.context_key,
        b"home-platform/app-user-context\x00" + message,
        hashlib.sha256,
    ).digest()
    return (message + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()


def test_instance_facts_are_pinned_and_replacement_is_refused(platform):
    platform.registry.publish_instance(platform.settings.instance)
    for changes in (
        {"environment": "production"},
        {"app_api_url": "http://other/api/apps/v1"},
        {"timezone": "Asia/Singapore"},
    ):
        with pytest.raises(EnvironmentMismatch):
            platform.registry.publish_instance(
                replace(platform.settings.instance, **changes)
            )
    with platform.database.connect() as connection:
        for statement in (
            "DELETE FROM platform_instance",
            "UPDATE platform_instance SET timezone = 'changed'",
            "INSERT OR REPLACE INTO platform_instance "
            "VALUES (1, 'production', 'x', 'x')",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)


def test_load_settings_requires_paired_file_only_key_and_admin_token(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("HOME_PLATFORM_ENVIRONMENT", raising=False)
    monkeypatch.delenv("HOME_PLATFORM_APP_CONTEXT_KEY_FILE", raising=False)
    assert load_app_platform(None) is None
    monkeypatch.setenv("HOME_PLATFORM_ENVIRONMENT", "acceptance")
    with pytest.raises(ValueError, match="together"):
        load_app_platform("admin-key")
    key = tmp_path / "key"
    key.write_bytes(b"c" * 32)
    monkeypatch.setenv("HOME_PLATFORM_APP_CONTEXT_KEY_FILE", str(key))
    with pytest.raises(ValueError, match="administrator"):
        load_app_platform(None)
    settings = load_app_platform("admin-key")
    assert settings.instance == InstanceFacts("acceptance", DEFAULT_APP_API_URL, "UTC")
    with pytest.raises(ValueError, match="separate"):
        load_app_platform("c" * 32)
    key.write_bytes(b"short")
    with pytest.raises(ValueError, match="32 bytes"):
        load_app_platform("admin-key")
    key.unlink()
    with pytest.raises(ValueError) as failure:
        load_app_platform("admin-key")
    assert str(tmp_path) not in str(failure.value)


@pytest.mark.parametrize(
    "name,value",
    [
        ("HOME_PLATFORM_ENVIRONMENT", "Production"),
        ("HOME_PLATFORM_APP_API_URL", "http://user:password@core/api/apps/v1"),
        ("HOME_PLATFORM_APP_API_URL", "http://core/api/apps/v1?x=y"),
        ("HOME_PLATFORM_APP_API_URL", "http://core:999999/api/apps/v1"),
        ("HOME_PLATFORM_APP_TIMEZONE", "not-a-zone"),
    ],
)
def test_invalid_instance_configuration_is_refused(tmp_path, monkeypatch, name, value):
    key = tmp_path / "key"
    key.write_bytes(b"c" * 32)
    monkeypatch.setenv("HOME_PLATFORM_ENVIRONMENT", "acceptance")
    monkeypatch.setenv("HOME_PLATFORM_APP_CONTEXT_KEY_FILE", str(key))
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        load_app_platform("admin-key")


@pytest.mark.parametrize(
    "path", ["relative", "/a/../b", "/a/./b", "/a,b", "/a\nsecret"]
)
def test_managed_paths_are_strict_host_paths(path):
    value = managed(state=path)
    with pytest.raises(ContractError):
        parse_document(Registration, document(value))


def test_managed_installation_requires_state_v1_probe_and_reserved_mount_name():
    for change in ("state", "probe", "secret"):
        value = managed()
        if change == "state":
            value["bindings"]["state_directory"] = None
        elif change == "probe":
            value["installation"]["probe"]["readiness"] = "legacy-json"
        else:
            value["bindings"]["secret_files"][0]["name"] = "app-credential"
        with pytest.raises(ContractError):
            parse_document(Registration, document(value))


def test_credentials_are_hashed_explicitly_rotated_and_old_files_survive(
    platform, tmp_path
):
    previous = platform.registry.active_credential("notes")
    token = previous.file_path.read_text().strip()
    assert previous.file_path.stat().st_mode & 0o777 == 0o600
    new_path = tmp_path / "credentials" / "rotated"
    with pytest.raises(ContractError, match="--rotate"):
        platform.registry.issue_credential("notes", new_path)
    assert platform.registry.verify_credential(token) is not None
    platform.registry.issue_credential("notes", new_path, rotate=True)
    assert platform.registry.verify_credential(token) is None
    assert previous.file_path.read_text().strip() == token
    assert platform.registry.verify_credential(new_path.read_text().strip()) is not None
    with platform.database.connect() as connection:
        rows = [
            dict(row) for row in connection.execute("SELECT * FROM app_credentials")
        ]
    assert token not in json.dumps(rows)
    assert (
        sum(row["revoked_at"] is None and row["app_id"] == "notes" for row in rows) == 1
    )


def test_rotation_file_failure_retains_old_active_credential(
    platform, tmp_path, monkeypatch
):
    token = bearer(platform)[7:]

    def fail(*args):
        raise OSError("private filesystem error")

    monkeypatch.setattr(os, "link", fail)
    with pytest.raises(OSError):
        platform.registry.issue_credential(
            "notes", tmp_path / "new-credential", rotate=True
        )
    assert platform.registry.verify_credential(token) is not None
    assert bearer(platform)[7:] == token


def test_rotation_database_failure_retains_old_credential_and_file(platform, tmp_path):
    token = bearer(platform)[7:]
    with platform.database.connect() as connection:
        connection.execute(
            "CREATE TRIGGER fail_issue BEFORE INSERT ON app_credentials "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    path = tmp_path / "failed-credential"
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        platform.registry.issue_credential("notes", path, rotate=True)
    assert platform.registry.verify_credential(token) is not None
    assert bearer(platform)[7:] == token
    assert not path.exists()


def test_credential_cannot_replace_files_or_live_under_writable_state(
    platform, tmp_path
):
    active = platform.registry.active_credential("notes")
    with pytest.raises(ContractError, match="fresh"):
        platform.registry.issue_credential("notes", active.file_path, rotate=True)
    for app_id, directory in (("notes", "state"), ("notes", "admin-state")):
        with pytest.raises(ContractError, match="writable"):
            platform.registry.issue_credential(
                app_id, tmp_path / directory / "credential", rotate=True
            )
    link = tmp_path / "state-alias"
    (tmp_path / "state").mkdir()
    link.symlink_to(tmp_path / "state")
    with pytest.raises(ContractError, match="writable"):
        platform.registry.issue_credential("notes", link / "credential", rotate=True)


def test_assertions_are_bound_to_app_environment_and_live_account(platform):
    token = assertion(platform)
    context = platform.identity.verify(bearer(platform), token)
    assert isinstance(context, AppContext)
    assert context.user.id == platform.user.id
    assert context.administrator is None
    for credential, claims in (
        (bearer(platform, "admin-notes"), token),
        (bearer(platform), resign(platform, token, env="staging")),
        (bearer(platform), token + "forged"),
    ):
        assert platform.identity.verify(credential, claims).status == 401
    platform.accounts.set_disabled(platform.user.id, True)
    assert platform.identity.verify(bearer(platform), token).status == 403
    platform.accounts.set_disabled(platform.user.id, False)
    platform.accounts.unlink_external_identity(platform.user.id, "tailscale")
    assert platform.identity.verify(bearer(platform), token).status == 403
    other = platform.accounts.create(
        UserCreate(username="other", password="a long test password")
    )
    platform.accounts.link_external_identity(
        other.id, "tailscale", "alice@example.test", "Alice"
    )
    assert platform.identity.verify(bearer(platform), token).status == 403


@pytest.mark.parametrize(
    "age,accepted", [(119, True), (120, False), (121, False), (-30, True), (-31, False)]
)
def test_assertion_exact_expiry_and_future_skew(platform, age, accepted):
    token = assertion(platform)
    platform.clock[0] += age
    result = platform.identity.verify(bearer(platform), token)
    assert isinstance(result, AppContext) == accepted


@pytest.mark.parametrize(
    "changes",
    [
        {"iat": True},
        {"exp": "1120"},
        {"exp": 1121},
        {"exp": 1119},
        {"iat": 1000.0},
        {"provider": "other"},
        {"unknown": "x"},
    ],
)
def test_assertion_claims_are_strict(platform, changes):
    token = resign(platform, assertion(platform), **changes)
    assert platform.identity.verify(bearer(platform), token).status == 401


def test_staging_credential_inserted_into_production_database_is_refused(platform):
    token = bearer(platform)[7:]
    with platform.database.connect() as connection:
        connection.execute(
            "UPDATE app_credentials SET environment = 'staging' WHERE app_id = 'notes'"
        )
    assert (
        platform.identity.verify("Bearer " + token, assertion(platform)).status == 401
    )


def test_missing_untrusted_unlinked_and_disabled_gateway_identity(platform):
    app = platform.registry.managed_app("notes")
    for incoming, status in (
        (request(scheme="http"), 401),
        (request(login=None), 401),
        (request(login="unknown@example.test"), 403),
    ):
        assert platform.identity.admit(app, incoming).status == status
    platform.accounts.set_disabled(platform.user.id, True)
    assert platform.identity.admit(app, request()).status == 403
    unavailable = AppIdentity(platform.accounts, platform.registry, None)
    assert unavailable.admit(app, request()).status == 503
    assert unavailable.verify(None, None).status == 503


def test_admin_assertion_keeps_linked_subject_and_live_revocation(platform):
    admin = platform.accounts.get_identity(UUID(ADMIN_USER_ID))
    version = platform.accounts.session_version(admin.id)
    cookie = encode_session(admin, "admin-key", int(time.time()) + 1000, version)
    token = assertion(platform, "admin-notes", cookie)
    result = platform.identity.verify(bearer(platform, "admin-notes"), token)
    assert result.user.id == platform.user.id
    assert result.administrator.account_id == admin.id
    plain = resign(
        platform,
        assertion(platform),
        admin={"account": str(admin.id), "session_version": version},
    )
    assert platform.identity.verify(bearer(platform), plain).status == 403
    platform.accounts.revoke_sessions(admin.id, version)
    platform.accounts.revoke_sessions(admin.id, version)
    assert platform.accounts.session_version(admin.id) == version + 1
    assert (
        platform.identity.verify(bearer(platform, "admin-notes"), token).status == 403
    )
    assert (
        platform.identity.admit(
            platform.registry.managed_app("admin-notes"), request(cookie=cookie)
        ).status
        == 401
    )


@pytest.mark.parametrize("identity_kind,status", [("missing", 401), ("member", 403)])
def test_admin_gateway_refuses_missing_or_member_cookie(
    platform, identity_kind, status
):
    cookie = (
        None
        if identity_kind == "missing"
        else encode_session(platform.user, "admin-key", int(time.time()) + 1000)
    )
    assert (
        platform.identity.admit(
            platform.registry.managed_app("admin-notes"), request(cookie=cookie)
        ).status
        == status
    )


def test_logout_revokes_admin_and_member_sessions_and_replay_is_idempotent(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "admin-key")
    application = create_app(tmp_path / "logout.db")
    with TestClient(application) as client:
        accounts = application.state.account_store
        member = accounts.create(
            UserCreate(username="alice", password="a long test password")
        )
        for credentials, user_id in (
            ({"token": "admin-key"}, UUID(ADMIN_USER_ID)),
            ({"username": "alice", "password": "a long test password"}, member.id),
        ):
            version = accounts.session_version(user_id)
            assert client.post("/dashboard/login", json=credentials).status_code == 204
            cookie = client.cookies.get(SESSION_COOKIE)
            assert client.get("/jobs-ui/api/session").status_code == 200
            assert client.post("/dashboard/logout").status_code == 204
            assert accounts.session_version(user_id) == version + 1
            assert (
                client.get(
                    "/jobs-ui/api/session",
                    headers={"Cookie": SESSION_COOKIE + "=" + cookie},
                ).status_code
                == 401
            )
            assert (
                client.post(
                    "/dashboard/logout",
                    headers={"Cookie": SESSION_COOKIE + "=" + cookie},
                ).status_code
                == 204
            )
            assert accounts.session_version(user_id) == version + 1
            assert client.post("/dashboard/login", json=credentials).status_code == 204
            assert client.get("/jobs-ui/api/session").status_code == 200


@pytest.mark.parametrize(
    "cookie",
    [
        "home_platform_dashboard=bad; Path=/apps/notes",
        "__Host-home_platform_other=bad; Path=/apps/notes",
        "app=ok",
        "app=ok; Domain=platform.test; Path=/apps/notes",
        "app=ok; Path=/apps/notes-neighbor",
        "app=ok; Path=/apps/notes/../other",
        "app=ok; Path=/apps/notes/%2e%2e/other",
        "app=ok; Path=/apps/notes?x=y",
        "app=ok; Path=/apps/notes; Path=/",
        "app=ok; DOMAIN; Path=/apps/notes",
        "app=ok, other=bad; Path=/apps/notes",
    ],
)
def test_cookie_boundary_refuses_unsafe_values(cookie):
    assert cookie_violation(cookie, "/apps/notes") is not None


def test_header_cookie_location_and_multi_cookie_boundaries(platform):
    headers = [
        (
            b"cookie",
            b"home_platform_dashboard=secret; app_session=ok; "
            b"__Secure-home_platform_other=secret",
        ),
        (b"x-platform-user-context", b"forged"),
        (b"x-api-token", b"secret"),
        (b"x-service-identity-token", b"secret"),
        (b"tailscale-user-login", b"forged"),
        (b"forwarded", b"forged"),
        (b"x-forwarded-proto", b"forged"),
        (b"x-real-ip", b"forged"),
        (b"connection", b"custom-hop"),
        (b"custom-hop", b"forged"),
        (b"content-type", b"application/json"),
    ]
    filtered = forward_headers(headers, "trusted-context")
    assert filtered == [
        (b"content-type", b"application/json"),
        (b"cookie", b"app_session=ok"),
        (b"x-platform-user-context", b"trusted-context"),
    ]
    app = platform.registry.managed_app("notes")
    inside = [
        ("Set-Cookie", "one=a; Path=/apps/notes"),
        ("Set-Cookie", "two=b; Path=/apps/notes/child; HttpOnly"),
    ]
    assert screen_response(inside, app) == inside
    assert isinstance(
        screen_response([("Clear-Site-Data", '"cookies"')], app), UpstreamFailure
    )
    origin = f"http://127.0.0.1:{app.installation.host_port}"
    assert rewrite_location(origin + "/landed?q=x", app) == "/apps/notes/landed?q=x"
    assert (
        rewrite_location("https://elsewhere.test/landed", app)
        == "https://elsewhere.test/landed"
    )


@pytest.mark.parametrize(
    "raw",
    [
        b"/apps/%6eotes/file",
        b"/apps/notes/%2felsewhere",
        b"/apps/notes/%2e%2e/x",
        b"/apps/notes/../x",
        b"/apps/notes/%5cx",
        b"/apps/notes/%00x",
    ],
)
def test_gateway_path_ambiguity_is_refused(raw):
    assert (
        internal_path({"path": "/apps/notes/file", "raw_path": raw}, "/apps/notes")
        is None
    )


def test_upstream_client_does_not_share_app_cookies(tmp_path):
    async def exercise():
        received = []

        async def handler(request):
            received.append(request.headers.get("cookie"))
            return httpx.Response(200, headers={"Set-Cookie": "private=one; Path=/"})

        async with create_upstream_client() as client:
            transport = httpx.MockTransport(handler)
            client._transport = transport
            await asyncio.gather(
                *(
                    client.get(
                        "http://127.0.0.1:1234/", headers={"Cookie": f"person={index}"}
                    )
                    for index in range(5)
                )
            )
            await client.get("http://127.0.0.1:1234/")
            assert received[-1] is None
            assert not list(client.cookies.jar)

    asyncio.run(exercise())


def test_runtime_hardening_and_environment_contract(platform):
    app = platform.registry.managed_app("notes")
    spec = runtime_spec(
        app,
        platform.registry.active_credential("notes"),
        platform.settings.instance,
        8080,
    )
    assert {name for name, _ in spec.environment[:6]} == PLATFORM_VARIABLES
    environment = dict(spec.environment)
    assert environment["APP_CREDENTIAL_FILE"] == "/run/secrets/app-credential"
    assert environment["APP_STATE_DIR"] == "/data"
    assert environment["APP_SECRET_PROVIDER_KEY_FILE"] == "/run/secrets/provider-key"
    assert [mount.read_only for mount in spec.mounts] == [False, True, True]
    argv = docker_run_argv(spec, "fixture:acceptance", (501, 20))
    assert argv[argv.index("--publish") + 1] == "127.0.0.1:19001:8080"
    assert (
        "--read-only" in argv and "--cap-drop" in argv and "no-new-privileges" in argv
    )
    assert bearer(platform)[7:] not in " ".join(argv)


def test_credential_and_runtime_cli_need_only_the_database(platform):
    args = [sys.executable, "-m", "ops.apps", "--database", str(platform.database.path)]
    refused = subprocess.run(
        [*args, "credential", "notes"], capture_output=True, text=True
    )
    assert refused.returncode == 2 and "--rotate" in refused.stderr
    rotated = subprocess.run(
        [*args, "credential", "notes", "--rotate"], capture_output=True, text=True
    )
    assert rotated.returncode == 0
    receipt = json.loads(rotated.stdout)
    assert receipt["outcome"] == "rotated"
    command = subprocess.run(
        [*args, "runtime", "notes", "--image", "fixture:acceptance", "--port", "8080"],
        capture_output=True,
        text=True,
    )
    assert command.returncode == 0
    assert str(platform.registry.active_credential("notes").file_path) in command.stdout
    assert bearer(platform)[7:] not in rotated.stdout + command.stdout


def test_credential_cli_writes_chosen_file_or_database_default(platform):
    database_dir = platform.database.path.parent
    args = [sys.executable, "-m", "ops.apps", "--database", str(platform.database.path)]
    chosen = database_dir / "policy-root" / "notes-credential"
    explicit = subprocess.run(
        [*args, "credential", "notes", "--rotate", "--file", str(chosen)],
        capture_output=True,
        text=True,
    )
    assert explicit.returncode == 0, explicit.stderr
    assert json.loads(explicit.stdout)["file_path"] == str(chosen)
    assert chosen.stat().st_mode & 0o777 == 0o600
    assert platform.registry.active_credential("notes").file_path == chosen
    default = subprocess.run(
        [*args, "credential", "admin-notes", "--rotate"],
        capture_output=True,
        text=True,
    )
    assert default.returncode == 0, default.stderr
    written = Path(json.loads(default.stdout)["file_path"])
    assert written.parent == database_dir.resolve() / "app-credentials"
    assert written.name.startswith("admin-notes-")
    assert written.stat().st_mode & 0o777 == 0o600


def test_migration_18_rebuilds_old_rowid_ledger_without_changing_retained_records(
    tmp_path, monkeypatch
):
    database = Database(tmp_path / "old17.db")
    current = migrations.MIGRATIONS
    with monkeypatch.context() as old:
        old.setattr(migrations, "MIGRATIONS", current[:17])
        database.initialize()
    registry = AppRegistry(database)
    registry.register(document(registration()))
    registry.ingest_release("notes", document(manifest()))
    with database.connect() as connection:
        before = [
            tuple(row) for row in connection.execute("SELECT * FROM app_releases")
        ]
        for name in ("replace", "update", "delete"):
            connection.execute(f"DROP TRIGGER app_releases_no_{name}")
        connection.execute("ALTER TABLE app_releases RENAME TO new_ledger")
        connection.execute(
            "CREATE TABLE app_releases "
            "(app_id TEXT NOT NULL REFERENCES app_registrations(app_id), "
            "manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64), "
            "manifest_bytes BLOB NOT NULL, "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "PRIMARY KEY(app_id,manifest_sha256))"
        )
        connection.execute("INSERT INTO app_releases SELECT * FROM new_ledger")
        connection.execute("DROP TABLE new_ledger")
        for name, action in (
            ("replace", "INSERT"),
            ("update", "UPDATE"),
            ("delete", "DELETE"),
        ):
            connection.execute(
                f"CREATE TRIGGER app_releases_no_{name} "
                f"BEFORE {action} ON app_releases "
                "BEGIN SELECT RAISE(ABORT, 'immutable'); END"
            )
    database.initialize()
    with database.connect() as connection:
        assert before == [
            tuple(row) for row in connection.execute("SELECT * FROM app_releases")
        ]
        for sql in (
            "UPDATE app_releases SET manifest_bytes = 'bad'",
            "DELETE FROM app_releases",
            "INSERT OR REPLACE INTO app_releases SELECT * FROM app_releases",
            "INSERT OR REPLACE INTO app_releases "
            "(rowid, app_id,manifest_sha256,manifest_bytes) VALUES (1,'notes','"
            + "f" * 64
            + "',X'00')",
        ):
            with pytest.raises(sqlite3.Error):
                connection.execute(sql)
    database.close()


class ResponseStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


def configured_app(tmp_path, monkeypatch):
    key = tmp_path / "context-key"
    key.write_bytes(b"c" * 32)
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "admin-key")
    monkeypatch.setenv("HOME_PLATFORM_ENVIRONMENT", "acceptance")
    monkeypatch.setenv("HOME_PLATFORM_APP_CONTEXT_KEY_FILE", str(key))
    return create_app(tmp_path / "gateway.db")


def test_gateway_asgi_forwards_body_query_and_live_context_and_passes_legacy(
    tmp_path, monkeypatch
):
    application = configured_app(tmp_path, monkeypatch)
    with TestClient(application, base_url="https://platform.test") as browser:
        registry = application.state.app_registry
        accounts = application.state.account_store
        registry.register(document(managed(state=str(tmp_path / "state"))))
        registry.issue_credential("notes", tmp_path / "credential")
        user = accounts.create(
            UserCreate(username="alice", password="a long test password")
        )
        accounts.link_external_identity(
            user.id, "tailscale", "alice@example.test", "Alice"
        )
        captured = []
        streams = []

        async def upstream(incoming):
            captured.append(incoming)
            body = await incoming.aread()
            verified = application.state.app_identity.verify(
                "Bearer " + (tmp_path / "credential").read_text().strip(),
                incoming.headers["X-Platform-User-Context"],
            )
            assert isinstance(verified, AppContext) and verified.user.id == user.id
            stream = ResponseStream([body or b"first", b"second"])
            streams.append(stream)
            return httpx.Response(
                200,
                headers=[
                    ("Set-Cookie", "app=one; Path=/apps/notes"),
                    ("Set-Cookie", "other=two; Path=/apps/notes"),
                ],
                stream=stream,
            )

        application.state.gateway_client._transport = httpx.MockTransport(upstream)
        headers = {
            "Tailscale-User-Login": "alice@example.test",
            "X-Platform-User-Context": "forged",
            "X-API-Token": "secret",
            "Cookie": "home_platform_dashboard=secret; app=allowed",
        }
        response = browser.post(
            "/apps/notes/echo?x=%2F&x=two", content=b"body", headers=headers
        )
        assert response.status_code == 200 and response.content == b"bodysecond"
        assert len(response.headers.get_list("Set-Cookie")) == 2
        assert captured[0].url.raw_path == b"/echo?x=%2F&x=two"
        assert captured[0].method == "POST"
        assert captured[0].headers["Cookie"] == "app=allowed"
        assert (
            "X-API-Token" not in captured[0].headers
            and "Tailscale-User-Login" not in captured[0].headers
        )
        assert streams[0].closed
        response = browser.get(
            "/apps/notes?x=yes", headers=headers, follow_redirects=False
        )
        assert (
            response.status_code == 308
            and response.headers["Location"] == "/apps/notes/?x=yes"
        )
        assert browser.get("/apps/notes/echo").status_code == 401
        assert browser.get("/habits/").status_code == 404
        assert (
            browser.get("/apps/notes/%2e%2e/echo", headers=headers).status_code == 400
        )
        assert len(captured) == 1
        context = captured[0].headers["X-Platform-User-Context"]
        call = {
            "Authorization": "Bearer " + (tmp_path / "credential").read_text().strip(),
            "X-Platform-User-Context": context,
        }
        response = browser.get("/api/apps/v1/user", headers=call)
        assert response.json() == {"id": str(user.id), "display_name": "alice"}
        assert response.headers["Cache-Control"] == "no-store"
        accounts.set_disabled(user.id, True)
        assert browser.get("/api/apps/v1/user", headers=call).status_code == 403


@pytest.mark.parametrize(
    "case,status", [("cookie", 502), ("clear", 502), ("timeout", 504), ("connect", 502)]
)
def test_gateway_asgi_refuses_bad_responses_and_upstream_errors(
    tmp_path, monkeypatch, case, status
):
    application = configured_app(tmp_path, monkeypatch)
    with TestClient(application, base_url="https://platform.test") as browser:
        registry = application.state.app_registry
        registry.register(document(managed(state=str(tmp_path / "state"))))
        accounts = application.state.account_store
        user = accounts.create(
            UserCreate(username="alice", password="a long test password")
        )
        accounts.link_external_identity(
            user.id, "tailscale", "alice@example.test", "Alice"
        )
        stream = ResponseStream([b"must never reach browser"])

        async def upstream(incoming):
            if case == "timeout":
                raise httpx.ReadTimeout("private upstream error", request=incoming)
            if case == "connect":
                raise httpx.ConnectError("private upstream error", request=incoming)
            headers = (
                {"Set-Cookie": "home_platform_dashboard=bad; Path=/"}
                if case == "cookie"
                else {"Clear-Site-Data": '"cookies"'}
            )
            return httpx.Response(200, headers=headers, stream=stream)

        application.state.gateway_client._transport = httpx.MockTransport(upstream)
        response = browser.get(
            "/apps/notes/", headers={"Tailscale-User-Login": "alice@example.test"}
        )
        assert response.status_code == status
        assert (
            "private upstream" not in response.text
            and "must never" not in response.text
        )
        if case in {"cookie", "clear"}:
            assert stream.closed


def test_managed_websocket_is_closed_without_forwarding(tmp_path, monkeypatch):
    application = configured_app(tmp_path, monkeypatch)
    with TestClient(application) as browser:
        application.state.app_registry.register(
            document(managed(state=str(tmp_path / "state")))
        )
        from starlette.websockets import WebSocketDisconnect

        with (
            pytest.raises(WebSocketDisconnect) as failure,
            browser.websocket_connect("/apps/notes/ws"),
        ):
            pytest.fail("Managed WebSocket was accepted")
        assert failure.value.code == 1008


def test_migration_18_failure_rolls_back_and_retries(tmp_path, monkeypatch):
    database = Database(tmp_path / "migration18.db")
    current = migrations.MIGRATIONS
    with monkeypatch.context() as old:
        old.setattr(migrations, "MIGRATIONS", current[:17])
        database.initialize()

    class FailCredentials:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args):
            if "CREATE TABLE app_credentials" in sql:
                raise sqlite3.OperationalError("injected failure")
            return self.connection.execute(sql, *args)

    def fail(connection):
        migrations.add_app_runtime_identity(FailCredentials(connection))

    with monkeypatch.context() as failure:
        failure.setattr(migrations, "MIGRATIONS", (*current[:17], (18, fail)))
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            database.initialize()
    with database.connect() as connection:
        assert (
            connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[
                0
            ]
            == 17
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'platform_instance'"
            ).fetchone()
            is None
        )
    database.initialize()
    with database.connect() as connection:
        assert (
            connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[
                0
            ]
            == migrations.MIGRATIONS[-1][0]
        )
    database.close()


@pytest.mark.parametrize(
    "cookie",
    ['app="quoted"; Path=/apps/notes', 'app=""; Path=/apps/notes'],
)
def test_cookie_boundary_accepts_rfc6265_quoted_values(cookie):
    assert cookie_violation(cookie, "/apps/notes") is None


@pytest.mark.parametrize(
    "cookie",
    [
        'app="a b"; Path=/apps/notes',
        'app="open; Path=/apps/notes',
        'app=close"; Path=/apps/notes',
        'app="; Path=/apps/notes',
        'app=""x""; Path=/apps/notes',
        'app="a\\b"; Path=/apps/notes',
    ],
)
def test_cookie_boundary_refuses_malformed_quoted_values(cookie):
    assert cookie_violation(cookie, "/apps/notes") is not None


def test_response_screen_strips_service_worker_scope(platform):
    app = platform.registry.managed_app("notes")
    assert screen_response(
        [("Service-Worker-Allowed", "/"), ("Content-Type", "text/javascript")], app
    ) == [("Content-Type", "text/javascript")]


def test_gateway_preserves_request_framing_over_a_real_connection(platform):
    async def exchange(method, headers, body, *, connects=True):
        received = []
        handled = asyncio.Event()

        async def upstream(reader, writer):
            try:
                head = (await reader.readuntil(b"\r\n\r\n")).lower()
                length = next(
                    (
                        int(line.split(b":", 1)[1])
                        for line in head.split(b"\r\n")
                        if line.startswith(b"content-length:")
                    ),
                    0,
                )
                try:
                    if b"transfer-encoding: chunked" in head:
                        payload = await reader.readuntil(b"0\r\n\r\n")
                    else:
                        payload = await reader.readexactly(length)
                except asyncio.IncompleteReadError as error:
                    received.append((head, error.partial, False))
                else:
                    received.append((head, payload, True))
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                    await writer.drain()
                writer.close()
            finally:
                handled.set()

        server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        app = platform.registry.managed_app("notes")
        app = replace(
            app, installation=app.installation.model_copy(update={"host_port": port})
        )
        messages = [{"type": "http.request", "body": body, "more_body": False}]
        sent = []

        async def receive():
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "method": method,
            "path": "/apps/notes/x",
            "query_string": b"",
            "headers": headers,
        }
        client = create_upstream_client()
        try:
            await proxy(
                client,
                Request(scope, receive),
                Forward(app, b"/x", "context"),
                receive,
                send,
            )
            if connects:
                async with asyncio.timeout(5):
                    await handled.wait()
        finally:
            await client.aclose()
            server.close()
        return sent[0]["status"], received

    status, [(head, body, complete)] = asyncio.run(
        exchange("POST", [(b"content-length", b"5")], b"hello")
    )
    assert status == 200 and complete
    assert b"content-length: 5" in head and b"transfer-encoding" not in head
    assert body == b"hello"
    status, [(head, body, complete)] = asyncio.run(
        exchange("POST", [(b"transfer-encoding", b"chunked")], b"hello")
    )
    assert status == 200 and complete
    assert b"transfer-encoding: chunked" in head and b"content-length" not in head
    assert body == b"5\r\nhello\r\n0\r\n\r\n"
    status, [(head, body, complete)] = asyncio.run(exchange("GET", [], b""))
    assert status == 200 and complete
    assert b"transfer-encoding" not in head and b"content-length" not in head
    both = [(b"content-length", b"5"), (b"transfer-encoding", b"chunked")]
    status, [(head, body, complete)] = asyncio.run(exchange("POST", both, b"hello"))
    assert status == 200 and complete
    assert b"transfer-encoding: chunked" in head and b"content-length" not in head
    assert asyncio.run(
        exchange("POST", [(b"content-length", b"abc")], b"hello", connects=False)
    ) == (400, [])
    # The gateway streams the body, so a mismatch surfaces mid-request. The
    # upstream must see the connection abort before the declared length, never
    # a complete request carrying the wrong body.
    status, [(head, body, complete)] = asyncio.run(
        exchange("POST", [(b"content-length", b"5")], b"A" * 100)
    )
    assert status == 400 and not complete and body == b""
    status, [(head, body, complete)] = asyncio.run(
        exchange("POST", [(b"content-length", b"5")], b"hi")
    )
    assert status == 400 and not complete and body == b"hi"


def test_gateway_drops_a_stale_content_length_from_a_chunked_upstream_response(
    platform,
):
    async def upstream(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n"
            b"Content-Type: text/plain\r\n\r\n5\r\nhello\r\n0\r\n\r\n"
        )
        await writer.drain()
        writer.close()

    async def exchange():
        server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        app = platform.registry.managed_app("notes")
        app = replace(
            app, installation=app.installation.model_copy(update={"host_port": port})
        )
        sent = []

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/apps/notes/x",
            "query_string": b"",
            "headers": [],
        }
        client = create_upstream_client()
        try:
            await proxy(
                client,
                Request(scope, receive),
                Forward(app, b"/x", "context"),
                receive,
                send,
            )
        finally:
            await client.aclose()
            server.close()
        return sent

    sent = asyncio.run(exchange())
    names = {name for name, _ in sent[0]["headers"]}
    assert b"content-length" not in names and b"transfer-encoding" not in names
    assert b"content-type" in names
    assert b"".join(m.get("body", b"") for m in sent[1:]) == b"hello"


def test_gateway_forwards_content_length_to_an_upstream_that_requires_it(
    tmp_path, monkeypatch
):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen = []

    class RequiresLength(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            if "Content-Length" not in self.headers:
                self.send_response(411)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append((self.headers.get("Transfer-Encoding"), body))
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), RequiresLength)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        application = configured_app(tmp_path, monkeypatch)
        with TestClient(application, base_url="https://platform.test") as browser:
            registry = application.state.app_registry
            accounts = application.state.account_store
            value = managed(state=str(tmp_path / "state"))
            value["installation"]["host_port"] = server.server_address[1]
            registry.register(document(value))
            registry.issue_credential("notes", tmp_path / "credential")
            user = accounts.create(
                UserCreate(username="alice", password="a long test password")
            )
            accounts.link_external_identity(
                user.id, "tailscale", "alice@example.test", "Alice"
            )
            headers = {"Tailscale-User-Login": "alice@example.test"}
            response = browser.post(
                "/apps/notes/items", content=b"hello", headers=headers
            )
            assert (response.status_code, response.content) == (200, b"hello")
            assert seen == [(None, b"hello")]
            response = browser.post(
                "/apps/notes/items",
                content=b"A" * 100,
                headers={**headers, "Content-Length": "5"},
            )
            assert response.status_code == 400
            assert all(b"A" not in body for _, body in seen)
    finally:
        server.shutdown()
        server.server_close()


def test_gateway_identity_database_failure_is_unavailable(tmp_path, monkeypatch):
    application = configured_app(tmp_path, monkeypatch)
    with TestClient(application, base_url="https://platform.test") as browser:
        application.state.app_registry.register(
            document(managed(state=str(tmp_path / "state")))
        )

        def broken(*args):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(
            application.state.app_identity.accounts,
            "resolve_external_identity",
            broken,
        )
        response = browser.get(
            "/apps/notes/", headers={"Tailscale-User-Login": "alice@example.test"}
        )
        assert response.status_code == 503
        assert response.headers["Cache-Control"] == "no-store"


def test_gateway_handles_a_client_that_disconnects_while_uploading(platform):
    app = platform.registry.managed_app("notes")
    messages = [{"type": "http.request", "body": b"A" * 10, "more_body": True}]
    sent = []

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    async def upstream(incoming):
        await incoming.aread()
        return httpx.Response(200)

    async def exchange():
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/apps/notes/x",
            "query_string": b"",
            "headers": [(b"transfer-encoding", b"chunked")],
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            await proxy(
                client,
                Request(scope, receive),
                Forward(app, b"/x", "context"),
                receive,
                send,
            )

    asyncio.run(exchange())
    assert sent == []

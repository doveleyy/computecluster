import io

import httpx
import pytest
import starlette.formparsers
from fastapi.testclient import TestClient
from test_app_platform import ResponseStream, configured_app, managed
from test_app_registry import document

from app.accounts import UserCreate
from app.main import create_app
from app.request_guards import MULTIPART_ENVELOPE_BYTES, upload_limit

ORIGIN = "https://home-platform.tailnet.ts.net"
SIBLING = "https://preview.tailnet.ts.net"


@pytest.fixture
def parsed(monkeypatch):
    """Bytes the multipart parser spooled, counted at the parser itself."""
    received: list[int] = []

    class CountingFile(io.BytesIO):
        _rolled = False
        _max_size = 1024 * 1024

        def write(self, data):
            received.append(len(data))
            return super().write(data)

    monkeypatch.setattr(
        starlette.formparsers, "SpooledTemporaryFile", lambda **_: CountingFile()
    )
    return received


def admin_workspace(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "admin-key")
    monkeypatch.setenv("HOME_PLATFORM_APPS_ORIGIN", ORIGIN)
    monkeypatch.setenv("HOME_PLATFORM_STORAGE_DIR", str(storage))
    monkeypatch.setenv("HOME_PLATFORM_WORKSPACE_DIR", str(storage))
    return storage


def test_sibling_tailnet_page_cannot_write_through_the_administrator_cookie(
    tmp_path, monkeypatch
):
    storage = admin_workspace(tmp_path, monkeypatch)
    with TestClient(create_app(tmp_path / "jobs.db"), base_url=ORIGIN) as client:
        client.post("/dashboard/login", json={"token": "admin-key"})
        member = client.post(
            "/dashboard/api/users",
            json={"username": "alice", "password": "member password 1234"},
        ).json()["id"]
        workspace = storage / "users" / member / "Workspace"
        workspace.mkdir(parents=True)

        def upload(name, headers):
            return client.post(
                "/jobs-ui/api/workspace/uploads",
                data={"directory": f"Storage/users/{member}/Workspace"},
                files={"file": (name, b"content")},
                headers=headers,
            ).status_code

        assert upload("planted.sh", {"Origin": SIBLING}) == 403
        assert upload("opaque.sh", {"Origin": "null"}) == 403
        assert upload("own.txt", {"Origin": ORIGIN}) == 201
        assert upload("script.txt", {}) == 201
        assert (
            client.post("/dashboard/logout", headers={"Origin": SIBLING}).status_code
            == 403
        )
    assert sorted(path.name for path in workspace.iterdir()) == [
        "own.txt",
        "script.txt",
    ]


def test_cross_origin_write_never_reaches_a_managed_app(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME_PLATFORM_APPS_ORIGIN", ORIGIN)
    application = configured_app(tmp_path, monkeypatch)
    with TestClient(application, base_url=ORIGIN) as browser:
        application.state.app_registry.register(
            document(managed(state=str(tmp_path / "state")))
        )
        application.state.app_registry.issue_credential(
            "notes", tmp_path / "credential"
        )
        accounts = application.state.account_store
        user = accounts.create(
            UserCreate(username="alice", password="a long test password")
        )
        accounts.link_external_identity(
            user.id, "tailscale", "alice@example.test", "Alice"
        )
        forwarded = []

        async def upstream(request):
            forwarded.append(request.method)
            return httpx.Response(200, stream=ResponseStream([b"ok"]))

        application.state.gateway_client._transport = httpx.MockTransport(upstream)
        identity = {"Tailscale-User-Login": "alice@example.test"}
        refused = browser.post(
            "/apps/notes/api/refresh",
            content=b"",
            headers={**identity, "Origin": SIBLING},
        )
        read = browser.get("/apps/notes/page", headers={**identity, "Origin": SIBLING})
        own = browser.post(
            "/apps/notes/api/refresh",
            content=b"",
            headers={**identity, "Origin": ORIGIN},
        )
    assert (refused.status_code, read.status_code, own.status_code) == (403, 200, 200)
    assert forwarded == ["GET", "POST"]


def test_uploads_without_a_credential_are_refused_before_parsing(
    tmp_path, monkeypatch, parsed
):
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "admin-key")
    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        for path in ("/uploads/datasets", "/jobs-ui/api/uploads", "/jobs/x/artifacts"):
            response = client.post(
                path, files={"file": ("large.csv", b"x" * 2 * 1024**2, "text/csv")}
            )
            assert response.status_code == 401, path
        forged = client.post(
            "/uploads/datasets",
            files={"file": ("large.csv", b"x" * 1024, "text/csv")},
            headers={"X-API-Token": "wrong"},
            cookies={"home_platform_dashboard": "forged"},
        )
    assert forged.status_code == 401
    assert sum(parsed) == 0


def test_credentialed_uploads_are_cut_off_at_their_route_limit(
    tmp_path, monkeypatch, parsed
):
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "admin-key")
    monkeypatch.setenv("HOME_PLATFORM_MAX_UPLOAD_BYTES", "1024")
    token = {"X-API-Token": "admin-key"}
    oversized = b"x" * (MULTIPART_ENVELOPE_BYTES + 512 * 1024)

    def chunks():
        yield b"--b\r\nContent-Disposition: form-data; name=file; "
        yield b"filename=a.csv\r\n\r\n"
        for start in range(0, len(oversized), 65536):
            yield oversized[start : start + 65536]
        yield b"\r\n--b--\r\n"

    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        declared = client.post(
            "/uploads/datasets", files={"file": ("a.csv", oversized)}, headers=token
        )
        assert declared.status_code == 413 and sum(parsed) == 0
        streamed = client.post(
            "/uploads/datasets",
            content=chunks(),
            headers={**token, "Content-Type": "multipart/form-data; boundary=b"},
        )
        assert streamed.status_code == 413
        assert sum(parsed) <= MULTIPART_ENVELOPE_BYTES + 1024
        accepted = client.post(
            "/uploads/datasets", files={"file": ("a.csv", b"a,b\n1,2\n")}, headers=token
        )
    assert accepted.status_code == 201


def test_every_form_route_has_a_pre_parse_limit(tmp_path):
    schema = create_app(tmp_path / "jobs.db").openapi()
    forms = [
        path
        for path, operations in schema["paths"].items()
        for operation in operations.values()
        if {"multipart/form-data", "application/x-www-form-urlencoded"}
        & set(operation.get("requestBody", {}).get("content", {}))
    ]
    assert len(forms) == 10
    assert [path for path in forms if upload_limit(path) is None] == []


@pytest.mark.parametrize("scheme,secure", [("https", True), ("http", False)])
def test_session_cookie_is_secure_when_issued_over_https(
    tmp_path, monkeypatch, scheme, secure
):
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", "admin-key")
    with TestClient(
        create_app(tmp_path / "jobs.db"), base_url=f"{scheme}://platform.test"
    ) as client:
        client.post("/dashboard/login", json={"token": "admin-key"})
        (cookie,) = client.cookies.jar
    assert cookie.secure is secure

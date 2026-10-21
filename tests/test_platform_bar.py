"""The platform bar, the launcher, and the dashboard's member redirect all
render from the one app registry, so these tests pin what each role sees."""

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.app_registry import AppRegistry
from app.main import create_app

TOKEN = "test-secret"
MEMBER_PASSWORD = "member password 1234"
TAILNET = {"Tailscale-User-Login": "member@example.test", "Tailscale-User-Name": "M"}
ORIGIN = "https://testserver"
REPO = Path(__file__).resolve().parents[1]
# Every page frames the bar with this one tag, so no app can size, offset or
# restyle it; only the app key varies.
BAR_FRAME = (
    '<iframe class="platform-bar" src="/platform/bar?app={app}"'
    ' title="Home Platform apps" style="display:block;width:100%;height:41px;'
    'border:0;flex:none;background:#070707"></iframe>'
)


@contextmanager
def platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("HOME_PLATFORM_API_TOKEN", TOKEN)
    monkeypatch.setenv("HOME_PLATFORM_APPS_ORIGIN", ORIGIN)
    with TestClient(
        create_app(tmp_path / "jobs.db"), base_url="https://testserver"
    ) as client:
        assert client.post("/dashboard/login", json={"token": TOKEN}).status_code == 204
        created = client.post(
            "/dashboard/api/users",
            json={"username": "member", "password": MEMBER_PASSWORD, "role": "MEMBER"},
        )
        assert created.status_code == 201
        yield client


def sign_in_member(client: TestClient) -> None:
    client.post("/dashboard/logout")
    response = client.post(
        "/dashboard/login", json={"username": "member", "password": MEMBER_PASSWORD}
    )
    assert response.status_code == 204


def link_member(client: TestClient) -> None:
    """Sign in as the member and link the TAILNET login to that account."""
    sign_in_member(client)
    linked = client.post("/jobs-ui/api/account/identities/tailscale", headers=TAILNET)
    assert linked.status_code == 200


def pin(client: TestClient, *app_ids: str, tailnet: dict[str, str] = TAILNET) -> None:
    for app_id in app_ids:
        pinned = client.put(
            f"/platform/pins/{app_id}", headers={**tailnet, "Origin": ORIGIN}
        )
        assert pinned.status_code == 204, app_id


def register_sample_apps(registry: AppRegistry) -> None:
    for index, (app_id, label, access) in enumerate(
        (
            ("notes", "Notes", "linked-user"),
            ("calendar", "Calendar", "linked-user"),
            ("weather", "Weather", "linked-user"),
            ("toolbox", "Toolbox", "administrator"),
        )
    ):
        definition = {
            "app_id": app_id,
            "route": f"/{app_id}",
            "source": {"kind": "legacy"},
            "access": access,
            "capabilities": ["identity"],
            "presentation": {
                "label": label,
                "health_name": label,
                "position": 60 if access == "administrator" else (index + 1) * 10,
                "accent": "#abcdef",
            },
            "installation": {"owner": "legacy-scripts", "host_port": 19000 + index},
        }
        registry.register(json.dumps(definition).encode())


def bar_links(html: str) -> list[tuple[str, str, bool]]:
    """(href, label, current) for every link in the bar, in document order."""
    found = []
    for match in re.finditer(r'<a [^>]*href="([^"]+)"([^>]*)>(.*?)</a>', html):
        label = re.sub(r"<[^>]+>|&nbsp;", " ", match.group(3)).split()
        found.append(
            (match.group(1), " ".join(label), "aria-current" in match.group(2))
        )
    return found


def test_bar_shows_each_role_only_the_apps_it_can_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with platform(tmp_path, monkeypatch) as client:
        register_sample_apps(client.app.state.app_registry)
        link_member(client)
        pin(client, "notes", "calendar", "weather", "toolbox")
        member = client.get("/platform/bar", params={"app": "notes"}, headers=TAILNET)
        client.post("/dashboard/logout")
        anonymous = client.get(
            "/platform/bar", params={"app": "calendar"}, headers=TAILNET
        )
        assert client.post("/dashboard/login", json={"token": TOKEN}).status_code == 204
        admin = client.get("/platform/bar", params={"app": "toolbox"}, headers=TAILNET)

    assert admin.status_code == 200
    assert bar_links(admin.text) == [
        ("/", "Home Platform", False),
        ("/notes/", "Notes", False),
        ("/calendar/", "Calendar", False),
        ("/weather/", "Weather", False),
        ("/dashboard", "Dashboard", False),
        ("/toolbox/", "Toolbox", True),
    ]
    assert bar_links(member.text) == [
        ("/", "Home Platform", False),
        ("/notes/", "Notes", True),
        ("/calendar/", "Calendar", False),
        ("/weather/", "Weather", False),
        ("/jobs-ui", "Job Desk", False),
    ]
    assert [link[:2] for link in bar_links(anonymous.text)] == [
        link[:2] for link in bar_links(member.text)
    ]
    assert [link for link in bar_links(anonymous.text) if link[2]] == [
        ("/calendar/", "Calendar", True)
    ]
    for page in (admin, member, anonymous):
        assert '<base target="_top">' in page.text
        assert '<nav aria-label="Home Platform apps">' in page.text
        assert "<script" not in page.text
        assert page.headers["cache-control"] == "no-store"
        assert page.headers["content-security-policy"] == "frame-ancestors 'self'"
        assert page.headers["x-frame-options"] == "SAMEORIGIN"


def test_bar_rejects_an_app_outside_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with platform(tmp_path, monkeypatch) as client:
        unknown = client.get("/platform/bar", params={"app": "mailbox"})
        missing = client.get("/platform/bar")

    assert unknown.status_code == 400
    assert missing.status_code == 422


def test_new_registration_appears_on_the_next_browser_read_and_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    definition = {
        "app_id": "notes",
        "route": "/notes",
        "source": {"kind": "legacy"},
        "access": "linked-user",
        "capabilities": ["identity"],
        "presentation": {
            "label": "Notes <safe>",
            "health_name": "Notes",
            "position": 35,
            "accent": "#abcdef",
        },
        "installation": {"owner": "legacy-scripts", "host_port": 19000},
    }
    bar = {"url": "/platform/bar", "params": {"app": "notes"}, "headers": TAILNET}
    with platform(tmp_path, monkeypatch) as client:
        link_member(client)
        client.post("/dashboard/logout")
        assert client.get(**bar).status_code == 400
        client.app.state.app_registry.register(json.dumps(definition).encode())
        pin(client, "notes")
        current = client.get(**bar)
        assert current.status_code == 200
        assert ("/notes/", "Notes &lt;safe&gt;", True) in bar_links(current.text)
        assert '<a href="/notes/" aria-current="page"' in current.text
        assert "<safe>" not in current.text
    with TestClient(
        create_app(tmp_path / "jobs.db"), base_url="https://testserver"
    ) as client:
        assert client.get(**bar).text == current.text


def test_bar_divider_follows_every_shared_app_when_a_role_app_sorts_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    definition = {
        "app_id": "vault",
        "route": "/vault",
        "source": {"kind": "legacy"},
        "access": "administrator",
        "capabilities": ["identity"],
        "presentation": {
            "label": "Vault",
            "health_name": "Vault",
            "position": 15,
            "accent": "#abcdef",
        },
        "installation": {"owner": "legacy-scripts", "host_port": 19000},
    }
    with platform(tmp_path, monkeypatch) as client:
        register_sample_apps(client.app.state.app_registry)
        client.app.state.app_registry.register(json.dumps(definition).encode())
        link_member(client)
        pin(client, "notes", "calendar", "weather", "toolbox", "vault")
        client.post("/dashboard/logout")
        assert client.post("/dashboard/login", json={"token": TOKEN}).status_code == 204
        admin = client.get("/platform/bar", params={"app": "vault"}, headers=TAILNET)

    sequence = re.findall(r'<li class="sep"|href="([^"]+)"', admin.text)
    assert [item or "|" for item in sequence] == [
        "/",
        "|",
        "/notes/",
        "/calendar/",
        "/weather/",
        "|",
        "/vault/",
        "/dashboard",
        "/toolbox/",
    ]


def test_member_session_is_sent_from_dashboard_pages_to_job_desk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with platform(tmp_path, monkeypatch) as client:
        admin_pages = {
            path: client.get(path)
            for path in (
                "/dashboard",
                "/dashboard/operations",
                "/dashboard/jobs",
                "/dashboard/files",
            )
        }
        sign_in_member(client)
        member_pages = {
            path: client.get(path, follow_redirects=False) for path in admin_pages
        }
        client.post("/dashboard/logout")
        signed_out = client.get("/dashboard", follow_redirects=False)
        client.cookies.set("home_platform_dashboard", "not-a-session")
        forged = client.get("/dashboard", follow_redirects=False)

    for path, page in admin_pages.items():
        assert page.status_code == 200, path
        assert 'src="/platform/bar?app=dashboard"' in page.text, path
    assert {
        path: (page.status_code, page.headers["location"])
        for path, page in member_pages.items()
    } == {
        "/dashboard": (303, "/jobs-ui"),
        "/dashboard/operations": (303, "/jobs-ui"),
        "/dashboard/jobs": (303, "/jobs-ui"),
        "/dashboard/files": (303, "/jobs-ui/files"),
    }
    # Without a usable session the administrator still reaches the token form.
    assert signed_out.status_code == 200
    assert forged.status_code == 200
    assert 'href="/jobs-ui">Job Desk</a>' in signed_out.text


def test_job_desk_pages_mark_job_desk_as_the_current_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with platform(tmp_path, monkeypatch) as client:
        pages = [
            client.get(path) for path in ("/jobs-ui", "/jobs-ui/new", "/jobs-ui/files")
        ]

    for page in pages:
        assert page.status_code == 200
        assert 'src="/platform/bar?app=jobs"' in page.text
        assert "__PLATFORM_APP__" not in page.text


def test_launcher_shows_one_tile_per_app_the_role_can_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with platform(tmp_path, monkeypatch) as client:
        register_sample_apps(client.app.state.app_registry)
        sign_in_member(client)
        linked = client.post(
            "/jobs-ui/api/account/identities/tailscale", headers=TAILNET
        )
        member_home = client.get("/", headers=TAILNET)
        client.post("/dashboard/logout")
        assert client.post("/dashboard/login", json={"token": TOKEN}).status_code == 204
        admin_home = client.get("/", headers=TAILNET)
        client.post("/dashboard/logout")
        no_session_home = client.get("/", headers=TAILNET)

    assert linked.status_code in (200, 201)
    assert member_home.status_code == admin_home.status_code == 200

    def tile_hrefs(html: str) -> list[str]:
        return re.findall(r'<a class="tile" href="([^"]+)"', html)

    member_tiles = tile_hrefs(member_home.text)
    assert member_tiles == ["/notes/", "/calendar/", "/weather/", "/jobs-ui"]
    assert tile_hrefs(admin_home.text) == [
        "/notes/",
        "/calendar/",
        "/weather/",
        "/dashboard",
        "/toolbox/",
    ]
    assert tile_hrefs(no_session_home.text) == member_tiles

    for page in (member_home.text, admin_home.text):
        # The Job Desk tabs belong inside Job Desk, not on the launcher.
        assert "/jobs-ui/new" not in page
        assert "/jobs-ui/files" not in page
        assert page.count("<a ") == len(tile_hrefs(page))
        assert "__TILES__" not in page
        assert "__DISPLAY_NAME__" not in page
        assert '<canvas id="art" aria-hidden="true">' in page
        assert '<p class="art-alt">Decorative animation:' in page
        script = page[page.index("<script") :]
        # The one request the launcher makes is a pin change the viewer asks for.
        assert re.findall(r"fetch\(`([^$`]*)", script) == ["/platform/pins/"]
        for service_call in ("XMLHttpRequest", "EventSource"):
            assert service_call not in script
    assert "<strong>member</strong>" in member_home.text


def test_every_page_frames_the_bar_with_the_one_canonical_tag() -> None:
    canonical = {
        BAR_FRAME.format(app=key): key
        for key in {
            "home",
            "habits",
            "wishlist",
            "transport",
            "jobs",
            "dashboard",
            "sorter",
            "__PLATFORM_APP__",
        }
    }
    pages = {
        str(path.relative_to(REPO)): path.read_text(encoding="utf-8")
        for path in [*REPO.glob("app/*.html"), *REPO.glob("app/*.css")]
    }
    framed = {}
    for name, html in pages.items():
        tags = re.findall(r"<[^<>]*platform-bar[^<>]*>(?:</iframe>)?", html)
        stray = html.count("platform-bar") - len(tags)
        if tags or stray:
            framed[name] = [canonical.get(tag, tag) for tag in tags]
            framed[name] += ["platform-bar styled outside the tag"] * stray

    assert framed == {
        "app/apps.html": ["dashboard"],
        "app/dashboard.html": ["dashboard"],
        "app/jobs.html": ["__PLATFORM_APP__"],
    }

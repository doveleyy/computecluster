"""Bar pins: each user's choice of which registered apps their platform bar
shows. Pins filter the bar; they never decide who may open an app."""

import json
import re
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from test_platform_bar import (
    ORIGIN,
    TAILNET,
    TOKEN,
    bar_links,
    link_member,
    pin,
    platform,
    register_sample_apps,
)

from app import migrations
from app.accounts import AccountStore, UserCreate
from app.app_registry import AppNotServed, AppRegistry
from app.database import Database
from app.identity import ADMIN_USER_ID

PASSWORD = "member password 1234"
SAMPLE_APPS = frozenset({"notes", "calendar", "weather", "toolbox"})
OTHER_TAILNET = {"Tailscale-User-Login": "other@example.test"}


def register(registry: AppRegistry, app_id: str, *, installed: bool = True) -> None:
    definition: dict[str, object] = {
        "app_id": app_id,
        "route": f"/{app_id}",
        "source": {"kind": "legacy"},
        "access": "linked-user",
        "capabilities": ["identity"],
        "presentation": {
            "label": app_id.title(),
            "health_name": app_id.title(),
            "position": 25,
            "accent": "#abcdef",
        },
    }
    if installed:
        definition["installation"] = {"owner": "legacy-scripts", "host_port": 19100}
    registry.register(json.dumps(definition).encode())


def test_migration_pins_every_existing_app_for_every_existing_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "before-pins.db")
    with monkeypatch.context() as before:
        before.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:24])
        database.initialize()
    registry = AppRegistry(database)
    accounts = AccountStore(database)
    register_sample_apps(registry)
    early = accounts.create(UserCreate(username="early", password=PASSWORD))

    database.initialize()
    later = accounts.create(UserCreate(username="later", password=PASSWORD))
    register(registry, "journal")

    assert registry.pins(early.id) == SAMPLE_APPS
    assert registry.pins(UUID(ADMIN_USER_ID)) == SAMPLE_APPS
    assert registry.pins(later.id) == frozenset()
    database.close()


def test_only_a_served_app_can_be_pinned_and_unpinning_is_idempotent(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "pins.db")
    database.initialize()
    registry = AppRegistry(database)
    register_sample_apps(registry)
    register(registry, "drafts", installed=False)
    user = AccountStore(database).create(UserCreate(username="alex", password=PASSWORD))

    for app_id in ("drafts", "mailbox"):
        with pytest.raises(AppNotServed):
            registry.pin(user.id, app_id)
    registry.pin(user.id, "notes")
    registry.pin(user.id, "notes")
    assert registry.pins(user.id) == {"notes"}
    registry.unpin(user.id, "notes")
    registry.unpin(user.id, "notes")
    registry.unpin(user.id, "mailbox")
    assert registry.pins(user.id) == frozenset()
    database.close()


def bar_hrefs(
    client: TestClient, tailnet: dict[str, str] | None = TAILNET, app: str = "home"
) -> list[str]:
    response = client.get("/platform/bar", params={"app": app}, headers=tailnet)
    assert response.status_code == 200
    return [href for href, _, _ in bar_links(response.text)]


def test_new_app_is_on_the_launcher_but_reaches_the_bar_only_while_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with platform(tmp_path, monkeypatch) as client:
        register_sample_apps(client.app.state.app_registry)
        link_member(client)
        launcher = client.get("/", headers=TAILNET)
        unpinned = client.get("/platform/bar", params={"app": "notes"}, headers=TAILNET)
        pin(client, "weather", "notes", "weather")
        pinned = bar_hrefs(client)
        for _ in range(2):
            removed = client.delete(
                "/platform/pins/notes", headers={**TAILNET, "Origin": ORIGIN}
            )
            assert removed.status_code == 204
        after_unpin = bar_hrefs(client)

    assert 'href="/notes/"' in launcher.text
    assert unpinned.status_code == 200
    assert bar_links(unpinned.text) == [
        ("/", "Home Platform", False),
        ("/jobs-ui", "Job Desk", False),
    ]
    assert pinned == ["/", "/notes/", "/weather/", "/jobs-ui"]
    assert after_unpin == ["/", "/weather/", "/jobs-ui"]


def test_each_user_sees_their_own_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with platform(tmp_path, monkeypatch) as client:
        register_sample_apps(client.app.state.app_registry)
        created = client.post(
            "/dashboard/api/users",
            json={"username": "other", "password": PASSWORD, "role": "MEMBER"},
        )
        assert created.status_code == 201
        link_member(client)
        pin(client, "notes")
        client.post("/dashboard/logout")
        signed_in = client.post(
            "/dashboard/login", json={"username": "other", "password": PASSWORD}
        )
        assert signed_in.status_code == 204
        linked = client.post(
            "/jobs-ui/api/account/identities/tailscale", headers=OTHER_TAILNET
        )
        assert linked.status_code == 200
        pin(client, "calendar", "weather", tailnet=OTHER_TAILNET)
        client.post("/dashboard/logout")

        assert bar_hrefs(client) == ["/", "/notes/", "/jobs-ui"]
        assert bar_hrefs(client, OTHER_TAILNET) == [
            "/",
            "/calendar/",
            "/weather/",
            "/jobs-ui",
        ]


def test_without_a_linked_identity_the_bar_has_only_builtins_and_pins_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stranger = {"Tailscale-User-Login": "stranger@example.test"}
    with platform(tmp_path, monkeypatch) as client:
        register_sample_apps(client.app.state.app_registry)
        link_member(client)
        pin(client, "notes")
        client.post("/dashboard/logout")
        refusals = [
            client.request(
                method, "/platform/pins/notes", headers={**tailnet, "Origin": ORIGIN}
            ).status_code
            for method in ("PUT", "DELETE")
            for tailnet in ({}, stranger)
        ]

        assert refusals == [401, 403, 401, 403]
        assert bar_hrefs(client, None) == ["/", "/jobs-ui"]
        assert bar_hrefs(client, stranger) == ["/", "/jobs-ui"]
        assert bar_hrefs(client) == ["/", "/notes/", "/jobs-ui"]


def test_pin_changes_need_the_configured_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with platform(tmp_path, monkeypatch) as client:
        register_sample_apps(client.app.state.app_registry)
        link_member(client)
        pin(client, "notes")
        forged = [
            client.request(method, f"/platform/pins/{app_id}", headers=headers)
            for method, app_id in (("PUT", "calendar"), ("DELETE", "notes"))
            for headers in (
                TAILNET,
                {**TAILNET, "Origin": "https://evil.test"},
                {**TAILNET, "Origin": ORIGIN + ":443"},
            )
        ]
        monkeypatch.delenv("HOME_PLATFORM_APPS_ORIGIN")
        unconfigured = client.put(
            "/platform/pins/calendar", headers={**TAILNET, "Origin": ORIGIN}
        )

        assert [response.status_code for response in forged] == [403] * 6
        assert unconfigured.status_code == 403
        assert bar_hrefs(client) == ["/", "/notes/", "/jobs-ui"]


def test_only_served_apps_can_be_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with platform(tmp_path, monkeypatch) as client:
        register(client.app.state.app_registry, "drafts", installed=False)
        link_member(client)
        refused = [
            client.put(
                f"/platform/pins/{app_id}", headers={**TAILNET, "Origin": ORIGIN}
            ).status_code
            for app_id in ("mailbox", "drafts", "jobs")
        ]

    assert refused == [404, 404, 404]


def test_a_pin_never_shows_an_app_the_role_cannot_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with platform(tmp_path, monkeypatch) as client:
        register_sample_apps(client.app.state.app_registry)
        link_member(client)
        pin(client, "toolbox")
        member = bar_hrefs(client, app="toolbox")
        client.post("/dashboard/logout")
        signed_out = bar_hrefs(client, app="toolbox")
        assert client.post("/dashboard/login", json={"token": TOKEN}).status_code == 204
        admin = bar_hrefs(client, app="toolbox")

    assert member == signed_out == ["/", "/jobs-ui"]
    assert admin == ["/", "/dashboard", "/toolbox/"]


def test_launcher_pin_toggles_mirror_the_viewers_pins_on_registered_tiles_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with platform(tmp_path, monkeypatch) as client:
        register_sample_apps(client.app.state.app_registry)
        link_member(client)
        pin(client, "calendar")
        page = client.get("/", headers=TAILNET).text

    toggles = [
        dict(re.findall(r'([\w-]+)="([^"]*)"', button))
        for button in re.findall(r"<button [^>]*>", page)
    ]
    assert [
        (toggle["data-app"], toggle["aria-pressed"], toggle["aria-label"])
        for toggle in toggles
    ] == [
        ("notes", "false", "Pin Notes to the bar"),
        ("calendar", "true", "Pin Calendar to the bar"),
        ("weather", "false", "Pin Weather to the bar"),
    ]
    assert re.findall(r'<a class="tile" href="([^"]+)"', page)[-1] == "/jobs-ui"
    assert re.search(r"<a [^>]*>(?:(?!</a>).)*<button", page, re.S) is None

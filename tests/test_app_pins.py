"""Bar pins: each user's choice of which registered apps their platform bar
shows. Pins filter the bar; they never decide who may open an app."""

import json
from pathlib import Path
from uuid import UUID

import pytest
from test_platform_bar import register_sample_apps

from app import migrations
from app.accounts import AccountStore, UserCreate
from app.app_registry import AppNotServed, AppRegistry
from app.database import Database
from app.identity import ADMIN_USER_ID

PASSWORD = "member password 1234"
SAMPLE_APPS = frozenset({"notes", "calendar", "weather", "toolbox"})


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

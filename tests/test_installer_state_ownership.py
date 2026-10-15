import os
import stat

import pytest
from test_installer_existing_state import existing_app
from test_installer_existing_state import helper as helper_fixture
from test_installer_existing_state import ledger as ledger_fixture

from app.app_contracts import ContractError
from ops import managed_installer
from ops.installer_policy import root_path

helper = helper_fixture
ledger = ledger_fixture


@pytest.fixture
def owned_state(helper, tmp_path, monkeypatch):
    installer, _, _, app, _, _, directory = existing_app(helper, tmp_path, monkeypatch)
    monkeypatch.setattr(managed_installer, "root_path", root_path)
    path = directory / app.state.sqlite_file
    directory.chmod(0o700)
    path.write_bytes(b"state")
    path.chmod(0o600)
    real_lstat = type(path).lstat

    def lstat(entry):
        info = real_lstat(entry)
        fields = list(info)
        if entry in (directory, path):
            fields[4] = app.uid
        elif entry in directory.parents:
            fields[4] = 0
            fields[0] = stat.S_IFDIR | 0o755
        return os.stat_result(fields)

    monkeypatch.setattr(type(path), "lstat", lstat)
    return installer, app, directory, path


def test_existing_state_accepts_app_owned_directory_and_file(owned_state):
    installer, app, _, _ = owned_state
    assert installer.existing_state(app) is True


def test_missing_state_is_first_install_only_after_directory_validation(owned_state):
    installer, app, directory, path = owned_state
    path.unlink()
    assert installer.existing_state(app) is False
    directory.chmod(0o722)
    with pytest.raises(ContractError, match="ownership"):
        installer.existing_state(app)


@pytest.mark.parametrize(
    "kind",
    ["symlink", "directory", "group-write", "world-write", "unreadable", "wrong-uid"],
)
def test_existing_state_refuses_unsafe_leaf(owned_state, monkeypatch, kind):
    installer, app, _, path = owned_state
    if kind in {"symlink", "directory"}:
        path.unlink()
        if kind == "symlink":
            path.symlink_to(path.parent / "absent")
        else:
            path.mkdir()
    elif kind == "wrong-uid":
        original = type(path).lstat

        def wrong_owner(entry):
            info = original(entry)
            fields = list(info)
            if entry == path:
                fields[4] = app.uid + 1
            return os.stat_result(fields)

        monkeypatch.setattr(type(path), "lstat", wrong_owner)
    else:
        path.chmod(
            {"group-write": 0o620, "world-write": 0o602, "unreadable": 0o000}[kind]
        )
    with pytest.raises(ContractError):
        installer.existing_state(app)

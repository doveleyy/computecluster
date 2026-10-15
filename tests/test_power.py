from pathlib import Path

import pytest

from app.power import (
    PowerAction,
    PowerControlUnavailableError,
    PowerRequestPendingError,
    queue_power_request,
)


def test_queue_power_request_creates_only_fixed_marker(tmp_path: Path) -> None:
    queue_power_request(tmp_path, PowerAction.REBOOT)

    marker = tmp_path / "reboot"
    assert marker.read_text() == "reboot\n"
    assert marker.stat().st_mode & 0o777 == 0o600


def test_queue_power_request_rejects_unavailable_or_pending_directory(
    tmp_path: Path,
) -> None:
    with pytest.raises(PowerControlUnavailableError):
        queue_power_request(tmp_path / "missing", PowerAction.SHUTDOWN)

    queue_power_request(tmp_path, PowerAction.SHUTDOWN)
    with pytest.raises(PowerRequestPendingError):
        queue_power_request(tmp_path, PowerAction.REBOOT)


def test_queue_power_request_rejects_symlink_directory(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(PowerControlUnavailableError):
        queue_power_request(link, PowerAction.REBOOT)

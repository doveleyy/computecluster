import os
from enum import StrEnum
from pathlib import Path


class PowerAction(StrEnum):
    REBOOT = "reboot"
    SHUTDOWN = "shutdown"


class PowerControlUnavailableError(RuntimeError):
    pass


class PowerRequestPendingError(RuntimeError):
    pass


def queue_power_request(directory: Path | None, action: PowerAction) -> None:
    """Atomically signal a root-owned systemd path unit.

    The API never receives privilege. It can only create one of two fixed marker
    names in a systemd-owned runtime directory.
    """
    if directory is None or not directory.is_dir() or directory.is_symlink():
        raise PowerControlUnavailableError("Pi power control is not configured")

    for candidate in PowerAction:
        if (directory / candidate.value).exists():
            raise PowerRequestPendingError("A Pi power request is already pending")

    marker = directory / action.value
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(marker, flags, 0o600)
    except FileExistsError as error:
        raise PowerRequestPendingError(
            "A Pi power request is already pending"
        ) from error
    try:
        os.write(descriptor, f"{action.value}\n".encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

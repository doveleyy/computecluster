import fcntl
import os
import stat
from pathlib import Path
from uuid import uuid4

from app.app_contracts import ContractError


def read_owned(path: Path, owner: int, limit: int = 65536) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != owner
            or info.st_mode & 0o022
            or info.st_nlink != 1
            or info.st_size > limit
        ):
            raise ContractError("Unsafe installer record")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            payload = source.read(limit + 1)
        if len(payload) > limit:
            raise ContractError("Installer record exceeds byte limit")
        return payload
    finally:
        os.close(descriptor)


def publish(path: Path, payload: bytes, *, mode: int = 0o644) -> None:
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fcntl.flock(directory, fcntl.LOCK_EX)
    temporary = path.parent / (".publish-" + str(uuid4()))
    try:
        try:
            existing = read_owned(path, os.geteuid(), max(len(payload), 65536))
        except FileNotFoundError:
            pass
        else:
            if existing != payload:
                raise ContractError("Immutable installer record conflicts")
            return
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode
        )
        with os.fdopen(descriptor, "wb") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        os.rename(temporary, path)
        os.fsync(directory)
    finally:
        temporary.unlink(missing_ok=True)
        os.close(directory)


def write_private(path: Path, payload: bytes, uid: int, gid: int) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    try:
        os.fchown(descriptor, uid, gid)
        with os.fdopen(descriptor, "wb", closefd=False) as target:
            target.write(payload)
    finally:
        os.close(descriptor)

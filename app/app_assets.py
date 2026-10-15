import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import BinaryIO


class ComputeError(ValueError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@contextmanager
def confined_directory(root: Path, relative: str) -> Iterator[int]:
    parts = PurePosixPath(relative).parts
    if (
        relative.startswith("/")
        or "\\" in relative
        or any(p in {".", ".."} for p in parts)
    ):
        raise ComputeError(422, "Invalid relative path")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def confined_file(root: Path, relative: str) -> Iterator[BinaryIO]:
    path = PurePosixPath(relative)
    with confined_directory(root, str(path.parent)) as directory:
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ComputeError(409, "Expected a regular file")
        yield source


def fingerprint(source: BinaryIO) -> str:
    info = os.fstat(source.fileno())
    return (
        f"{info.st_dev}:{info.st_ino}:{info.st_size}:"
        f"{info.st_mtime_ns}:{info.st_ctime_ns}"
    )


def copy_digest(
    source: BinaryIO, output: BinaryIO | None, limit: int
) -> tuple[int, str, str]:
    before = fingerprint(source)
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        size += len(chunk)
        if size > limit:
            raise ComputeError(413, "Input exceeds the size limit")
        digest.update(chunk)
        if output is not None:
            output.write(chunk)
    if before != fingerprint(source):
        raise ComputeError(409, "File changed while being read")
    return size, digest.hexdigest(), before

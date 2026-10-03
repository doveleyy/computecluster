"""Exact-content duplicate review of one dump and its shared tree.

Filesystem presence establishes candidates; historical hashes never establish
that a copy still exists. Discards and whole-folder items are outside this view.
Checks bypass the stat-keyed cache and read current content. Reading is streamed,
including explicit large checks; resolution rechecks the entire group.
"""

from __future__ import annotations

import os
import stat
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from services.file_sorter.library import (
    MAX_FOLDERS,
    MAX_QUEUE_FILES,
    STOP,
    Library,
    LibraryError,
    category_folder,
    file_sha256,
    queueable,
)
from services.file_sorter.repository import Project, SorterRepository

AUTOMATIC_BYTES = 16 * 1024 * 1024
FULL_HASH_BYTES = 2**63 - 1


@dataclass(frozen=True)
class Problem:
    """An entry the index could not read.

    `incomplete` means something may be hidden in that area, so nothing there
    may be declared missing; a file that simply vanished is just absent.
    """

    area: str
    detail: str
    incomplete: bool = True


def vanished(error: Exception) -> bool:
    return isinstance(error, FileNotFoundError) or (
        isinstance(error, LibraryError) and error.status == 404
    )


@dataclass(frozen=True)
class Copy:
    area: str
    path: str
    size: int
    mtime_ns: str

    def json(self) -> dict[str, Any]:
        return {**asdict(self), "name": self.path.rsplit("/", 1)[-1]}


class DuplicateReview:
    def __init__(
        self,
        project: Project,
        library: Library,
        repository: SorterRepository,
        units: set[str],
    ) -> None:
        self.project = project
        self.library = library
        self.repository = repository
        self.units = units

    def location(self, area: str, path: str) -> Path:
        if area == "dump":
            location = self.library.entry_path(path)
            if not location.is_file():
                raise LibraryError("Duplicate review applies to individual files")
            return location
        return self.library.sorted_entry_path(path, self.units)

    def copy(self, area: str, path: str) -> Copy:
        stat = self.location(area, path).stat()
        return Copy(area, path, stat.st_size, str(stat.st_mtime_ns))

    def candidates(
        self,
        *,
        include_dump: bool = True,
        include_tree: bool = True,
        problems: list[Problem] | None = None,
    ) -> list[Copy]:
        """Every kept file. Strict by default: any read error raises.

        With `problems`, an entry or folder that cannot be read is recorded and
        skipped instead (the background index uses this so that one bad file
        does not stop everything). A recorded problem marks its area
        incomplete; a file that simply vanished does not.
        """
        copies = []
        self.library.walk_errors = []
        entries = self.library.entries() if include_dump else []
        if len(entries) >= MAX_QUEUE_FILES:
            raise LibraryError(
                "Too many dump entries for a complete duplicate check", 409
            )
        if problems is not None:
            for error in getattr(self.library, "walk_errors", []):
                problems.append(Problem("dump", error, incomplete=True))
        for path in entries:
            try:
                if self.library.entry(path).kind == "file":
                    copies.append(self.copy("dump", path))
            except (OSError, LibraryError) as error:
                if problems is None:
                    raise
                problems.append(
                    Problem("dump", f"{path}: {error}", not vanished(error))
                )

        if not include_tree:
            return copies

        def failed(error: OSError) -> None:
            if problems is not None:
                problems.append(
                    Problem("tree", f"{error.filename}: {error.strerror or error}")
                )
                return
            raise LibraryError(
                "A folder could not be read; duplicate check incomplete", 409
            ) from error

        for folders, (current, dirs, files) in enumerate(
            os.walk(self.library.sorted_root, followlinks=False, onerror=failed),
            start=1,
        ):
            if folders > MAX_FOLDERS:
                raise LibraryError(
                    "Too many folders for a complete duplicate check", 409
                )
            base = Path(current)

            def relative(name: str, base: Path = base) -> str:
                return (base / name).relative_to(self.library.sorted_root).as_posix()

            dirs[:] = sorted(
                name
                for name in dirs
                if category_folder(name)
                and queueable(name)
                # `__stop__` is the label marker and can never be a category;
                # its contents are outside the sorter, as hidden folders are.
                and name != STOP
                and relative(name) not in self.units
                and not (base / name).is_symlink()
            )
            # The walk supplies exact entry names. Validate the category once
            # per directory, rather than listing every ancestor for every file.
            # Actions and hashing still use location()/copy() for fresh checks.
            try:
                self.library.category_path(
                    base.relative_to(self.library.sorted_root).as_posix()
                    if base != self.library.sorted_root
                    else "",
                    self.units,
                )
            except (OSError, LibraryError) as error:
                if problems is None:
                    raise
                problems.append(Problem("tree", f"{base}: {error}"))
                dirs[:] = []
                continue
            for name in sorted(files):
                if queueable(name):
                    try:
                        snapshot = (base / name).lstat()
                        if not stat.S_ISREG(snapshot.st_mode):
                            continue
                        copies.append(
                            Copy(
                                "tree",
                                relative(name),
                                snapshot.st_size,
                                str(snapshot.st_mtime_ns),
                            )
                        )
                    except (OSError, LibraryError) as error:
                        if problems is None:
                            raise
                        problems.append(
                            Problem(
                                "tree",
                                f"{relative(name)}: {error}",
                                not vanished(error),
                            )
                        )
                        continue
                    if len(copies) >= MAX_QUEUE_FILES:
                        raise LibraryError(
                            "Too many files for a complete duplicate check", 409
                        )
        return copies

    def digest(self, copy: Copy, *, fresh: bool = False) -> str:
        base = self.project.source if copy.area == "dump" else self.project.target
        key = f"{base}/{copy.path}"
        digest = (
            None
            if fresh
            else self.repository.cached_hash(key, copy.size, int(copy.mtime_ns))
        )
        if digest is None:
            digest = file_sha256(self.location(copy.area, copy.path), FULL_HASH_BYTES)
        if self.copy(copy.area, copy.path) != copy:
            raise LibraryError(
                "A file changed during duplicate checking; scan again", 409
            )
        if digest is None:
            raise LibraryError("Could not hash this file", 409)
        if not fresh:
            self.repository.remember_hash(key, copy.size, int(copy.mtime_ns), digest)
        return digest

    def matches(
        self,
        area: str,
        path: str,
        *,
        force: bool = False,
        fresh: bool = True,
        include_dump: bool = True,
    ) -> tuple[str | None, list[Copy]]:
        chosen = self.copy(area, path)
        if not force and chosen.size > AUTOMATIC_BYTES:
            return None, []
        digest = self.digest(chosen, fresh=fresh)
        matches = [chosen]
        for copy in self.candidates(include_dump=include_dump):
            if copy == chosen or copy.size != chosen.size:
                continue
            if self.digest(copy, fresh=fresh) == digest:
                matches.append(copy)
        return digest, matches

    def require_single(
        self,
        area: str,
        path: str,
        *,
        include_dump: bool = True,
        known: list[Copy] | None = None,
    ) -> None:
        """Refuse to keep a second identical copy.

        `known` is a list of kept files from the index; without it the dump and
        tree are walked. Either way only same-size files are hashed, freshly,
        and each is re-read from disk first, so a stale index entry can never
        block a sort; at worst a copy added since the last scan is caught by
        the next one and shows up in duplicate review.
        """
        chosen = self.copy(area, path)
        # Empty files carry no content to duplicate: every empty __init__.py
        # is "identical", yet each belongs where its project put it.
        if chosen.size == 0:
            return
        pool = (
            known if known is not None else self.candidates(include_dump=include_dump)
        )
        same = [
            copy
            for copy in pool
            if copy.size == chosen.size
            and (copy.area, copy.path) != (chosen.area, chosen.path)
        ]
        if not same:
            return
        digest = self.digest(chosen, fresh=True)
        for copy in same:
            try:
                current = self.copy(copy.area, copy.path)
                identical = (
                    current.size == chosen.size
                    and self.digest(current, fresh=True) == digest
                )
            except (OSError, LibraryError) as error:
                if vanished(error):
                    continue
                raise
            if identical:
                raise LibraryError(
                    "Identical copies exist. Resolve duplicates and choose one "
                    "keeper first.",
                    409,
                )

    def scan(self, *, force: bool = False, include_dump: bool = True) -> dict[str, Any]:
        sizes: dict[int, list[Copy]] = defaultdict(list)
        copies = self.candidates(include_dump=include_dump)
        for copy in copies:
            sizes[copy.size].append(copy)
        groups = []
        skipped = checked = 0
        for size, candidates in sizes.items():
            if len(candidates) < 2 or size == 0:
                continue
            if size > AUTOMATIC_BYTES and not force:
                skipped += len(candidates)
                continue
            hashes: dict[str, list[Copy]] = defaultdict(list)
            for copy in candidates:
                hashes[self.digest(copy, fresh=True)].append(copy)
                checked += 1
            for digest, matches in hashes.items():
                if len(matches) > 1:
                    groups.append(
                        {"sha256": digest, "copies": [c.json() for c in matches]}
                    )
        return {
            "groups": groups,
            "files": len(copies),
            "checked": checked,
            "skipped_large": skipped,
        }

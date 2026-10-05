"""Filesystem operations on the owner's library of dumps and trees.

Every dump and tree lives under one library root on one filesystem, so every
move is a single server-side rename: no bytes are copied, and a move is never
half done.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import logging
import os
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DISCARD_FOLDER = "_discarded"
RESERVED_FOLDERS = {DISCARD_FOLDER, "_routing"}
STOP = "__stop__"
IGNORED_NAMES = {".DS_Store", "@eaDir", "#recycle", "Thumbs.db", "desktop.ini"}
PARTIAL_SUFFIXES = (".crdownload", ".part", ".partial", ".download", ".tmp")
MAX_DEPTH = 12
MAX_FOLDERS = 5_000
MAX_QUEUE_FILES = 100_000
MAX_BROWSE = 500
MODES = {"top", "files"}
# Larger files are logged without a hash rather than read whole over SMB.
HASH_LIMIT_BYTES = 32 * 1024 * 1024


class LibraryError(Exception):
    """A request the library refuses; the message is safe to show the owner."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def rename_no_replace(source: Path, target: Path) -> None:
    """Ask the mounted filesystem to refuse replacement.

    Never fall back to an exists-check followed by ordinary rename: that
    would allow a concurrent local writer's destination to be overwritten.
    Independent SMB clients depend on the kernel's server-side rename flags;
    this syscall does not establish a cross-client guarantee on CIFS.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "linux" and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    elif sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(os.fsencode(source), os.fsencode(target), 4)
    else:
        raise LibraryError("This system cannot safely move without replacement", 503)
    if result:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise LibraryError(
                "The destination already exists; reload and rename it", 409
            )
        if code in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
            raise LibraryError(
                "This filesystem cannot safely move without replacement", 503
            )
        raise OSError(code, os.strerror(code), str(source))


@dataclass(frozen=True)
class Entry:
    name: str
    kind: str
    size: int | None
    mtime_ns: int


def existing_name(name: str) -> str:
    """Validate a name that already exists on disk, however untidy.

    Downloads keep whatever name the browser gave them, including leading or
    trailing spaces. Only names that cannot denote one child are refused.
    """
    if (
        not isinstance(name, str)
        or name in {"", ".", ".."}
        or any(character in name for character in "/\x00")
        or not representable(name)
        or len(name.encode()) > 255
    ):
        raise LibraryError("Invalid file or folder name")
    return name


def representable(name: str) -> bool:
    """False for names that are not valid UTF-8 on disk.

    Python decodes such bytes as lone surrogates; they cannot be shown,
    typed back or stored as text, so the sorter leaves those entries alone.
    """
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def check_name(name: str) -> str:
    """Validate a name the owner is about to create: stricter than on disk."""
    if (
        not isinstance(name, str)
        or not name.strip()
        or name != name.strip()
        or name in {".", "..", STOP}
        or any(character in name for character in "/\\\x00")
        or len(name.encode()) > 255
    ):
        raise LibraryError("Invalid file or folder name")
    return name


def check_file_name(name: str) -> str:
    """A new filename the sorter's own views will still show."""
    check_name(name)
    if not queueable(name):
        raise LibraryError(
            "That filename would be hidden: it starts with a dot or looks like "
            "a temporary or system file"
        )
    return name


def check_folder_name(name: str) -> str:
    """A new category folder name the tree will still show."""
    check_name(name)
    if not (category_folder(name) and queueable(name)):
        raise LibraryError(
            "That folder would be hidden: folder names cannot start with . # or "
            "@, use a reserved name, or look like temporary files"
        )
    return name


def folder_parts(relative: str) -> list[str]:
    if relative in {"", "."}:
        return []
    parts = [existing_name(part) for part in relative.split("/")]
    if STOP in parts:
        raise LibraryError("Invalid file or folder name")
    return parts


def entry_parts(relative: str) -> list[str]:
    """Split a dump-relative entry path; every part must be a visible name."""
    parts = [existing_name(part) for part in relative.split("/")]
    if not all(queueable(part) for part in parts):
        raise LibraryError("That entry is not in the queue", 404)
    return parts


def library_parts(relative: str) -> list[str]:
    """Split a library-relative project folder; hidden and reserved refused."""
    parts = folder_parts(relative)
    if not parts or not all(category_folder(part) for part in parts):
        raise LibraryError("Choose a visible folder inside the library")
    return parts


def tree_path(folder: str, name: str) -> str:
    """The tree-relative path of `name` inside `folder` ("" is the root)."""
    return f"{folder}/{name}" if folder else name


def nested(a: str, b: str) -> bool:
    """True when two library paths are the same or one contains the other."""
    return a == b or a.startswith(f"{b}/") or b.startswith(f"{a}/")


def queueable(name: str) -> bool:
    return (
        representable(name)
        and not name.startswith(".")
        and name not in IGNORED_NAMES
        and not name.lower().endswith(PARTIAL_SUFFIXES)
    )


def category_folder(name: str) -> bool:
    return not name.startswith((".", "@", "#")) and name not in RESERVED_FOLDERS


def file_sha256(path: Path, limit: int = HASH_LIMIT_BYTES) -> str | None:
    if path.stat().st_size > limit:
        return None
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


# One process owns every rename; this lock serializes browser tabs and projects.
LOCK = threading.RLock()


class Library:
    """One project's view: a dump queue sorted into a tree.

    In `top` mode the queue holds the dump's top-level entries, and a folder
    moves whole. In `files` mode it holds every file in the dump, addressed by
    its path inside the dump, and emptied subfolders are tidied away.
    """

    def __init__(self, dump: Path, sorted_root: Path, mode: str = "top") -> None:
        self.dump = dump
        self.sorted_root = sorted_root
        self.mode = mode
        self.lock = LOCK

    def ready(self) -> bool:
        try:
            dump = self.dump.resolve(strict=True)
            sorted_root = self.sorted_root.resolve(strict=True)
        except OSError:
            return False
        return (
            dump.is_dir()
            and sorted_root.is_dir()
            and not dump.is_relative_to(sorted_root)
            and not sorted_root.is_relative_to(dump)
            and dump.stat().st_dev == sorted_root.stat().st_dev
        )

    def entries(self) -> list[str]:
        if self.mode == "files":
            return self._all_files()
        with os.scandir(self.dump) as listing:
            return sorted(
                (
                    item.name
                    for item in listing
                    if queueable(item.name)
                    and not item.is_symlink()
                    and (item.is_file() or item.is_dir())
                ),
                key=str.casefold,
            )

    def _all_files(self) -> list[str]:
        found: list[str] = []
        # Unreadable subfolders are listed here instead of vanishing silently:
        # an incomplete walk must never look like deleted files to the index.
        self.walk_errors: list[str] = []

        def failed(error: OSError) -> None:
            self.walk_errors.append(f"{error.filename}: {error.strerror or error}")

        for current, dirs, files in os.walk(
            self.dump, followlinks=False, onerror=failed
        ):
            base = Path(current)
            dirs[:] = sorted(
                (
                    name
                    for name in dirs
                    if queueable(name) and not (base / name).is_symlink()
                ),
                key=str.casefold,
            )
            prefix = base.relative_to(self.dump).as_posix()
            for name in sorted(files, key=str.casefold):
                if queueable(name) and not (base / name).is_symlink():
                    found.append(name if prefix == "." else f"{prefix}/{name}")
                    if len(found) >= MAX_QUEUE_FILES:
                        return sorted(found, key=str.casefold)
        # Whole-path order keeps a folder's files together, and matches how the
        # queue cache re-inserts an entry after Undo.
        return sorted(found, key=str.casefold)

    def entry_path(self, relative: str) -> Path:
        parts = entry_parts(relative)
        if self.mode == "top" and len(parts) != 1:
            raise LibraryError("That entry is not in the queue", 404)
        path = self.dump
        for part in parts:
            path = path / part
            if path.is_symlink():
                raise LibraryError("Symlinks are not sorted")
        valid = path.is_file() or (self.mode == "top" and path.is_dir())
        if not valid:
            raise LibraryError("That entry is no longer in the dump", 404)
        return path

    def entry(self, relative: str) -> Entry:
        path = self.entry_path(relative)
        status = path.stat()
        if path.is_dir():
            return Entry(relative, "folder", None, status.st_mtime_ns)
        return Entry(relative, "file", status.st_size, status.st_mtime_ns)

    def tidy(self, folder: Path) -> None:
        """Remove dump subfolders emptied by sorting (files mode only).

        A folder holding nothing but Finder or Windows metadata counts as
        empty; that metadata is regenerated on demand and is never sorted.
        Tidying is best effort: it runs after the entry has already moved,
        so a folder that cannot be removed (Finder or a download wrote into
        it meanwhile) is left behind rather than failing the move.
        """
        if self.mode != "files":
            return
        try:
            while folder != self.dump and folder.is_relative_to(self.dump):
                if folder.is_symlink() or not folder.is_dir():
                    return
                names = os.listdir(folder)
                if any(name not in IGNORED_NAMES for name in names):
                    return
                for name in names:
                    junk = folder / name
                    if junk.is_symlink() or not junk.is_file():
                        return
                for name in names:
                    (folder / name).unlink()
                folder.rmdir()
                folder = folder.parent
        except OSError as error:
            logger.warning("Left untidied dump folder %s: %s", folder, error)

    def folder_path(self, relative: str) -> Path:
        path = self.sorted_root
        for part in folder_parts(relative):
            path = path / part
            if path.is_symlink():
                raise LibraryError("Symlinks are not allowed in folder paths")
        if not path.is_dir():
            raise LibraryError("That folder does not exist", 404)
        return path

    def category_path(
        self, relative: str, units: set[str], *, discarded: bool = False
    ) -> Path:
        """A category, excluding folders that were sorted whole as one item.

        Each part must match a folder's exact name: on a case-insensitive
        share, `WORK` would otherwise land in `work` but be labelled `WORK`.
        """
        if relative == ".":
            raise LibraryError("Choose a category folder")
        parts = folder_parts(relative)
        if parts and parts[0] in RESERVED_FOLDERS:
            if not discarded or parts != [DISCARD_FOLDER]:
                raise LibraryError("That folder is reserved")
        elif not all(category_folder(part) for part in parts):
            raise LibraryError("Choose a category folder")
        if any(relative == unit or relative.startswith(f"{unit}/") for unit in units):
            raise LibraryError("That folder is a sorted item, not a category")
        path = self.folder_path(relative)
        parent = self.sorted_root
        for part in parts:
            if part not in os.listdir(parent):
                raise LibraryError("That folder does not exist", 404)
            parent = parent / part
        return path

    def sorted_entry_path(self, relative: str, units: set[str]) -> Path:
        parts = entry_parts(relative)
        parent = "/".join(parts[:-1])
        path = self.category_path(parent, units, discarded=True) / parts[-1]
        if path.is_symlink():
            raise LibraryError("Symlinks are not allowed")
        if not path.is_file():
            raise LibraryError("That file is no longer in the tree", 404)
        return path

    def sorted_entry(self, relative: str, units: set[str]) -> Entry:
        status = self.sorted_entry_path(relative, units).stat()
        return Entry(relative, "file", status.st_size, status.st_mtime_ns)

    def browse_sorted(
        self, folder: str, units: set[str], offset: int = 0, search: str = ""
    ) -> tuple[list[dict[str, object]], bool]:
        """One folder at a time; never recurse through the whole NAS library."""
        base = self.category_path(folder, units, discarded=True)
        found: list[dict[str, object]] = []
        with os.scandir(base) as listing:
            for item in listing:
                if item.is_symlink() or not queueable(item.name):
                    continue
                relative = f"{folder}/{item.name}" if folder else item.name
                if item.is_dir():
                    if relative in units or not (
                        category_folder(item.name) or relative == DISCARD_FOLDER
                    ):
                        continue
                    kind = "folder"
                elif item.is_file():
                    kind = "file"
                else:
                    continue
                if search.casefold() not in item.name.casefold():
                    continue
                found.append({"path": relative, "name": item.name, "kind": kind})
        found.sort(
            key=lambda row: (row["kind"] != "folder", str(row["name"]).casefold())
        )
        return found[offset : offset + MAX_BROWSE], len(found) > offset + MAX_BROWSE

    def reclassify(
        self,
        source_path: str,
        folder: str,
        filename: str,
        size: int | None,
        mtime_ns: int,
        units: set[str],
    ) -> str:
        with self.lock:
            source = self.sorted_entry_path(source_path, units)
            self.check_unchanged(self.sorted_entry(source_path, units), size, mtime_ns)
            target = self.category_path(folder, units) / check_file_name(filename)
            destination = f"{folder}/{filename}" if folder else filename
            if destination == source_path:
                raise LibraryError("Choose a different folder or filename")
            if target.exists() or target.is_symlink():
                raise LibraryError(
                    "A file with that name already exists there; rename it", 409
                )
            rename_no_replace(source, target)
            return destination

    def restore_sorted(self, destination: str, previous: str, units: set[str]) -> None:
        """Undo a correction to its prior tree location, including untidy names."""
        with self.lock:
            source = self.sorted_entry_path(destination, units)
            parts = entry_parts(previous)
            target = (
                self.category_path("/".join(parts[:-1]), units, discarded=True)
                / parts[-1]
            )
            if target.exists() or target.is_symlink():
                raise LibraryError(
                    "The previous location already has that filename", 409
                )
            rename_no_replace(source, target)

    def folder_stats(self, units: set[str]) -> list[tuple[str, int, int]]:
        """(path, sorted items, subfolders) for every category folder.

        Folder units that were sorted whole count as items, never as folders.
        """
        found: list[tuple[str, int, int]] = []

        def visit(path: Path, prefix: str, depth: int) -> None:
            if depth >= MAX_DEPTH:
                return
            children: list[str] = []
            items = 0
            with os.scandir(path) as listing:
                for item in listing:
                    relative = f"{prefix}/{item.name}" if prefix else item.name
                    if item.is_symlink() or not queueable(item.name):
                        continue
                    if item.is_dir() and relative not in units:
                        if category_folder(item.name):
                            children.append(item.name)
                    elif item.is_file() or item.is_dir():
                        items += 1
            if prefix:
                found.append((prefix, items, len(children)))
            for name in sorted(children, key=str.casefold):
                if len(found) >= MAX_FOLDERS:
                    return
                visit(path / name, f"{prefix}/{name}" if prefix else name, depth + 1)

        visit(self.sorted_root, "", 0)
        return found

    def folders(self, units: set[str]) -> list[str]:
        """Category folders, excluding folder units that were sorted whole."""
        return [path for path, _, _ in self.folder_stats(units)]

    def create_folder(self, parent: str, name: str) -> str:
        with self.lock:
            target = self.category_path(parent, set()) / check_folder_name(name)
            if target.exists() or target.is_symlink():
                raise LibraryError("That folder already exists", 409)
            target.mkdir()
        return f"{parent}/{name}" if parent else name

    def move_folder(self, path: str, parent: str, name: str, units: set[str]) -> str:
        """Move or rename a category folder, contents and all; never overwrite."""
        with self.lock:
            parts = folder_parts(path)
            if not parts or parts[0] in RESERVED_FOLDERS or path in units:
                raise LibraryError("Only category folders can be moved")
            if any(path.startswith(f"{unit}/") for unit in units):
                raise LibraryError("That folder belongs to a sorted item")
            if parent == path or parent.startswith(f"{path}/"):
                raise LibraryError("A folder cannot be moved inside itself")
            if parent in units or any(parent.startswith(f"{unit}/") for unit in units):
                raise LibraryError("That folder is a sorted item, not a category")
            if not parent and name in RESERVED_FOLDERS:
                raise LibraryError("That folder name is reserved")
            source = self.folder_path(path)
            target = self.category_path(parent, units) / check_folder_name(name)
            new_path = f"{parent}/{name}" if parent else name
            if new_path == path:
                return path
            if target.exists() or target.is_symlink():
                raise LibraryError("A folder with that name already exists there", 409)
            rename_no_replace(source, target)
        return new_path

    def check_unchanged(self, entry: Entry, size: int | None, mtime_ns: int) -> None:
        if (entry.size, entry.mtime_ns) != (size, mtime_ns):
            raise LibraryError(
                "This entry changed since it was previewed; reload it first", 409
            )

    def move_in(self, name: str, folder: str, filename: str) -> str:
        """Rename a dump entry into a category folder; never overwrite."""
        with self.lock:
            source = self.entry_path(name)
            target = self.category_path(folder, set()) / check_file_name(filename)
            if target.exists() or target.is_symlink():
                raise LibraryError(
                    "A file with that name already exists there; rename it", 409
                )
            rename_no_replace(source, target)
            self.tidy(source.parent)
        return f"{folder}/{filename}" if folder else filename

    def discard_destination(self, name: str) -> str:
        """The free `_discarded` name an entry will take, creating the bin."""
        with self.lock:
            source = self.entry_path(name)
            bin_path = self.sorted_root / DISCARD_FOLDER
            if bin_path.is_symlink():
                raise LibraryError("The discard folder must not be a symlink")
            bin_path.mkdir(exist_ok=True)
            leaf = source.name
            stem, suffix = (leaf, "") if source.is_dir() else os.path.splitext(leaf)
            candidate = leaf
            counter = 1
            while (bin_path / candidate).exists() or (
                bin_path / candidate
            ).is_symlink():
                counter += 1
                candidate = f"{stem} ({counter}){suffix}"
        return f"{DISCARD_FOLDER}/{candidate}"

    def discard(self, name: str, destination: str) -> None:
        """Rename a dump entry to the name `discard_destination` chose."""
        with self.lock:
            source = self.entry_path(name)
            rename_no_replace(source, self.sorted_root / destination)
            self.tidy(source.parent)

    def move_back(self, destination: str, original_name: str) -> None:
        with self.lock:
            parts = folder_parts(destination)
            source = self.folder_path("/".join(parts[:-1])) / parts[-1]
            if source.is_symlink() or not source.exists():
                raise LibraryError(
                    "The sorted entry is no longer where it was put", 409
                )
            original = entry_parts(original_name)
            parent = self.dump
            for part in original[:-1]:
                parent = parent / part
                if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                    raise LibraryError("The original folder is no longer usable", 409)
            # Files mode may have tidied the original subfolder away.
            parent.mkdir(parents=True, exist_ok=True)
            target = parent / original[-1]
            if target.exists() or target.is_symlink():
                raise LibraryError("The dump already has an entry with that name", 409)
            rename_no_replace(source, target)

    def sorted_files(self, units: set[str]) -> Iterator[tuple[str, str, str]]:
        """Yield (folder, name, kind) for entries already inside category folders."""
        folders = ["", *self.folders(units)]
        discarded = self.sorted_root / DISCARD_FOLDER
        if discarded.is_dir() and not discarded.is_symlink():
            folders.append(DISCARD_FOLDER)
        for folder in folders:
            path = self.sorted_root / folder if folder else self.sorted_root
            with os.scandir(path) as listing:
                for item in sorted(listing, key=lambda item: item.name.casefold()):
                    relative = f"{folder}/{item.name}" if folder else item.name
                    if item.is_symlink() or not queueable(item.name):
                        continue
                    if item.is_file():
                        yield folder, item.name, "file"
                    elif relative in units:
                        yield folder, item.name, "folder"


class LibraryRoot:
    """The library as a whole: browsing it and checking project folders."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def ready(self) -> bool:
        return self.root.is_dir() and not self.root.is_symlink()

    def folder(self, relative: str) -> Path:
        path = self.root
        for part in library_parts(relative):
            path = path / part
            if path.is_symlink():
                raise LibraryError("Symlinks are not allowed in folder paths")
        if not path.is_dir():
            raise LibraryError(f"{relative} does not exist", 404)
        return path

    def exists(self, relative: str) -> bool:
        try:
            self.folder(relative)
        except LibraryError:
            return False
        return True

    def project(self, source: str, target: str, mode: str) -> Library:
        if mode not in MODES:
            raise LibraryError("Unknown queue mode")
        return Library(self.folder(source), self.folder(target), mode)

    def check_project(
        self,
        source: str,
        target: str,
        mode: str,
        others: list[tuple[str, str]],
        create_target: bool = False,
    ) -> None:
        """Refuse pairs that would let one queue or tree leak into another."""
        if mode not in MODES:
            raise LibraryError("Choose top-level entries or every file")
        library_parts(source)
        library_parts(target)
        self.folder(source)
        if create_target and not self.exists(target):
            parent, _, name = target.rpartition("/")
            check_folder_name(name)
            if parent:
                self.folder(parent)
        else:
            self.folder(target)
        if nested(source, target):
            raise LibraryError("The dump and the tree must be separate folders")
        for other_source, other_target in others:
            if nested(source, other_source):
                raise LibraryError(f"{other_source} is already being sorted")
            if nested(source, other_target):
                raise LibraryError(f"{other_target} is another project's tree")
            if nested(target, other_source):
                raise LibraryError(f"{other_source} is another project's dump")
            if target != other_target and nested(target, other_target):
                raise LibraryError(
                    f"{other_target} is a tree already; share it or pick a "
                    "folder outside it"
                )

    def create_target(self, target: str) -> None:
        with LOCK:
            if self.exists(target):
                return
            parent, _, name = target.rpartition("/")
            base = self.folder(parent) if parent else self.root
            (base / check_folder_name(name)).mkdir()

    def browse(self, relative: str) -> list[dict[str, object]]:
        """Visible subfolders of a library folder, for the project pickers."""
        base = self.folder(relative) if relative else self.root
        found: list[dict[str, object]] = []
        with os.scandir(base) as listing:
            children = sorted(
                (
                    item.name
                    for item in listing
                    if category_folder(item.name) and item.is_dir(follow_symlinks=False)
                ),
                key=str.casefold,
            )
        for name in children[:MAX_BROWSE]:
            path = base / name
            folders = entries = 0
            with os.scandir(path) as listing:
                for item in listing:
                    if not queueable(item.name) or item.is_symlink():
                        continue
                    entries += 1
                    if item.is_dir() and category_folder(item.name):
                        folders += 1
            found.append(
                {
                    "name": name,
                    "path": f"{relative}/{name}" if relative else name,
                    "folders": folders,
                    "entries": entries,
                }
            )
        return found

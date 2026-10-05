"""Create and verify an online SQLite backup with bounded retention."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    """Row count per user table: the content fingerprint a backup must match."""
    names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    counts: dict[str, int] = {}
    for name in names:
        quoted = '"' + name.replace('"', '""') + '"'
        row = connection.execute(f"SELECT count(*) FROM {quoted}").fetchone()
        counts[name] = row[0]
    return counts


def verify_backup(path: Path, *, expected: dict[str, int] | None = None) -> None:
    checksum = path.with_suffix(".db.sha256")
    if checksum.exists():
        recorded = checksum.read_text(encoding="ascii").split()[0]
        actual = sha256_file(path)
        if recorded != actual:
            raise RuntimeError(
                f"sha256 mismatch for {path}: sidecar {recorded}, file {actual}"
            )
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise RuntimeError(f"SQLite integrity check failed: {result!r}")
        counts = table_counts(connection)
    finally:
        connection.close()
    if not counts:
        raise RuntimeError(f"backup has no user tables: {path}")
    if expected is not None and counts != expected:
        raise RuntimeError(
            f"backup content differs from source: {counts} != {expected}"
        )


def create_backup(source: Path, destination: Path, *, retain: int = 14) -> Path:
    if retain < 1:
        raise ValueError("retention must keep at least one backup")
    if not source.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {source}")
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    final = destination / f"home-platform-{stamp}-{uuid4().hex[:8]}.db"
    remote_temporary = destination / f".{final.name}.{uuid4().hex}.part"
    checksum = final.with_suffix(".db.sha256")
    checksum_temporary = destination / f".{checksum.name}.{uuid4().hex}.part"
    try:
        # SQLite must not create its backup database directly on CIFS. Build and
        # verify a closed local copy first; only immutable bytes cross the NAS
        # boundary.
        with tempfile.TemporaryDirectory(prefix="home-platform-backup-") as work:
            local = Path(work) / "backup.db"
            source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
            backup_connection = sqlite3.connect(local)
            try:
                # The open read transaction pins one snapshot for both the
                # counts and the page copy, so the fingerprint is exact.
                source_connection.execute("BEGIN")
                expected = table_counts(source_connection)
                if not expected:
                    raise RuntimeError(f"SQLite source has no user tables: {source}")
                source_connection.backup(backup_connection)
            finally:
                backup_connection.close()
                source_connection.close()
            verify_backup(local, expected=expected)
            digest = sha256_file(local)
            shutil.copyfile(local, remote_temporary)
        if sha256_file(remote_temporary) != digest:
            raise RuntimeError("backup digest changed while copying to destination")
        os.replace(remote_temporary, final)
        checksum_temporary.write_text(f"{digest}  {final.name}\n", encoding="ascii")
        os.replace(checksum_temporary, checksum)
    finally:
        remote_temporary.unlink(missing_ok=True)
        checksum_temporary.unlink(missing_ok=True)

    backups = sorted(destination.glob("home-platform-*.db"), reverse=True)
    for expired in backups[retain:]:
        expired.unlink(missing_ok=True)
        expired.with_suffix(".db.sha256").unlink(missing_ok=True)
    return final


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="live SQLite database")
    parser.add_argument("destination", type=Path, help="private backup directory")
    parser.add_argument("--retain", type=int, default=14, help="backup sets to keep")
    parser.add_argument(
        "--verify", type=Path, help="verify an existing backup instead of creating one"
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.verify is not None:
        verify_backup(args.verify)
        print(f"verified {args.verify}")
        return
    created = create_backup(args.source, args.destination, retain=args.retain)
    print(f"created {created}")


if __name__ == "__main__":
    main()

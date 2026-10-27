import argparse
import ctypes
import hashlib
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from uuid import UUID

from app.app_contracts import parse_document
from app.app_deployment_contracts import Operation
from app.app_deployments import (
    ReconciliationPending,
    read_authority,
    read_installed_authority,
)

# A boot that must wait for core to finish an operation ends with this status;
# the app unit lists it as a success status so systemd neither fails nor
# restarts the unit, and the helper starts it once the operation settles.
DEFERRED = os.EX_TEMPFAIL


def backup(source: Path, target: Path) -> None:
    with (
        closing(
            sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=20)
        ) as database,
        closing(sqlite3.connect(target)) as output,
    ):
        database.backup(output, pages=256, sleep=0.05)
        if output.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Backup integrity failed")
        # The copy inherits the source's WAL header, and a WAL database cannot
        # open without a writable directory for its -shm file. Rehearsal and
        # restore mount copies read-only, so the copy carries its own journal.
        if output.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
            raise ValueError("Backup journal mode is not delete")
    with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)) as output:
        tables = output.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        content_sha256 = hashlib.sha256(
            "\n".join(output.iterdump()).encode()
        ).hexdigest()
    print(
        json.dumps(
            {
                "sha256": hashlib.file_digest(target.open("rb"), "sha256").hexdigest(),
                "tables": [row[0] for row in tables],
                "content_sha256": content_sha256,
            }
        )
    )


def main() -> None:
    if os.geteuid() == 0 or ctypes.CDLL(None).prctl(38, 1, 0, 0, 0) != 0:
        raise ValueError("Unprivileged no-new-privileges child required")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "kind", choices=("authority", "operation", "state", "installed", "backup")
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("target")
    parser.add_argument("digest", nargs="?")
    args = parser.parse_args()
    if args.kind == "backup":
        backup(args.source, Path(args.target))
    else:
        with sqlite3.connect(args.source.as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            if args.kind == "installed":
                print(
                    read_installed_authority(connection, args.target).model_dump_json()
                )
                return
            if args.kind == "state":
                row = connection.execute(
                    "SELECT state FROM app_operations WHERE operation_id = ?",
                    (str(UUID(args.target)),),
                ).fetchone()
                if row is None:
                    raise ValueError("Unknown operation")
                print(row[0])
                return
            if args.kind == "operation":
                row = connection.execute(
                    (
                        "SELECT operation_json FROM app_operations WHERE operation_id"
                        " = ? AND state IN ('queued', 'switching')"
                    ),
                    (str(UUID(args.target)),),
                ).fetchone()
                if row is None:
                    raise ValueError("No active operation")
                print(parse_document(Operation, row[0].encode()).model_dump_json())
                return
            print(
                read_authority(
                    connection, UUID(args.target), args.digest
                ).model_dump_json()
            )


if __name__ == "__main__":
    try:
        main()
    except ReconciliationPending as pending:
        print(pending, file=sys.stderr)
        sys.exit(DEFERRED)
    except Exception:
        print("Installer child refused the operation", file=sys.stderr)
        sys.exit(1)

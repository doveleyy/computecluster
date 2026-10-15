import sqlite3
import sys
from pathlib import Path

import pytest

from ops.backup_sqlite import create_backup, main, sha256_file, verify_backup


def make_source(path: Path, rows: int = 1) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, state TEXT)")
        connection.executemany(
            "INSERT INTO jobs VALUES (?, 'QUEUED')",
            [(str(index),) for index in range(rows)],
        )
        connection.commit()
    finally:
        connection.close()


def seed_old_backups(destination: Path, count: int) -> list[Path]:
    destination.mkdir(exist_ok=True)
    old = []
    for index in range(count):
        path = destination / f"home-platform-2000010{index}T000000Z.db"
        path.write_bytes(b"old")
        path.with_suffix(".db.sha256").write_text("old\n")
        old.append(path)
    return old


def test_online_backup_is_verified_and_has_checksum(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    make_source(source)
    destination = tmp_path / "backups"

    backup = create_backup(source, destination)
    verify_backup(backup)

    restored = sqlite3.connect(backup)
    try:
        assert restored.execute("SELECT * FROM jobs").fetchall() == [("0", "QUEUED")]
    finally:
        restored.close()
    assert backup.with_suffix(".db.sha256").read_text().startswith(sha256_file(backup))


def test_backup_retention_removes_oldest_sets(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    make_source(source)
    destination = tmp_path / "backups"
    seed_old_backups(destination, 3)

    created = create_backup(source, destination, retain=2)

    assert len(list(destination.glob("*.db"))) == 2
    assert created.exists()
    assert not (destination / "home-platform-20000100T000000Z.db").exists()


def test_corrupt_database_fails_verification(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not sqlite")

    with pytest.raises(sqlite3.DatabaseError):
        verify_backup(corrupt)


def test_empty_source_fails_before_retention(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    source.write_bytes(b"")
    destination = tmp_path / "backups"
    old = seed_old_backups(destination, 3)

    with pytest.raises(RuntimeError, match="no user tables"):
        create_backup(source, destination, retain=2)

    assert sorted(destination.iterdir()) == sorted(
        [*old, *(path.with_suffix(".db.sha256") for path in old)]
    )


def test_backup_without_tables_fails_verification(tmp_path: Path) -> None:
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()

    with pytest.raises(RuntimeError, match="no user tables"):
        verify_backup(empty)


def test_backup_content_must_match_source_fingerprint(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    make_source(source, rows=3)
    backup = create_backup(source, tmp_path / "backups")
    verify_backup(backup, expected={"jobs": 3})

    with pytest.raises(RuntimeError, match="differs from source"):
        verify_backup(backup, expected={"jobs": 0})
    with pytest.raises(RuntimeError, match="differs from source"):
        verify_backup(backup, expected={"jobs": 3, "users": 0})


def test_verify_checks_sha256_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "live.db"
    make_source(source)
    destination = tmp_path / "backups"
    backup = create_backup(source, destination)
    sidecar = backup.with_suffix(".db.sha256")
    argv = ["backup_sqlite", str(source), str(destination), "--verify", str(backup)]
    monkeypatch.setattr(sys, "argv", argv)
    main()

    sidecar.write_text(f"{'0' * 64}  {backup.name}\n", encoding="ascii")

    with pytest.raises(RuntimeError, match="sha256"):
        main()

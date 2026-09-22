import sqlite3
from pathlib import Path

import pytest

from ops.backup_sqlite import create_backup, sha256_file, verify_backup


def test_online_backup_is_verified_and_has_checksum(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    connection = sqlite3.connect(source)
    connection.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, state TEXT)")
    connection.execute("INSERT INTO jobs VALUES ('one', 'QUEUED')")
    connection.commit()
    destination = tmp_path / "backups"

    backup = create_backup(source, destination)
    verify_backup(backup)

    restored = sqlite3.connect(backup)
    try:
        assert restored.execute("SELECT * FROM jobs").fetchall() == [("one", "QUEUED")]
    finally:
        restored.close()
        connection.close()
    assert backup.with_suffix(".db.sha256").read_text().startswith(sha256_file(backup))


def test_backup_retention_removes_oldest_sets(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    sqlite3.connect(source).close()
    destination = tmp_path / "backups"
    for index in range(3):
        old = destination / f"home-platform-2000010{index}T000000Z.db"
        old.parent.mkdir(exist_ok=True)
        old.write_bytes(b"old")
        old.with_suffix(".db.sha256").write_text("old\n")

    created = create_backup(source, destination, retain=2)

    assert len(list(destination.glob("*.db"))) == 2
    assert created.exists()
    assert not (destination / "home-platform-20000100T000000Z.db").exists()


def test_corrupt_database_fails_verification(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not sqlite")

    with pytest.raises(sqlite3.DatabaseError):
        verify_backup(corrupt)

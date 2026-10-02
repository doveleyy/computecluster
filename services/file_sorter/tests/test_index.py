from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.file_sorter import main
from services.file_sorter.duplicates import DuplicateReview
from services.file_sorter.index import FileIndex
from services.file_sorter.library import LibraryError
from services.file_sorter.repository import Seed, SorterRepository

P = "/api/projects/1"
START_INDEX = FileIndex.start


@pytest.fixture
def indexed(tmp_path: Path) -> Iterator[tuple[TestClient, FileIndex, Path, Path]]:
    root = tmp_path / "library"
    (root / "dump").mkdir(parents=True)
    (root / "tree/work/nested").mkdir(parents=True)
    (root / "tree/school").mkdir()
    database = tmp_path / "db"
    app = main.create_app(
        database,
        library_root=root,
        seed=Seed("Index", "dump", "tree"),
        allow_dev_identity=True,
        background_index=False,
    )
    with TestClient(app) as client:
        yield client, app.state.index, root, database


def payload(client: TestClient, path: str, area: str = "tree") -> dict[str, Any]:
    route = "/sorted-entry" if area == "tree" else "/current"
    entry = client.get(P + route, params={"path": path}).json()["entry"]
    return {key: entry[key] for key in ("path", "size", "mtime_ns")}


def manifest(client: TestClient) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in client.get(P + "/labels.jsonl").text.splitlines()
    ]


def manual(client: TestClient, root: Path, name: str = "a.txt") -> dict[str, Any]:
    (root / "dump" / name).write_text("human labelled content " + name)
    response = client.post(
        P + "/classify",
        json={**payload(client, name, "dump"), "folder": "work", "filename": name},
    )
    assert response.status_code == 200, response.text
    return next(row for row in manifest(client) if row["name"] == name)


def test_unchanged_scan_reads_no_bytes_and_edit_hashes_only_changed_file(
    indexed: Any,
    monkeypatch: Any,
) -> None:
    client, index, root, database = indexed
    (root / "tree/work/a.txt").write_text("first")
    (root / "tree/work/b.txt").write_text("second")
    index.scan()
    first = index.lookup("tree", "work/a.txt")
    original = DuplicateReview.digest
    reads = []

    def count(self: Any, copy: Any, **kwargs: Any) -> str:
        reads.append(copy.path)
        return original(self, copy, **kwargs)

    monkeypatch.setattr(DuplicateReview, "digest", count)
    with sqlite3.connect(database) as connection:
        events_before = connection.execute(
            "SELECT COUNT(*) FROM document_events"
        ).fetchone()[0]
    index.scan()
    assert reads == []
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM document_events").fetchone()[0]
            == events_before
        )
    (root / "tree/work/a.txt").write_text("edited")
    index.scan()
    assert reads == ["work/a.txt"]
    second = index.lookup("tree", "work/a.txt")
    assert second["document_id"] != first["document_id"]
    assert second["reason"] == "content_changed" and not second["reviewed"]
    assert client.get(P + "/review", params={"unreviewed": True}).json()["total"] == 2


def test_manual_sort_is_reviewed_and_optional_directory_review_still_lists_it(
    indexed: Any,
) -> None:
    client, index, root, _ = indexed
    row = manual(client, root)
    assert row["training_eligible"] and not row["needs_review"]
    assert row["classification_provenance"] == "manual"
    assert client.get(P + "/review", params={"unreviewed": True}).json()["total"] == 0
    assert (
        client.get(P + "/review", params={"folder": "work", "recursive": False}).json()[
            "total"
        ]
        == 1
    )
    index.scan()
    assert index.lookup("tree", "work/a.txt")["document_id"] == row["document_id"]


def test_edit_retires_old_label_and_confirmation_uses_new_identity(
    indexed: Any,
) -> None:
    client, index, root, _ = indexed
    old = manual(client, root)
    (root / "tree/work/a.txt").write_text("a replacement document")
    index.scan()
    rows = manifest(client)
    historical = next(r for r in rows if r["document_id"] == old["document_id"])
    discovered = next(r for r in rows if r["decision_id"] is None)
    assert historical["file_status"] == "replaced" and historical["file_path"] is None
    assert not historical["training_eligible"]
    assert discovered["document_id"] != old["document_id"]
    assert discovered["needs_review"] and not discovered["training_eligible"]
    assert (
        client.post(
            P + "/review/accept", json=payload(client, "work/a.txt")
        ).status_code
        == 200
    )
    accepted = next(
        r for r in manifest(client) if r["document_id"] == discovered["document_id"]
    )
    assert accepted["training_eligible"] and accepted["replaces_decision_id"] is None
    assert client.post(P + "/undo").status_code == 200
    assert (root / "tree/work/a.txt").read_text() == "a replacement document"
    assert client.post(P + "/undo").status_code == 409


def test_metadata_only_change_keeps_identity_and_human_review(indexed: Any) -> None:
    client, index, root, _ = indexed
    old = manual(client, root)
    path = root / "tree/work/a.txt"
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
    index.scan()
    current = index.lookup("tree", "work/a.txt")
    assert current["document_id"] == old["document_id"] and current["reviewed"]
    assert manifest(client)[0]["training_eligible"]


def test_external_move_and_copy_are_new_discoveries_not_inferred_ids(
    indexed: Any,
) -> None:
    client, index, root, _ = indexed
    old = manual(client, root)
    source = root / "tree/work/a.txt"
    source.rename(root / "tree/school/moved.txt")
    index.scan()
    assert not index.lookup("tree", "work/a.txt")["present"]
    moved = index.lookup("tree", "school/moved.txt")
    assert moved["document_id"] != old["document_id"] and not moved["reviewed"]
    (root / "tree/work/copy.txt").write_bytes(
        (root / "tree/school/moved.txt").read_bytes()
    )
    index.scan()
    copied = index.lookup("tree", "work/copy.txt")
    assert copied["document_id"] != moved["document_id"]
    result = client.get(
        P + "/duplicates", params={"scope": "tree", "indexed": True}
    ).json()
    assert result["complete"] and len(result["groups"]) == 1


def test_internal_reclassification_folder_move_and_undo_keep_identity(
    indexed: Any,
) -> None:
    client, index, root, _ = indexed
    old = manual(client, root)
    assert (
        client.post(
            P + "/reclassify",
            json={
                **payload(client, "work/a.txt"),
                "folder": "school",
                "filename": "b.txt",
            },
        ).status_code
        == 200
    )
    assert index.lookup("tree", "school/b.txt")["document_id"] == old["document_id"]
    assert index.lookup("tree", "work/a.txt") is None
    assert (
        client.post(
            P + "/folders/move",
            json={"path": "school", "parent": "work", "name": "moved"},
        ).status_code
        == 200
    )
    assert index.lookup("tree", "work/moved/b.txt")["document_id"] == old["document_id"]
    assert client.post(P + "/undo").status_code == 200
    assert index.lookup("tree", "work/a.txt")["document_id"] == old["document_id"]
    assert index.lookup("tree", "work/a.txt")["reviewed"]


def test_directory_scope_and_pagination_preserve_all_files_option(indexed: Any) -> None:
    client, index, root, _ = indexed
    manual(client, root)
    (root / "tree/work/nested/child.txt").write_text("child")
    index.scan()
    direct = client.get(
        P + "/review", params={"folder": "work", "recursive": False}
    ).json()
    assert [r["path"] for r in direct["entries"]] == ["work/a.txt"]
    unfolded = client.get(
        P + "/review", params={"folder": "work", "recursive": True}
    ).json()
    assert unfolded["total"] == 2
    pending = client.get(
        P + "/review", params={"folder": "work", "recursive": True, "unreviewed": True}
    ).json()
    assert [r["path"] for r in pending["entries"]] == ["work/nested/child.txt"]


def test_failed_scan_cannot_declare_missing_files(
    indexed: Any, monkeypatch: Any
) -> None:
    _, index, root, _ = indexed
    (root / "tree/work/a.txt").write_text("exists")
    index.scan()
    completed = index.status()["last_completed_at"]
    (root / "tree/work/a.txt").unlink()

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise LibraryError("simulated NAS outage", 409)

    monkeypatch.setattr(DuplicateReview, "candidates", fail)
    index.scan()
    assert index.lookup("tree", "work/a.txt")["present"]
    assert index.status()["last_completed_at"] == completed
    assert not index.status()["complete"] and index.status()["error"]


@pytest.mark.parametrize("legacy_hash", ["matching", "different", None])
def test_legacy_bootstrap_preserves_decisions_and_detects_known_replacement(
    indexed: Any,
    legacy_hash: str | None,
) -> None:
    client, index, root, database = indexed
    (root / "tree/work/a.txt").write_text("current")
    digest = (
        hashlib.sha256(b"current").hexdigest()
        if legacy_hash == "matching"
        else legacy_hash
    )
    repo = SorterRepository(database)
    decision = repo.record(
        project_id=1,
        target="tree",
        action="sort",
        kind="file",
        original_name="old.txt",
        final_name="a.txt",
        label="work",
        destination="work/a.txt",
        sha256=digest,
        size=7,
    )
    with sqlite3.connect(database) as connection:
        # Reproduce a schema-4 database: new derived state did not exist yet.
        connection.execute("DELETE FROM file_index")
        connection.execute("DELETE FROM document_events")
        connection.execute("PRAGMA user_version=4")
        before = connection.execute("SELECT * FROM decisions").fetchall()
    repo.initialize()
    index.scan()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM decisions").fetchall() == before
    current = index.lookup("tree", "work/a.txt")
    if legacy_hash == "different":
        assert (
            current["document_id"] != decision.document_id and not current["reviewed"]
        )
    else:
        assert current["document_id"] == decision.document_id and current["reviewed"]
    assert client.get(P + "/index").json()["complete"]


def test_refresh_is_nonblocking_and_indexed_lookup_reads_no_content(
    indexed: Any, monkeypatch: Any
) -> None:
    client, index, root, _ = indexed
    (root / "tree/work/a.txt").write_text("same")
    (root / "tree/work/b.txt").write_text("same")
    index.scan()

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("indexed request must not hash content")

    monkeypatch.setattr(DuplicateReview, "digest", fail)
    assert client.post(P + "/index/refresh").status_code == 202
    result = client.get(
        P + "/duplicates", params={"indexed": True, "scope": "tree"}
    ).json()
    assert len(result["groups"]) == 1
    assert client.get(
        P + "/duplicate", params={"indexed": True, "area": "tree", "path": "work/a.txt"}
    ).json()["copies"]


def test_full_audit_detects_content_change_with_preserved_metadata(
    indexed: Any,
) -> None:
    client, index, root, _ = indexed
    old = manual(client, root)
    path = root / "tree/work/a.txt"
    stat = path.stat()
    path.write_bytes(b"X" * stat.st_size)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    index.scan(verify_all=True)
    assert index.lookup("tree", "work/a.txt")["document_id"] != old["document_id"]
    assert client.get(P + "/review", params={"unreviewed": True}).json()["total"] == 1


def test_shared_tree_is_hashed_once(indexed: Any, monkeypatch: Any) -> None:
    client, index, root, _ = indexed
    (root / "other-dump").mkdir()
    (root / "tree/work/a.txt").write_text("unique")
    assert (
        client.post(
            "/api/projects",
            json={
                "name": "Other",
                "source": "other-dump",
                "target": "tree",
                "mode": "files",
            },
        ).status_code
        == 201
    )
    original = DuplicateReview.digest
    reads = []

    def count(self: Any, copy: Any, **kwargs: Any) -> str:
        reads.append(copy.path)
        return original(self, copy, **kwargs)

    monkeypatch.setattr(DuplicateReview, "digest", count)
    index.scan()
    assert reads == ["work/a.txt"]


def test_scan_resumes_after_changed_file_and_does_not_publish_a_partial_hash(
    indexed: Any,
    monkeypatch: Any,
) -> None:
    _, index, root, _ = indexed
    (root / "tree/work/a.txt").write_text("stable")
    changing = root / "tree/work/b.txt"
    changing.write_text("before")
    original = DuplicateReview.digest

    def change(self: Any, copy: Any, **kwargs: Any) -> str:
        digest = original(self, copy, **kwargs)
        if copy.path == "work/b.txt":
            changing.write_text("edited during hashing")
        return digest

    monkeypatch.setattr(DuplicateReview, "digest", change)
    index.scan()
    assert index.lookup("tree", "work/a.txt")["verified"]
    assert index.lookup("tree", "work/b.txt") is None
    assert not index.status()["complete"]
    monkeypatch.setattr(DuplicateReview, "digest", original)
    index.scan()
    assert index.status()["complete"]
    assert (
        index.lookup("tree", "work/b.txt")["sha256"]
        == hashlib.sha256(changing.read_bytes()).hexdigest()
    )


def test_scan_retries_after_sorter_mutation_without_marking_missing(
    indexed: Any,
    monkeypatch: Any,
) -> None:
    client, index, root, database = indexed
    old = manual(client, root)
    index.scan()
    (root / "tree/work/new.txt").write_text("new")
    original = DuplicateReview.digest

    def mutate(self: Any, copy: Any, **kwargs: Any) -> str:
        digest = original(self, copy, **kwargs)
        (root / "tree/work/a.txt").rename(root / "tree/school/a.txt")
        SorterRepository(database).record_folder_move(
            "tree", "work/a.txt", "school/a.txt"
        )
        return digest

    monkeypatch.setattr(DuplicateReview, "digest", mutate)
    index.scan()
    assert index.lookup("tree", "school/a.txt")["document_id"] == old["document_id"]
    assert index.lookup("tree", "school/a.txt")["present"]
    # Sorting during a scan no longer restarts it (it could then never finish
    # while the owner sorts); the moved file is checked on disk, not missing.
    assert index.status()["complete"]


def test_duplicate_keeper_index_survives_adopting_another_copys_name_and_undo(
    indexed: Any,
) -> None:
    client, index, root, _ = indexed
    (root / "tree/work/a.txt").write_text("same")
    (root / "tree/work/b.txt").write_text("same")
    index.scan()
    old_ids = {
        p: index.lookup("tree", p)["document_id"] for p in ("work/a.txt", "work/b.txt")
    }
    group = client.get(
        P + "/duplicates", params={"indexed": True, "scope": "tree"}
    ).json()["groups"][0]
    group["copies"] = [
        {key: value for key, value in copy.items() if key != "name"}
        for copy in group["copies"]
    ]
    response = client.post(
        P + "/duplicates/resolve",
        json={
            **group,
            "keeper": 1,
            "folder": "work",
            "filename": "a.txt",
            "scope": "tree",
        },
    )
    assert response.status_code == 200, response.text
    assert index.lookup("tree", "work/a.txt")["document_id"] == old_ids["work/b.txt"]
    kept = next(row for row in manifest(client) if row["label"] == "work")
    assert kept["training_eligible"] and not kept["needs_review"]
    assert client.post(P + "/undo").status_code == 200
    for path, identity in old_ids.items():
        assert index.lookup("tree", path)["document_id"] == identity
        assert not index.lookup("tree", path)["reviewed"]


def test_new_project_never_reports_a_complete_index_before_first_scan(
    indexed: Any,
) -> None:
    client, index, root, _ = indexed
    index.scan()
    (root / "new-dump").mkdir()
    (root / "new-tree").mkdir()
    project = client.post(
        "/api/projects",
        json={"name": "New", "source": "new-dump", "target": "new-tree", "mode": "top"},
    ).json()
    assert not client.get(f"/api/projects/{project['id']}/index").json()["complete"]
    index.scan()
    assert client.get(f"/api/projects/{project['id']}/index").json()["complete"]


def test_index_worker_does_not_block_reads_while_hashing(
    indexed: Any, monkeypatch: Any
) -> None:
    client, index, root, _ = indexed
    (root / "tree/work/a.txt").write_text("content")
    reading, release = threading.Event(), threading.Event()
    original = DuplicateReview.digest

    def slow(self: Any, copy: Any, **kwargs: Any) -> str:
        reading.set()
        assert release.wait(3)
        return original(self, copy, **kwargs)

    monkeypatch.setattr(DuplicateReview, "digest", slow)
    START_INDEX(index)
    try:
        assert reading.wait(3)
        assert client.get(P + "/index").json()["running"]
        assert (
            client.get(P + "/duplicates", params={"indexed": True}).status_code == 200
        )
        assert client.get(P + "/review").json()["total"] == 1
    finally:
        release.set()
        index.stop()


def test_index_routes_require_administrator(tmp_path: Path) -> None:
    root = tmp_path / "library"
    (root / "dump").mkdir(parents=True)
    (root / "tree").mkdir()
    app = main.create_app(
        tmp_path / "db",
        library_root=root,
        seed=Seed("Private", "dump", "tree"),
        admin_account_id="admin",
        session_validator=lambda s: main.Identity("member", "member", "MEMBER"),
    )
    with TestClient(app) as client:
        headers = {
            "Tailscale-User-Login": "owner@example.test",
            "Cookie": "home_platform_dashboard=valid.sig",
        }
        assert client.get(P + "/index").status_code == 401
        assert client.get(P + "/index", headers=headers).status_code == 403
        assert client.post(P + "/index/refresh", headers=headers).status_code == 403


def test_save_during_quiet_interval_is_not_indexed(
    indexed: Any, monkeypatch: Any
) -> None:
    _, index, root, _ = indexed
    path = root / "tree/work/a.txt"
    path.write_text("being saved")

    def save(timeout: float) -> bool:
        path.write_text("save still in progress")
        return False

    monkeypatch.setattr(index._stop, "wait", save)
    index.scan()
    assert index.lookup("tree", "work/a.txt") is None
    assert not index.status()["complete"]

"""Regression tests for the 2 October 2026 edge-case audit.

Each test reproduces a failure the audit found, and now asserts the fixed
behaviour. Scenarios use throwaway libraries only.
"""

from __future__ import annotations

import errno
import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.file_sorter import main
from services.file_sorter.duplicates import DuplicateReview
from services.file_sorter.index import FileIndex
from services.file_sorter.library import LibraryRoot, file_sha256
from services.file_sorter.repository import Seed, SorterRepository

P = "/api/projects/1"
Env = tuple[TestClient, FileIndex, Path, Path]


@pytest.fixture(autouse=True)
def deterministic_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FileIndex, "start", lambda self: None)
    monkeypatch.setattr(FileIndex, "settle_seconds", 0.0)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    root = tmp_path / "library"
    (root / "dump").mkdir(parents=True)
    (root / "tree/work").mkdir(parents=True)
    (root / "tree/school").mkdir()
    database = tmp_path / "db"
    app = main.create_app(
        database,
        library_root=root,
        seed=Seed("Index", "dump", "tree"),
        allow_dev_identity=True,
        background_index=False,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, app.state.index, root, database


def entry(client: TestClient, path: str, area: str = "dump") -> dict[str, Any]:
    route = "/sorted-entry" if area == "tree" else "/current"
    response = client.get(P + route, params={"path": path})
    assert response.status_code == 200, response.text
    found = response.json()["entry"]
    return {key: found[key] for key in ("path", "size", "mtime_ns")}


def sort(client: TestClient, root: Path, name: str, folder: str) -> None:
    (root / "dump" / name).write_text(f"human labelled content {name}")
    # New dump files appear after the queue cache window, or on Rescan.
    client.post(P + "/rescan")
    response = client.post(
        P + "/classify",
        json={**entry(client, name), "folder": folder, "filename": name},
    )
    assert response.status_code == 200, response.text


def labels(database: Path, target: str = "tree") -> list[str]:
    return [d.destination for d in SorterRepository(database).tree_decisions(target)]


def manifest(client: TestClient) -> list[dict[str, Any]]:
    response = client.get(P + "/labels.jsonl")
    return [json.loads(line) for line in response.text.splitlines()]


# ---------- 1. folder moves never split the disk from the labels ----------


def test_folder_move_onto_a_stale_index_location_succeeds(env: Env) -> None:
    client, index, root, database = env
    (root / "tree/b").mkdir()
    (root / "tree/b/a.txt").write_text("old b content")
    index.scan()
    # The owner deletes folder b in Finder, then sorts a.txt into work.
    (root / "tree/b/a.txt").unlink()
    (root / "tree/b").rmdir()
    index.scan()
    sort(client, root, "a.txt", "work")

    response = client.post(
        P + "/folders/move", json={"path": "work", "parent": "", "name": "b"}
    )
    assert response.status_code == 200, response.text
    assert (root / "tree/b/a.txt").read_text() == "human labelled content a.txt"
    assert labels(database) == ["b/a.txt"]
    with sqlite3.connect(database) as connection:
        moves = connection.execute("SELECT old_path, new_path FROM folder_moves")
        assert moves.fetchall() == [("work", "b")]
        events = connection.execute(
            "SELECT event FROM document_events WHERE event = 'location_reused'"
        )
        assert events.fetchall() == [("location_reused",)]
    (row,) = [r for r in manifest(client) if r["label_source"] == "owner_sorted"]
    assert (row["label"], row["file_status"]) == ("b", "present")


def test_failed_folder_move_log_moves_the_folder_back(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, root, database = env
    sort(client, root, "a.txt", "work")

    def broken(*args: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SorterRepository, "record_folder_move", broken)
    response = client.post(
        P + "/folders/move", json={"path": "work", "parent": "", "name": "jobs"}
    )
    assert response.status_code == 500
    assert "moved back" in response.json()["detail"]
    assert (root / "tree/work/a.txt").exists()
    assert not (root / "tree/jobs").exists()
    assert labels(database) == ["work/a.txt"]


def test_failed_group_log_reports_progress_and_restores_the_folder(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, root, database = env
    sort(client, root, "a.txt", "work")

    def broken(*args: Any) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(SorterRepository, "record_folder_move", broken)
    response = client.post(
        P + "/folders/group", json={"paths": ["work"], "name": "jobs"}
    )
    assert response.status_code == 500
    assert "moved 0 of 1" in response.json()["detail"]
    assert (root / "tree/work/a.txt").exists()
    assert labels(database) == ["work/a.txt"]


# ---------- 2/3. Undo reverses exactly what a notification reported ----------


def test_undo_with_an_expected_decision_refuses_anything_newer(env: Env) -> None:
    client, _, root, database = env
    sort(client, root, "a.txt", "work")
    first = SorterRepository(database).last_active(1)
    assert first is not None
    sort(client, root, "b.txt", "school")
    response = client.post(P + "/undo", json={"expect_decision_id": first.id})
    assert response.status_code == 409
    assert "no longer the latest" in response.json()["detail"]
    assert (root / "tree/school/b.txt").exists()
    latest = SorterRepository(database).last_active(1)
    assert latest is not None
    response = client.post(P + "/undo", json={"expect_decision_id": latest.id})
    assert response.status_code == 200
    assert (root / "dump/b.txt").exists()
    # Plain Undo, with or without a body, still steps back from the latest.
    assert client.post(P + "/undo").status_code == 200
    assert (root / "dump/a.txt").exists()


def test_discard_whose_log_write_fails_returns_the_file(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, root, _ = env
    (root / "dump/junk.txt").write_text("junk")
    client.post(P + "/rescan")
    guard = entry(client, "junk.txt")

    def broken(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SorterRepository, "record", broken)
    response = client.post(P + "/discard", json=guard)
    assert response.status_code == 500
    assert (root / "dump/junk.txt").read_text() == "junk"
    assert not (root / "tree/_discarded/junk.txt").exists()


# ---------- 4/5. the index never mistakes "unreadable" for "deleted" ----------

needs_permissions = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores file permissions",
)


def files_project(tmp_path: Path) -> tuple[FileIndex, Path]:
    root = tmp_path / "library"
    (root / "dump2/sub").mkdir(parents=True)
    (root / "tree2").mkdir()
    (root / "dump2/sub/f.txt").write_text("in a subfolder")
    (root / "dump2/top.txt").write_text("at the top")
    repository = SorterRepository(tmp_path / "db")
    repository.initialize()
    repository.create_project("Phone", "dump2", "tree2", "files")
    return FileIndex(repository, LibraryRoot(root)), root


@needs_permissions
def test_unreadable_dump_subfolder_keeps_its_files_and_their_identity(
    tmp_path: Path,
) -> None:
    index, root = files_project(tmp_path)
    index.scan()
    before = index.lookup("dump2", "sub/f.txt")
    assert before is not None and before["present"]
    os.chmod(root / "dump2/sub", 0)
    try:
        index.scan()
        during = index.lookup("dump2", "sub/f.txt")
        status = index.status()
    finally:
        os.chmod(root / "dump2/sub", 0o755)
    assert during is not None and during["present"] == 1
    assert status["error"] and "skipped" in status["error"]
    assert not status["complete"]
    index.scan()
    after = index.lookup("dump2", "sub/f.txt")
    assert after is not None and after["document_id"] == before["document_id"]
    assert index.status()["error"] is None


@needs_permissions
def test_unreadable_dump_root_marks_nothing_missing(tmp_path: Path) -> None:
    index, root = files_project(tmp_path)
    index.scan()
    os.chmod(root / "dump2", 0)
    try:
        index.scan()
    finally:
        os.chmod(root / "dump2", 0o755)
    for path in ("sub/f.txt", "top.txt"):
        row = index.lookup("dump2", path)
        assert row is not None and row["present"] == 1


@needs_permissions
def test_one_unreadable_file_does_not_stop_the_rest(env: Env) -> None:
    _, index, root, _ = env
    (root / "tree/work/a.txt").write_text("a")
    (root / "tree/work/z.txt").write_text("z")
    os.chmod(root / "tree/work/a.txt", 0)
    try:
        index.scan()
        status = index.status()
    finally:
        os.chmod(root / "tree/work/a.txt", 0o644)
    assert index.lookup("tree", "work/z.txt") is not None
    assert status["error"] and "work/a.txt" in status["error"]
    index.scan()
    assert index.lookup("tree", "work/a.txt") is not None
    assert index.status()["error"] is None


def test_a_file_being_written_is_retried_soon_without_blocking_others(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, index, root, _ = env
    (root / "tree/work/live.log").write_text("0")
    (root / "tree/work/z.txt").write_text("z")
    original = file_sha256

    def appends(path: Path, limit: int) -> str | None:
        digest = original(path, limit)
        if path.name == "live.log":
            with path.open("a") as stream:
                stream.write("x")
        return digest

    monkeypatch.setattr("services.file_sorter.duplicates.file_sha256", appends)
    index.scan()
    assert index.lookup("tree", "work/z.txt") is not None
    assert index.lookup("tree", "work/live.log") is None
    assert index._retry_soon
    assert "still changing" in (index.status()["error"] or "")


def test_a_project_with_missing_folders_does_not_block_the_others(env: Env) -> None:
    _, index, root, database = env
    (root / "dumpB").mkdir()
    (root / "treeB").mkdir()
    SorterRepository(database).create_project("AAA first", "dumpB", "treeB", "top")
    (root / "tree/work/a.txt").write_text("a")
    (root / "treeB").rmdir()
    index.scan()
    assert index.lookup("tree", "work/a.txt") is not None
    assert "treeB" in (index.status()["error"] or "")
    assert not index.status()["running"]


def test_undecodable_names_are_left_alone_and_the_scan_completes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, index, root, _ = env
    (root / "tree/work/z.txt").write_text("z")
    real_walk = os.walk

    def walk(top: Any, **kwargs: Any) -> Iterator[tuple[str, list[str], list[str]]]:
        for current, dirs, files in real_walk(top, **kwargs):
            if Path(current) == root / "tree/work":
                files = [*files, "bad\udcff.txt"]
            yield current, dirs, files

    monkeypatch.setattr("services.file_sorter.duplicates.os.walk", walk)
    index.scan()
    status = index.status()
    assert not status["running"]
    assert index.lookup("tree", "work/z.txt") is not None


def test_the_index_thread_survives_unexpected_errors(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, index, _, _ = env
    calls: list[int] = []

    def explode(*args: Any, **kwargs: Any) -> None:
        calls.append(1)
        raise RuntimeError("unexpected")

    monkeypatch.setattr(FileIndex, "scan", explode)
    thread = threading.Thread(target=index._run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not index.status()["error"]:
        time.sleep(0.01)
    assert calls and thread.is_alive()
    assert "Unexpected index error" in (index.status()["error"] or "")
    index.stop()
    thread.join(timeout=2)


def test_a_folder_named_stop_is_outside_the_index(env: Env) -> None:
    _, index, root, _ = env
    (root / "tree/__stop__").mkdir()
    (root / "tree/__stop__/x.txt").write_text("x")
    (root / "tree/work/z.txt").write_text("z")
    index.scan()
    assert index.status()["error"] is None
    assert index.lookup("tree", "work/z.txt") is not None
    assert index.lookup("tree", "__stop__/x.txt") is None


# ---------- reclassify corrects the decision about this document ----------


def test_reclassify_links_to_the_decision_for_this_document(env: Env) -> None:
    client, index, root, database = env
    sort(client, root, "x.txt", "work")
    first = SorterRepository(database).last_active(1)
    index.scan()
    (root / "tree/work/x.txt").unlink()  # the owner deletes it on the NAS
    index.scan()
    (root / "dump/x.txt").write_text("a different document")
    client.post(P + "/rescan")
    response = client.post(
        P + "/classify",
        json={**entry(client, "x.txt"), "folder": "work", "filename": "x.txt"},
    )
    assert response.status_code == 200, response.text
    second = response.json()["decision_id"]
    response = client.post(
        P + "/reclassify",
        json={
            **entry(client, "work/x.txt", "tree"),
            "folder": "school",
            "filename": "x.txt",
        },
    )
    assert response.status_code == 200, response.text
    rows = [r for r in manifest(client) if r["label_source"] != "pre_existing"]
    corrected = next(r for r in rows if r["label"] == "school")
    assert corrected["replaces_decision_id"] == second
    assert corrected["source_path"] == "x.txt"
    assert corrected["project"] == "Index"
    # The corrected document now has exactly one active label.
    same_document = [r for r in rows if r["document_id"] == corrected["document_id"]]
    assert [r["label"] for r in same_document] == ["school"]
    assert first is not None and first.id != second


# ---------- names the sorter would hide are refused up front ----------


@pytest.mark.parametrize(
    "name", ["#2026", ".work", "@eaDir", "_discarded", "x.tmp", "notes.part"]
)
def test_new_folders_must_stay_visible(env: Env, name: str) -> None:
    client, _, root, _ = env
    response = client.post(P + "/folders", json={"parent": "", "name": name})
    assert response.status_code == 400, response.text
    assert not (root / "tree" / name).exists()


@pytest.mark.parametrize("parent", ["_discarded", "#recycle", ".hidden"])
def test_folders_cannot_be_created_or_moved_into_hidden_places(
    env: Env, parent: str
) -> None:
    client, _, root, _ = env
    (root / "tree" / parent).mkdir()
    response = client.post(P + "/folders", json={"parent": parent, "name": "x"})
    assert response.status_code in {400, 404}
    response = client.post(
        P + "/folders/move", json={"path": "work", "parent": parent, "name": "work"}
    )
    assert response.status_code in {400, 404}
    assert (root / "tree/work").is_dir()


@pytest.mark.parametrize("filename", [".secret.pdf", "notes.tmp", "Thumbs.db"])
def test_sorted_filenames_must_stay_visible(env: Env, filename: str) -> None:
    client, _, root, _ = env
    (root / "dump/a.pdf").write_text("a")
    client.post(P + "/rescan")
    response = client.post(
        P + "/classify",
        json={**entry(client, "a.pdf"), "folder": "work", "filename": filename},
    )
    assert response.status_code == 400
    assert (root / "dump/a.pdf").exists()


@pytest.mark.parametrize("folder", [".", "WORK", "_routing", "#recycle"])
def test_classify_needs_an_exact_category_folder(env: Env, folder: str) -> None:
    client, _, root, database = env
    (root / "tree/_routing").mkdir()
    (root / "tree/#recycle").mkdir()
    (root / "dump/a.pdf").write_text("a")
    client.post(P + "/rescan")
    response = client.post(
        P + "/classify",
        json={**entry(client, "a.pdf"), "folder": folder, "filename": "a.pdf"},
    )
    assert response.status_code in {400, 404}, response.text
    assert (root / "dump/a.pdf").exists()
    assert labels(database) == []


# ---------- queue heads that vanish, and empty files ----------


def test_a_queue_head_moved_outside_the_sorter_is_skipped(env: Env) -> None:
    client, _, root, _ = env
    for name in ("a.txt", "b.txt"):
        (root / "dump" / name).write_text(name)
    client.post(P + "/rescan")
    assert client.get(P + "/current").json()["entry"]["path"] == "a.txt"
    (root / "dump/a.txt").rename(root / "tree/work/a.txt")  # moved in Finder
    payload = client.get(P + "/current").json()
    assert payload["entry"]["path"] == "b.txt"
    assert payload["progress"]["remaining"] == 1
    # An explicitly requested entry that vanished is still a clear 404.
    assert client.get(P + "/current", params={"path": "a.txt"}).status_code == 404


def test_empty_files_are_sorted_one_at_a_time(env: Env) -> None:
    client, index, root, _ = env
    (root / "tree/work/__init__.py").write_text("")
    (root / "dump/__init__.py").write_text("")
    client.post(P + "/rescan")
    index.scan()
    response = client.post(
        P + "/classify",
        json={
            **entry(client, "__init__.py"),
            "folder": "school",
            "filename": "__init__.py",
        },
    )
    assert response.status_code == 200, response.text
    groups = client.get(
        P + "/duplicates", params={"indexed": True, "scope": "tree"}
    ).json()["groups"]
    assert groups == []


# ---------- sorting checks duplicates without walking the NAS ----------


def test_classify_uses_the_index_and_rechecks_candidates_on_disk(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, index, root, _ = env
    (root / "tree/work/kept.txt").write_text("same bytes")
    (root / "tree/work/gone.txt").write_text("other bytes")
    (root / "dump/copy.txt").write_text("same bytes")
    (root / "dump/fresh.txt").write_text("other bytes")
    index.scan()
    (root / "tree/work/gone.txt").unlink()  # deleted since the last scan
    client.post(P + "/rescan")

    def no_walk(*args: Any, **kwargs: Any) -> list[Any]:
        raise AssertionError("classify walked the library")

    monkeypatch.setattr(DuplicateReview, "candidates", no_walk)
    blocked = client.post(
        P + "/classify",
        json={**entry(client, "copy.txt"), "folder": "school", "filename": "copy.txt"},
    )
    assert blocked.status_code == 409
    assert "Identical copies" in blocked.json()["detail"]
    allowed = client.post(
        P + "/classify",
        json={
            **entry(client, "fresh.txt"),
            "folder": "school",
            "filename": "fresh.txt",
        },
    )
    assert allowed.status_code == 200, allowed.text


# ---------- a stalled NAS is not "ready" ----------


def test_ready_reports_a_stalled_library(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _, _ = env
    assert client.get("/ready").status_code == 200
    release = threading.Event()
    real_listdir = os.listdir

    def stalled(path: Any) -> list[str]:
        if str(path).endswith("library"):
            release.wait(5)
        return real_listdir(path)

    monkeypatch.setattr(main, "READY_PROBE_SECONDS", 0.2)
    monkeypatch.setattr("services.file_sorter.main.os.listdir", stalled)
    try:
        assert client.get("/ready").status_code == 503
        # While the first probe is still stuck, no second thread is started.
        assert client.get("/ready").status_code == 503
        # Routine container liveness must stay independent of NAS access.
        assert client.get("/health").status_code == 200
        assert client.get("/health").json()["status"] == "healthy"
    finally:
        release.set()


# ---------- an upgrade baseline is not verified content ----------


def test_unhashed_legacy_decision_is_kept_but_not_training_eligible(
    env: Env,
) -> None:
    client, index, root, database = env
    sort(client, root, "big.bin", "work")
    with sqlite3.connect(database) as connection:
        # A schema-4 row for a file too large to have been hashed.
        connection.execute("UPDATE decisions SET sha256=NULL, file_mtime_ns=NULL")
        connection.execute("DELETE FROM file_index")
        connection.execute("DELETE FROM document_events")
        connection.execute("DELETE FROM index_roots")
        connection.execute("PRAGMA user_version=4")
    (root / "tree/work/big.bin").write_text("replaced by someone else")
    SorterRepository(database).initialize()
    index.scan()
    (row,) = [r for r in manifest(client) if r["label_source"] == "owner_sorted"]
    assert row["label"] == "work"
    assert row["classification_provenance"] == "manual"
    assert not row["needs_review"]
    assert not row["content_verified"]
    assert not row["training_eligible"]
    # Accepting it in review records today's bytes as the labelled content.
    response = client.post(
        P + "/review/accept", json=entry(client, "work/big.bin", "tree")
    )
    assert response.status_code == 200, response.text
    (row,) = [r for r in manifest(client) if r["label_source"] != "pre_existing"]
    assert row["content_verified"] and row["training_eligible"]


def test_overlong_paths_are_a_clear_400(env: Env) -> None:
    client, _, _, _ = env
    response = client.get(P + "/raw", params={"path": "a" * 2000, "area": "tree"})
    assert response.status_code == 400


# ---------- 5 October audit: tidying an emptied dump folder is best effort ----------


def test_a_dump_folder_that_cannot_be_tidied_still_logs_the_move(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, root, database = env
    (root / "phone/DCIM").mkdir(parents=True)
    (root / "phone/Camera").mkdir()
    (root / "phone/DCIM/a.jpg").write_bytes(b"jpeg a")
    (root / "phone/Camera/b.jpg").write_bytes(b"jpeg b")
    (root / "album/photos").mkdir(parents=True)
    created = client.post(
        "/api/projects",
        json={"name": "Phone", "source": "phone", "target": "album", "mode": "files"},
    )
    assert created.status_code == 201, created.text
    phone = f"/api/projects/{created.json()['id']}"

    def busy(path: Path) -> None:
        raise OSError(errno.ENOTEMPTY, "Directory not empty", str(path))

    monkeypatch.setattr(Path, "rmdir", busy)

    def guard(path: str) -> dict[str, Any]:
        response = client.get(phone + "/current", params={"path": path})
        assert response.status_code == 200, response.text
        found = response.json()["entry"]
        return {key: found[key] for key in ("path", "size", "mtime_ns")}

    sorted_ = client.post(
        phone + "/classify",
        json={**guard("DCIM/a.jpg"), "folder": "photos", "filename": "a.jpg"},
    )
    assert sorted_.status_code == 200, sorted_.text
    assert (root / "album/photos/a.jpg").read_bytes() == b"jpeg a"
    discarded = client.post(phone + "/discard", json=guard("Camera/b.jpg"))
    assert discarded.status_code == 200, discarded.text
    assert (root / "album/_discarded/b.jpg").read_bytes() == b"jpeg b"
    assert not (root / "phone/DCIM/a.jpg").exists()
    assert not (root / "phone/Camera/b.jpg").exists()
    assert (root / "phone/DCIM").is_dir()
    assert (root / "phone/Camera").is_dir()
    rows = (
        sqlite3.connect(database)
        .execute("SELECT action, original_name, destination FROM decisions ORDER BY id")
        .fetchall()
    )
    assert rows == [
        ("sort", "DCIM/a.jpg", "photos/a.jpg"),
        ("discard", "Camera/b.jpg", "_discarded/b.jpg"),
    ]

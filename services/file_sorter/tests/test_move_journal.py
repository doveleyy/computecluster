"""Every rename is journaled before it happens.

A crash between the rename and its log write is settled on the next start by
looking at the disk: the decision is logged when the entry arrived, and the
intent is dropped when the entry never left. Throwaway libraries only.
"""

from __future__ import annotations

import errno
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from services.file_sorter import library as library_module
from services.file_sorter import main
from services.file_sorter.index import FileIndex
from services.file_sorter.repository import Seed, SorterRepository

P = "/api/projects/1"


class Crash(BaseException):
    """A power cut between the rename and the log write: no Python runs after."""


@pytest.fixture(autouse=True)
def deterministic_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FileIndex, "start", lambda self: None)
    monkeypatch.setattr(FileIndex, "settle_seconds", 0.0)


@pytest.fixture
def env(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "library"
    (root / "dump").mkdir(parents=True)
    (root / "tree/work").mkdir(parents=True)
    (root / "tree/school").mkdir()
    return root, tmp_path / "db"


def app_for(root: Path, database: Path) -> FastAPI:
    return main.create_app(
        database,
        library_root=root,
        seed=Seed("Journal", "dump", "tree"),
        allow_dev_identity=True,
        background_index=False,
    )


def entry(client: TestClient, path: str, area: str = "dump") -> dict[str, Any]:
    route = "/sorted-entry" if area == "tree" else "/current"
    response = client.get(P + route, params={"path": path})
    assert response.status_code == 200, response.text
    found = response.json()["entry"]
    return {key: found[key] for key in ("path", "size", "mtime_ns")}


def sort(client: TestClient, root: Path, name: str, folder: str) -> None:
    (root / "dump" / name).write_text(f"human labelled content {name}")
    client.post(P + "/rescan")
    response = client.post(
        P + "/classify",
        json={**entry(client, name), "folder": folder, "filename": name},
    )
    assert response.status_code == 200, response.text


def labels(database: Path) -> list[tuple[str, str]]:
    return [
        (d.action, d.destination)
        for d in SorterRepository(database).tree_decisions("tree")
    ]


def pending(database: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM duplicate_resolutions "
                "WHERE completed_at IS NULL AND cancelled_at IS NULL"
            )
        ]


def dead(*args: Any, **kwargs: Any) -> None:
    raise Crash()


def kill_during(
    app: FastAPI, database: Path, route: str, payload: dict[str, Any]
) -> None:
    """Run the route in-process so the crash stops it dead, as a power cut would.

    The HTTP test client turns a BaseException into a cancelled portal and
    keeps running, so the route function is called directly instead.
    """
    endpoint = next(
        r.endpoint
        for r in app.routes
        if isinstance(r, APIRoute) and r.path == "/api/projects/{project_id}" + route
    )
    project = SorterRepository(database).project(1)
    assert project is not None
    with pytest.raises(Crash):
        if route == "/undo":
            endpoint(project)
        else:
            endpoint(MODELS[route](**payload), project)


MODELS: dict[str, Any] = {
    "/classify": main.ClassifyRequest,
    "/discard": main.EntryAction,
    "/reclassify": main.ReclassifyRequest,
    "/folders/move": main.FolderMove,
}

# Each path: how to reach the rename, which log write the crash interrupts,
# where the entry is afterwards, and what the log must say once settled.
Path_ = tuple[
    Callable[[TestClient, Path], tuple[str, dict[str, Any]]],
    str,
    str,
    list[tuple[str, str]],
]


def classify_path(client: TestClient, root: Path) -> tuple[str, dict[str, Any]]:
    (root / "dump/a.txt").write_text("human labelled content a.txt")
    client.post(P + "/rescan")
    return "/classify", {
        **entry(client, "a.txt"),
        "folder": "work",
        "filename": "a.txt",
    }


def discard_path(client: TestClient, root: Path) -> tuple[str, dict[str, Any]]:
    (root / "dump/a.txt").write_text("human labelled content a.txt")
    client.post(P + "/rescan")
    return "/discard", entry(client, "a.txt")


def reclassify_path(client: TestClient, root: Path) -> tuple[str, dict[str, Any]]:
    sort(client, root, "a.txt", "work")
    return "/reclassify", {
        **entry(client, "work/a.txt", "tree"),
        "folder": "school",
        "filename": "a.txt",
    }


def folder_move_path(client: TestClient, root: Path) -> tuple[str, dict[str, Any]]:
    sort(client, root, "a.txt", "work")
    return "/folders/move", {"path": "work", "parent": "", "name": "jobs"}


def undo_path(client: TestClient, root: Path) -> tuple[str, dict[str, Any]]:
    sort(client, root, "a.txt", "work")
    return "/undo", {}


PATHS: dict[str, Path_] = {
    "classify": (classify_path, "record", "tree/work/a.txt", [("sort", "work/a.txt")]),
    "discard": (
        discard_path,
        "record",
        "tree/_discarded/a.txt",
        [("discard", "_discarded/a.txt")],
    ),
    "reclassify": (
        reclassify_path,
        "record",
        "tree/school/a.txt",
        [("sort", "school/a.txt")],
    ),
    "folder_move": (
        folder_move_path,
        "record_folder_move",
        "tree/jobs/a.txt",
        [("sort", "jobs/a.txt")],
    ),
    "undo": (undo_path, "mark_undone", "dump/a.txt", []),
}


@pytest.mark.parametrize("path", sorted(PATHS))
def test_crash_after_rename_is_logged_on_next_start(
    env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    root, database = env
    reach, log_write, arrived, expected = PATHS[path]
    app = app_for(root, database)
    with TestClient(app) as client:
        route, payload = reach(client, root)
        before = labels(database)
        monkeypatch.setattr(SorterRepository, log_write, dead)
        kill_during(app, database, route, payload)
    monkeypatch.undo()
    assert (root / arrived).exists(), "the rename happened before the crash"
    assert labels(database) == before, "nothing was logged before the crash"

    with TestClient(app_for(root, database)) as client:
        assert labels(database) == expected
        assert (root / arrived).exists()
        assert pending(database) == []
        assert client.get("/ready").status_code == 200
        assert client.post(P + "/rescan").status_code == 200
    if path == "folder_move":
        with sqlite3.connect(database) as connection:
            moves = connection.execute("SELECT old_path, new_path FROM folder_moves")
            assert moves.fetchall() == [("work", "jobs")]


def test_intent_without_a_rename_is_dropped_on_next_start(
    env: tuple[Path, Path],
) -> None:
    root, database = env
    (root / "dump/a.txt").write_text("human labelled content a.txt")
    with TestClient(app_for(root, database)):
        pass
    stat = (root / "dump/a.txt").stat()
    plan = {
        "action": "classify",
        "source": "dump",
        "move": {
            "from_area": "dump",
            "from_path": "a.txt",
            "to_area": "tree",
            "to_path": "work/a.txt",
            "kind": "file",
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        },
        "record": {
            "project_id": 1,
            "target": "tree",
            "action": "sort",
            "kind": "file",
            "original_name": "a.txt",
            "final_name": "a.txt",
            "label": "work",
            "destination": "work/a.txt",
            "sha256": None,
            "size": stat.st_size,
            "review_type": "manual",
            "file_mtime_ns": str(stat.st_mtime_ns),
        },
    }
    SorterRepository(database).prepare_duplicates(1, "tree", "", json.dumps(plan))

    with TestClient(app_for(root, database)) as client:
        assert (root / "dump/a.txt").exists()
        assert not (root / "tree/work/a.txt").exists()
        assert labels(database) == []
        assert pending(database) == []
        assert client.post(P + "/rescan").status_code == 200


def test_ambiguous_intent_keeps_blocking_writes(env: tuple[Path, Path]) -> None:
    root, database = env
    (root / "dump/a.txt").write_text("one")
    (root / "tree/work/a.txt").write_text("another")
    with TestClient(app_for(root, database)):
        pass
    plan = {
        "action": "classify",
        "source": "dump",
        "move": {
            "from_area": "dump",
            "from_path": "a.txt",
            "to_area": "tree",
            "to_path": "work/a.txt",
            "kind": "file",
            "size": 3,
            "mtime_ns": 0,
        },
        "record": {},
    }
    SorterRepository(database).prepare_duplicates(1, "tree", "", json.dumps(plan))

    with TestClient(app_for(root, database)) as client:
        assert len(pending(database)) == 1
        assert client.get("/ready").status_code == 503
        assert client.post(P + "/rescan").status_code == 409
        assert client.post(P + "/duplicates/recover").status_code == 409
        assert labels(database) == []
        assert (root / "dump/a.txt").read_text() == "one"
        assert (root / "tree/work/a.txt").read_text() == "another"


def test_any_failed_log_write_moves_the_entry_back(
    env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, database = env
    with TestClient(app_for(root, database), raise_server_exceptions=False) as client:
        (root / "dump/a.txt").write_text("human labelled content a.txt")
        client.post(P + "/rescan")
        payload = {**entry(client, "a.txt"), "folder": "work", "filename": "a.txt"}

        def broken(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("not a database error")

        monkeypatch.setattr(SorterRepository, "record", broken)
        response = client.post(P + "/classify", json=payload)
        assert response.status_code == 500
        assert "moved back" in response.json()["detail"]
        assert (root / "dump/a.txt").exists()
        assert not (root / "tree/work/a.txt").exists()
        assert labels(database) == []
        assert pending(database) == []


@pytest.mark.parametrize(
    ("reach", "log_write", "arrived", "named"),
    [
        (folder_move_path, "record_folder_move", "tree/jobs/a.txt", "jobs"),
        (reclassify_path, "record", "tree/school/a.txt", "school/a.txt"),
    ],
    ids=["folder_move", "reclassify"],
)
def test_failed_reversal_reports_both_errors_and_settles_later(
    env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    reach: Callable[[TestClient, Path], tuple[str, dict[str, Any]]],
    log_write: str,
    arrived: str,
    named: str,
) -> None:
    root, database = env
    with TestClient(app_for(root, database), raise_server_exceptions=False) as client:
        route, payload = reach(client, root)
        real_rename = library_module.rename_no_replace
        calls = 0

        def rename(source: Path, target: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError(errno.EIO, "Input/output error", str(source))
            real_rename(source, target)

        def broken(*args: Any, **kwargs: Any) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(library_module, "rename_no_replace", rename)
        monkeypatch.setattr(SorterRepository, log_write, broken)
        response = client.post(P + route, json=payload)
        assert response.status_code == 500
        detail = response.json()["detail"]
        assert "could not be logged" in detail
        assert "database is locked" in detail
        assert "Input/output error" in detail
        assert f"it is now at {named}" in detail
        assert (root / arrived).exists()
        assert len(pending(database)) == 1
    monkeypatch.undo()

    with TestClient(app_for(root, database)) as client:
        assert (root / arrived).exists()
        assert pending(database) == []
        assert [d for _, d in labels(database)] == [arrived.split("/", 1)[1]]
        assert client.post(P + "/rescan").status_code == 200

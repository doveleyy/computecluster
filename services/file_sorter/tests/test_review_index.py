"""Sorted review lists the tree from the index instead of walking the NAS."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.file_sorter import main
from services.file_sorter.duplicates import DuplicateReview
from services.file_sorter.index import FileIndex
from services.file_sorter.repository import Seed

P = "/api/projects/1"
FIELDS = ("path", "folder", "reviewed", "document_id", "decision_id")


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[TestClient, FileIndex, Path]]:
    root = tmp_path / "library"
    (root / "dump/album").mkdir(parents=True)
    (root / "tree/work/nested").mkdir(parents=True)
    (root / "tree/school").mkdir()
    app = main.create_app(
        tmp_path / "db",
        library_root=root,
        seed=Seed("Index", "dump", "tree"),
        allow_dev_identity=True,
        background_index=False,
    )
    with TestClient(app) as client:
        yield client, app.state.index, root


def guard(client: TestClient, path: str, area: str) -> dict[str, Any]:
    route = "/sorted-entry" if area == "tree" else "/current"
    entry = client.get(P + route, params={"path": path}).json()["entry"]
    return {key: entry[key] for key in ("path", "size", "mtime_ns")}


def classify(client: TestClient, path: str, folder: str) -> dict[str, Any]:
    response = client.post(
        P + "/classify",
        json={**guard(client, path, "dump"), "folder": folder, "filename": path},
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def review(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    entries = client.get(P + "/review", params=params).json()["entries"]
    return [{key: entry[key] for key in FIELDS} for entry in entries]


def indexed_review(client: TestClient, **params: Any) -> list[str]:
    """Review paths, failing if answering them walked the NAS."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("sorted review walked the NAS")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(DuplicateReview, "candidates", refuse)
        return [entry["path"] for entry in review(client, **params)]


def populate(client: TestClient, root: Path) -> None:
    for name in ("a.txt", "b.txt", "c.txt"):
        (root / "dump" / name).write_text("content " + name)
    (root / "dump/album/photo.txt").write_text("whole folder")
    classify(client, "a.txt", "work")
    classify(client, "b.txt", "school")
    classify(client, "album", "work")  # a folder sorted whole is an item
    response = client.post(P + "/discard", json=guard(client, "c.txt", "dump"))
    assert response.status_code == 200
    (root / "tree/work/nested/found.txt").write_text("already here")
    (root / "tree/work/.hidden").write_text("never listed")


def test_index_lists_exactly_what_the_walk_lists(env: Any) -> None:
    client, index, root = env
    populate(client, root)
    walked = [entry["path"] for entry in review(client)]  # no scan yet: walk
    index.scan()
    for params in (
        {},
        {"folder": "work", "recursive": False},
        {"folder": "work"},
        {"unreviewed": True},
    ):
        from_index = review(client, **params)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(FileIndex, "tree_files", lambda self, project: None)
            assert review(client, **params) == from_index, params
    expected = ["school/b.txt", "work/a.txt", "work/nested/found.txt"]
    assert walked == expected
    assert indexed_review(client) == expected


def test_indexed_review_filters_without_walking(env: Any) -> None:
    client, index, root = env
    populate(client, root)
    index.scan()
    assert indexed_review(client, folder="work", recursive=True) == [
        "work/a.txt",
        "work/nested/found.txt",
    ]
    assert indexed_review(client, search="FOUND") == ["work/nested/found.txt"]
    assert indexed_review(client, unreviewed=True) == ["work/nested/found.txt"]


def test_sorter_actions_show_without_a_rescan(env: Any) -> None:
    client, index, root = env
    populate(client, root)
    index.scan()
    (root / "dump/d.txt").write_text("sorted after the scan")
    assert client.post(P + "/rescan").status_code == 200  # the queue is cached
    classify(client, "d.txt", "school")
    assert "school/d.txt" in indexed_review(client)
    moved = client.post(
        P + "/reclassify",
        json={
            **guard(client, "school/d.txt", "tree"),
            "folder": "work",
            "filename": "d.txt",
            "review": True,
        },
    )
    assert moved.status_code == 200, moved.text
    paths = indexed_review(client)
    assert "work/d.txt" in paths and "school/d.txt" not in paths
    folder = client.post(
        P + "/folders/move", json={"path": "school", "parent": "work", "name": "uni"}
    )
    assert folder.status_code == 200, folder.text
    assert "work/uni/b.txt" in indexed_review(client)
    assert client.post(P + "/undo").status_code == 200  # the reclassification
    paths = indexed_review(client)
    assert "work/uni/d.txt" in paths and "work/d.txt" not in paths

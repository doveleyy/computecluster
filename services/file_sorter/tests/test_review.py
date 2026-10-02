from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.file_sorter import main
from services.file_sorter.repository import Seed, SorterRepository

P = "/api/projects/1"


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[tuple[TestClient, Path, Path]]:
    root = tmp_path / "library"
    (root / "dump").mkdir(parents=True)
    (root / "tree/school/economics").mkdir(parents=True)
    (root / "tree/work").mkdir()
    database = tmp_path / "sorter.db"
    app = main.create_app(
        database,
        library_root=root,
        seed=Seed("Review", "dump", "tree"),
        allow_dev_identity=True,
    )
    with TestClient(app) as client:
        yield client, root, database


def guard(client: TestClient, path: str) -> dict[str, Any]:
    response = client.get(P + "/sorted-entry", params={"path": path})
    assert response.status_code == 200, response.text
    entry = response.json()["entry"]
    return {k: entry[k] for k in ("path", "size", "mtime_ns")}


def rows(client: TestClient, route: str = "labels.jsonl") -> list[dict[str, Any]]:
    return [json.loads(line) for line in client.get(P + "/" + route).text.splitlines()]


def test_tree_review_searches_all_depths_and_excludes_discard_units_symlinks(
    setup: Any,
) -> None:
    client, root, database = setup
    for path in (
        "work/report.txt",
        "school/economics/Report.txt",
        ".hidden.txt",
        "work/unfinished.part",
        "_discarded/report.txt",
        "bundle/report.txt",
    ):
        location = root / "tree" / path
        location.parent.mkdir(parents=True, exist_ok=True)
        location.write_text(path)
    (root / "tree/work/link.txt").symlink_to(root / "tree/work/report.txt")
    SorterRepository(database).record(
        project_id=1,
        target="tree",
        action="sort",
        kind="folder",
        original_name="bundle",
        final_name="bundle",
        label="",
        destination="bundle",
        sha256=None,
        size=None,
    )
    result = client.get(P + "/review", params={"search": "REPORT"}).json()
    assert [e["path"] for e in result["entries"]] == [
        "school/economics/Report.txt",
        "work/report.txt",
    ]
    assert all(not e["reviewed"] for e in result["entries"])
    result = client.get(P + "/review", params={"folder": "school"}).json()
    assert [e["path"] for e in result["entries"]] == ["school/economics/Report.txt"]
    assert client.get(P + "/review", params={"folder": "../dump"}).status_code == 400


def test_confirm_appends_label_identity_and_undo_does_not_move_file(setup: Any) -> None:
    client, root, database = setup
    (root / "dump/original.txt").write_text("a training document")
    entry = client.get(P + "/current").json()["entry"]
    original = client.post(
        P + "/classify",
        json={
            **{k: entry[k] for k in ("path", "size", "mtime_ns")},
            "folder": "work",
            "filename": "tidy.txt",
        },
    ).json()
    repo = SorterRepository(database)
    before = repo.tree_decisions("tree")[0]
    file = root / "tree/work/tidy.txt"
    stat = file.stat()
    accepted = client.post(P + "/review/accept", json=guard(client, "work/tidy.txt"))
    assert accepted.status_code == 200, accepted.text
    label = next(r for r in rows(client) if r["relative_path"] == "work/tidy.txt")
    assert label["review_type"] == "accepted"
    assert label["document_id"] == original["decision_id"]
    assert label["replaces_decision_id"] == before.id
    assert label["source_path"] == "original.txt" and label["source"] == "dump"
    assert label["file_path"] == "tree/work/tidy.txt"
    assert label["sha256"] == hashlib.sha256(file.read_bytes()).hexdigest()
    assert repo.tree_decisions("tree")[0].previous_destination == "work/tidy.txt"
    assert file.stat().st_ino == stat.st_ino
    assert client.get(P + "/review").json()["entries"][0]["reviewed"]
    assert client.get(P + "/review", params={"unreviewed": True}).json()["total"] == 0
    assert client.post(P + "/undo").status_code == 200
    assert (
        file.stat().st_ino == stat.st_ino and not (root / "dump/original.txt").exists()
    )
    assert client.get(P + "/review").json()["entries"][0]["reviewed"]
    log = [
        r for r in rows(client, "decision-log.jsonl") if r["record_type"] == "decision"
    ]
    assert len(log) == 2 and log[1]["undone_at"]
    assert repo.tree_decisions("tree")[0] == before


def test_corrected_review_is_logged_and_undo_restores_previous_location(
    setup: Any,
) -> None:
    client, root, _ = setup
    (root / "tree/work/wrong.txt").write_text("economics coursework")
    result = client.post(
        P + "/reclassify",
        json={
            **guard(client, "work/wrong.txt"),
            "folder": "school/economics",
            "filename": "correct.txt",
            "review": True,
        },
    )
    assert result.status_code == 200, result.text
    label = rows(client)[0]
    assert label["review_type"] == "corrected" and label["source"] is None
    assert label["relative_path"] == "school/economics/correct.txt"
    assert client.get(P + "/review").json()["entries"][0]["reviewed"]
    assert client.post(P + "/undo").status_code == 200
    assert (root / "tree/work/wrong.txt").exists()
    assert not (root / "tree/school/economics/correct.txt").exists()


def test_sorted_duplicate_scope_ignores_dump_and_resolution_keeps_one_tree_copy(
    setup: Any,
) -> None:
    client, root, _ = setup
    for path in ("dump/pending.txt", "tree/work/a.txt", "tree/school/b.txt"):
        (root / path).write_text("identical")
    groups = client.get(
        P + "/duplicates", params={"scope": "tree", "force": True}
    ).json()["groups"]
    assert len(groups) == 1 and groups[0]["scope"] == "tree"
    assert {c["path"] for c in groups[0]["copies"]} == {"work/a.txt", "school/b.txt"}
    assert (
        client.post(P + "/review/accept", json=guard(client, "work/a.txt")).status_code
        == 409
    )
    assert (
        client.post(
            P + "/reclassify",
            json={
                **guard(client, "work/a.txt"),
                "folder": "work",
                "filename": "rename.txt",
                "review": True,
            },
        ).status_code
        == 409
    )
    found = groups[0]
    result = client.post(
        P + "/duplicates/resolve",
        json={
            "scope": "tree",
            "sha256": found["sha256"],
            "copies": [
                {k: c[k] for k in ("area", "path", "size", "mtime_ns")}
                for c in found["copies"]
            ],
            "keeper": 1,
            "folder": "work",
            "filename": "a.txt",
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["discarded"] == 1 and (root / "dump/pending.txt").exists()
    assert (
        client.get(P + "/duplicates", params={"scope": "tree", "force": True}).json()[
            "groups"
        ]
        == []
    )
    assert (
        client.post(P + "/review/accept", json=guard(client, "work/a.txt")).status_code
        == 200
    )
    assert client.post(P + "/undo").status_code == 200
    assert client.post(P + "/undo").status_code == 200
    assert (root / "tree/work/a.txt").exists() and (root / "tree/school/b.txt").exists()


def test_changed_file_is_unreviewed_and_stale_confirmation_is_refused(
    setup: Any,
) -> None:
    client, root, _ = setup
    file = root / "tree/work/a.txt"
    file.write_text("before")
    payload = guard(client, "work/a.txt")
    assert client.post(P + "/review/accept", json=payload).status_code == 200
    file.write_text("after is different")
    assert not client.get(P + "/review").json()["entries"][0]["reviewed"]
    assert client.post(P + "/review/accept", json=payload).status_code == 409
    assert (
        len(
            [
                r
                for r in rows(client, "decision-log.jsonl")
                if r["record_type"] == "decision"
            ]
        )
        == 1
    )


def test_confirmation_failure_leaves_file_and_log_intact(
    setup: Any, monkeypatch: Any
) -> None:
    client, root, _ = setup
    file = root / "tree/work/a.txt"
    file.write_text("keep this")

    def fail(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("test failure")

    monkeypatch.setattr(SorterRepository, "record", fail)
    with pytest.raises(sqlite3.OperationalError):
        client.post(P + "/review/accept", json=guard(client, "work/a.txt"))
    assert file.read_text() == "keep this"
    assert not client.get(P + "/review").json()["entries"][0]["reviewed"]


def test_large_review_anchor_retains_position_after_confirmation(setup: Any) -> None:
    client, root, _ = setup
    for index in range(505):
        (root / "tree/work" / f"file-{index:04}.txt").write_text(str(index))
    initial = client.get(P + "/review").json()
    assert (
        len(initial["entries"]) == 500
        and initial["next_offset"] == 500
        and initial["more"]
    )
    second = client.get(P + "/review", params={"anchor": "work/file-0501.txt"}).json()
    assert second["offset"] == 500 and second["total"] == 505 and not second["more"]
    assert second["entries"][1]["path"] == "work/file-0501.txt"
    assert (
        client.post(
            P + "/review/accept", json=guard(client, "work/file-0501.txt")
        ).status_code
        == 200
    )
    refreshed = client.get(
        P + "/review", params={"anchor": "work/file-0502.txt", "unreviewed": True}
    ).json()
    assert (
        refreshed["offset"] == 500
        and refreshed["entries"][1]["path"] == "work/file-0502.txt"
    )


def test_schema_three_upgrade_preserves_all_old_decision_values(setup: Any) -> None:
    _, _, database = setup
    repo = SorterRepository(database)
    repo.record(
        project_id=1,
        target="tree",
        action="sort",
        kind="file",
        original_name="before.txt",
        final_name="a.txt",
        label="work",
        destination="work/a.txt",
        sha256="abc",
        size=7,
        duplicate_group_id=4,
        duplicate_of_document_id=2,
    )
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE decisions DROP COLUMN review_type")
        connection.execute("ALTER TABLE decisions DROP COLUMN file_mtime_ns")
        connection.execute("PRAGMA user_version = 3")
        columns = [r[1] for r in connection.execute("PRAGMA table_info(decisions)")]
        before = connection.execute("SELECT * FROM decisions").fetchall()
    SorterRepository(database).initialize()
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert (
            connection.execute(
                "SELECT " + ",".join(columns) + " FROM decisions"
            ).fetchall()
            == before
        )
        assert connection.execute(
            "SELECT review_type,file_mtime_ns FROM decisions"
        ).fetchall() == [(None, None)]


def test_review_requires_administrator(tmp_path: Path) -> None:
    root = tmp_path / "library"
    (root / "dump").mkdir(parents=True)
    (root / "tree").mkdir()
    app = main.create_app(
        tmp_path / "db",
        library_root=root,
        seed=Seed("Review", "dump", "tree"),
        admin_account_id="admin",
        session_validator=lambda s: main.Identity("member", "member", "MEMBER"),
    )
    with TestClient(app) as client:
        headers = {
            "Tailscale-User-Login": "owner@example.test",
            "Cookie": "home_platform_dashboard=valid.sig",
        }
        assert client.get(P + "/review").status_code == 401
        assert client.get(P + "/review", headers=headers).status_code == 403
        assert (
            client.post(P + "/review/accept", headers=headers, json={}).status_code
            == 403
        )

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.file_sorter import duplicates, main
from services.file_sorter.library import LibraryError, rename_no_replace
from services.file_sorter.repository import Seed, SorterRepository

P = "/api/projects/1"
CONTENT = b"the same training document\n"


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[tuple[TestClient, Path, Path]]:
    library = tmp_path / "library"
    (library / "dump").mkdir(parents=True)
    (library / "tree/work").mkdir(parents=True)
    (library / "tree/school").mkdir()
    database = tmp_path / "sorter.db"
    app = main.create_app(
        database,
        library_root=library,
        seed=Seed("Review", "dump", "tree"),
        allow_dev_identity=True,
    )
    with TestClient(app) as client:
        yield client, library, database


def group(
    client: TestClient, path: str = "incoming.txt", area: str = "dump"
) -> dict[str, Any]:
    response = client.get(
        P + "/duplicate", params={"path": path, "area": area, "force": True}
    )
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


def resolve(
    client: TestClient,
    result: dict[str, Any],
    *,
    keeper: int = 0,
    filename: str = "chosen.txt",
    folder: str = "work",
) -> Any:
    return client.post(
        P + "/duplicates/resolve",
        json={
            "sha256": result["sha256"],
            "copies": [
                {k: c[k] for k in ("area", "path", "size", "mtime_ns")}
                for c in result["copies"]
            ],
            "keeper": keeper,
            "filename": filename,
            "folder": folder,
        },
    )


def rows(client: TestClient, route: str = "labels.jsonl") -> list[dict[str, Any]]:
    return [json.loads(line) for line in client.get(P + "/" + route).text.splitlines()]


def seed_copies(library: Path) -> None:
    (library / "dump/incoming.txt").write_bytes(CONTENT)
    (library / "tree/work/old-name.txt").write_bytes(CONTENT)
    (library / "tree/school/other-name.txt").write_bytes(CONTENT)


def test_indicator_finds_preexisting_tree_and_dump_copies(setup: Any) -> None:
    client, library, _ = setup
    seed_copies(library)
    (library / "dump/another.txt").write_bytes(CONTENT)
    found = group(client)
    assert len(found["copies"]) == 4
    assert found["sha256"] == hashlib.sha256(CONTENT).hexdigest()
    assert found["sorted_at"].startswith("tree/")
    scan = client.get(P + "/duplicates").json()
    assert len(scan["groups"]) == 1
    assert len(scan["groups"][0]["copies"]) == 4


@pytest.mark.parametrize("state", ["discarded", "missing", "changed", "another-tree"])
def test_indicator_does_not_trust_historical_hashes(setup: Any, state: str) -> None:
    client, library, database = setup
    (library / "dump/incoming.txt").write_bytes(CONTENT)
    destination = "_discarded/old.txt" if state == "discarded" else "work/old.txt"
    target = "other" if state == "another-tree" else "tree"
    repository = SorterRepository(database)
    repository.record(
        project_id=1,
        target=target,
        action="discard" if state == "discarded" else "sort",
        kind="file",
        original_name="old.txt",
        final_name="old.txt",
        label=destination.rpartition("/")[0],
        destination=destination,
        sha256=hashlib.sha256(CONTENT).hexdigest(),
        size=len(CONTENT),
    )
    if state != "missing":
        path = library / target / destination
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"X" * len(CONTENT) if state == "changed" else CONTENT)
    found = group(client)
    assert found["copies"] == []
    assert found["sorted_at"] is None


def test_indicator_rehashes_even_if_stat_cache_was_preserved(setup: Any) -> None:
    client, library, database = setup
    seed_copies(library)
    path = library / "tree/work/old-name.txt"
    stat = path.stat()
    SorterRepository(database).remember_hash(
        "tree/work/old-name.txt",
        stat.st_size,
        stat.st_mtime_ns,
        hashlib.sha256(CONTENT).hexdigest(),
    )
    path.write_bytes(b"x" * len(CONTENT))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    found = group(client)
    assert {c["path"] for c in found["copies"]} == {
        "incoming.txt",
        "school/other-name.txt",
    }


@pytest.mark.parametrize("keeper", [0, 1, 2])
def test_choose_any_keeper_and_another_copys_name_and_undo(
    setup: Any, keeper: int
) -> None:
    client, library, database = setup
    seed_copies(library)
    found = group(client)
    response = resolve(client, found, keeper=keeper, filename="old-name.txt")
    assert response.status_code == 200, response.text
    assert response.json()["discarded"] == 2
    assert (library / "tree/work/old-name.txt").read_bytes() == CONTENT
    assert not (library / "dump/incoming.txt").exists()
    assert not (library / "tree/school/other-name.txt").exists()
    exported = rows(client)
    assert len(exported) == 3
    kept = next(r for r in exported if r["label"] == "work")
    discarded = [r for r in exported if r["label"] == "_discarded"]
    assert kept["training_eligible"]
    assert all(r["duplicate_of_document_id"] == kept["document_id"] for r in discarded)
    assert all(
        not r["training_eligible"] and r["file_status"] == "present" for r in discarded
    )
    log = rows(client, "decision-log.jsonl")
    intent = next(r for r in log if r["record_type"] == "duplicate_resolution")
    assert intent["completed_at"] and not intent["cancelled_at"]
    assert len(json.loads(intent["plan_json"])["copies"]) == 3
    undone = client.post(P + "/undo")
    assert undone.status_code == 200, undone.text
    assert undone.json()["restored_copies"] == 3
    assert (library / "dump/incoming.txt").read_bytes() == CONTENT
    assert (library / "tree/work/old-name.txt").read_bytes() == CONTENT
    assert (library / "tree/school/other-name.txt").read_bytes() == CONTENT
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM decisions WHERE undone_at IS NOT NULL"
            ).fetchone()[0]
            == 3
        )
    assert not list((library / "tree/_discarded").iterdir())


def test_classify_and_reclassify_cannot_keep_an_identical_second_copy(
    setup: Any,
) -> None:
    client, library, _ = setup
    seed_copies(library)
    entry = client.get(P + "/current").json()["entry"]
    assert (
        client.post(
            P + "/classify",
            json={
                **{k: entry[k] for k in ("path", "size", "mtime_ns")},
                "folder": "work",
                "filename": "second.txt",
            },
        ).status_code
        == 409
    )
    entry = client.get(
        P + "/sorted-entry", params={"path": "school/other-name.txt"}
    ).json()["entry"]
    assert (
        client.post(
            P + "/reclassify",
            json={
                **{k: entry[k] for k in ("path", "size", "mtime_ns")},
                "folder": "work",
                "filename": "second.txt",
            },
        ).status_code
        == 409
    )
    assert len(group(client)["copies"]) == 3
    assert not [
        r for r in rows(client, "decision-log.jsonl") if r["record_type"] == "decision"
    ]


@pytest.mark.parametrize(
    "change", ["changed", "missing", "new-copy", "same-stat-content"]
)
def test_stale_group_refused_before_any_move(setup: Any, change: str) -> None:
    client, library, _ = setup
    seed_copies(library)
    found = group(client)
    old = library / "tree/school/other-name.txt"
    if change == "missing":
        old.unlink()
    elif change == "new-copy":
        (library / "dump/new.txt").write_bytes(CONTENT)
    else:
        stat = old.stat()
        old.write_bytes(b"x" * len(CONTENT))
        if change == "same-stat-content":
            os.utime(old, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    response = resolve(client, found)
    assert response.status_code in {404, 409}, response.text
    assert (library / "dump/incoming.txt").exists()
    assert not (library / "tree/work/chosen.txt").exists()
    assert not [
        r
        for r in rows(client, "decision-log.jsonl")
        if r["record_type"] == "duplicate_resolution"
    ]


def test_resolve_refuses_unrelated_destination_and_reserved_paths(setup: Any) -> None:
    client, library, _ = setup
    seed_copies(library)
    found = group(client)
    (library / "tree/work/chosen.txt").write_bytes(b"unrelated")
    assert resolve(client, found).status_code == 409
    assert resolve(client, found, folder="_discarded").status_code == 400
    assert resolve(client, found, folder="../dump").status_code == 400
    assert resolve(client, found, filename="../escape.txt").status_code == 400
    assert (library / "tree/work/chosen.txt").read_bytes() == b"unrelated"


def test_hashing_large_files_is_explicit_and_really_reads_the_whole_file(
    setup: Any, monkeypatch: Any
) -> None:
    client, library, _ = setup
    seed_copies(library)
    monkeypatch.setattr(main, "DUPLICATE_CHECK_BYTES", 1)
    monkeypatch.setattr(duplicates, "AUTOMATIC_BYTES", 1)
    assert (
        client.get(P + "/duplicate", params={"path": "incoming.txt"}).json()["skipped"]
        == "large"
    )
    scan = client.get(P + "/duplicates").json()
    assert scan["skipped_large"] == 3 and not scan["groups"]
    assert len(client.get(P + "/duplicates?force=true").json()["groups"]) == 1
    assert resolve(client, group(client)).status_code == 200


@pytest.mark.parametrize("failure", ["record", "second-rename"])
def test_group_rolls_back_files_and_log_if_a_step_fails(
    setup: Any, monkeypatch: Any, failure: str
) -> None:
    client, library, database = setup
    seed_copies(library)
    found = group(client)
    if failure == "record":
        original = SorterRepository.record
        calls = 0

        def fail(self: SorterRepository, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise sqlite3.IntegrityError("fixture failure")
            return original(self, **kwargs)

        monkeypatch.setattr(SorterRepository, "record", fail)
        with pytest.raises(sqlite3.IntegrityError):
            resolve(client, found)
    else:
        original_move = rename_no_replace
        calls = 0

        def fail_move(source: Path, target: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise LibraryError("fixture failure", 409)
            original_move(source, target)

        monkeypatch.setattr(main, "rename_no_replace", fail_move)
        assert resolve(client, found).status_code == 409
    assert len(group(client)["copies"]) == 3
    assert not list((library / "tree/_discarded").iterdir())
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
        assert connection.execute(
            "SELECT cancelled_at FROM duplicate_resolutions"
        ).fetchone()[0]
    assert client.get("/ready").status_code == 200


def test_undo_refuses_changed_contents_or_occupied_original_name(setup: Any) -> None:
    client, library, _ = setup
    seed_copies(library)
    assert resolve(client, group(client)).status_code == 200
    (library / "dump/incoming.txt").write_bytes(b"new content")
    assert client.post(P + "/undo").status_code == 409
    (library / "dump/incoming.txt").unlink()
    path = library / "tree/work/chosen.txt"
    path.write_bytes(b"external change")
    assert client.post(P + "/undo").status_code == 409
    assert len(list((library / "tree/_discarded").iterdir())) == 2


def test_pending_operation_keeps_full_intent_and_blocks_mutations(setup: Any) -> None:
    client, _, database = setup
    SorterRepository(database).prepare_duplicates(1, "tree", "a" * 64, '{"copies": []}')
    assert client.get("/ready").status_code == 503
    assert client.post(P + "/undo").status_code == 409
    assert client.post(P + "/skip", json={"path": "anything"}).status_code == 409
    assert next(
        r
        for r in rows(client, "decision-log.jsonl")
        if r["record_type"] == "duplicate_resolution"
    )["plan_json"]


def test_version_two_migration_preserves_all_old_columns(setup: Any) -> None:
    _, _, database = setup
    # Re-create an exact schema-2 decision table, preserving its ordered columns.
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE duplicate_resolutions")
        connection.execute("ALTER TABLE decisions DROP COLUMN duplicate_group_id")
        connection.execute("ALTER TABLE decisions DROP COLUMN duplicate_of_document_id")
        connection.execute("ALTER TABLE decisions DROP COLUMN review_type")
        connection.execute("ALTER TABLE decisions DROP COLUMN file_mtime_ns")
        connection.execute("PRAGMA user_version = 2")
        connection.execute(
            "INSERT INTO decisions VALUES (1, 'before', 'sort', 'file', 'a', 'a', "
            "'work', 'work/a', NULL, 1, NULL, 1, 'tree', NULL, NULL, NULL, 1)"
        )
        old = connection.execute("SELECT * FROM decisions").fetchall()
    repository = SorterRepository(database)
    repository.initialize()
    repository.initialize()
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT * FROM decisions").fetchone()[: len(old[0])]
            == old[0]
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5


def test_shared_tree_provenance_survives_duplicate_choice_and_folder_move(
    setup: Any,
) -> None:
    client, library, database = setup
    (library / "dump/incoming.txt").write_bytes(CONTENT)
    entry = client.get(P + "/current").json()["entry"]
    first = client.post(
        P + "/classify",
        json={
            **{k: entry[k] for k in ("path", "size", "mtime_ns")},
            "folder": "work",
            "filename": "original.txt",
        },
    ).json()
    original = next(r for r in rows(client) if r["decision_id"] == first["decision_id"])
    (library / "dump2").mkdir()
    second = client.post(
        "/api/projects",
        json={"name": "Second", "source": "dump2", "target": "tree", "mode": "top"},
    ).json()["id"]
    base = f"/api/projects/{second}"
    (library / "dump2/incoming.txt").write_bytes(CONTENT)
    found = client.get(base + "/duplicate?path=incoming.txt").json()
    keeper = next(i for i, c in enumerate(found["copies"]) if c["area"] == "tree")
    payload = {
        "sha256": found["sha256"],
        "copies": [
            {k: c[k] for k in ("area", "path", "size", "mtime_ns")}
            for c in found["copies"]
        ],
        "keeper": keeper,
        "folder": "school",
        "filename": "best.txt",
    }
    response = client.post(base + "/duplicates/resolve", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["document_id"] == original["document_id"]
    kept = next(r for r in rows(client) if r["label"] == "school")
    assert kept["project"] == "Review" and kept["source_path"] == "incoming.txt"
    raw = rows(client, "decision-log.jsonl")
    assert (
        next(
            r
            for r in raw
            if r["record_type"] == "decision" and r["id"] == first["decision_id"]
        )["label"]
        == "work"
    )
    assert (
        client.post(
            P + "/folders/move",
            json={"path": "school", "parent": "", "name": "studies"},
        ).status_code
        == 200
    )
    assert client.post(base + "/undo").status_code == 200
    assert (library / "tree/work/original.txt").read_bytes() == CONTENT
    assert (library / "dump2/incoming.txt").read_bytes() == CONTENT
    assert (
        SorterRepository(database).tree_decisions("tree")[0].document_id
        == original["document_id"]
    )


def test_scan_excludes_symlinks_folder_units_and_nonidentical_same_size(
    setup: Any,
) -> None:
    client, library, database = setup
    (library / "dump/incoming.txt").write_bytes(CONTENT)
    (library / "tree/work/different.txt").write_bytes(b"x" * len(CONTENT))
    (library / "tree/work/link.txt").symlink_to(library / "dump/incoming.txt")
    (library / "tree/work/unit").mkdir()
    (library / "tree/work/unit/copy.txt").write_bytes(CONTENT)
    SorterRepository(database).record(
        project_id=1,
        target="tree",
        action="sort",
        kind="folder",
        original_name="unit",
        final_name="unit",
        label="work",
        destination="work/unit",
        sha256=None,
        size=None,
    )
    assert client.get(P + "/duplicates").json()["groups"] == []
    assert not group(client)["copies"]


def test_undo_log_failure_restores_resolved_group(setup: Any, monkeypatch: Any) -> None:
    client, library, database = setup
    seed_copies(library)
    assert resolve(client, group(client)).status_code == 200

    def fail(self: SorterRepository, group_id: int, operation_id: int) -> None:
        raise sqlite3.IntegrityError("fixture undo failure")

    monkeypatch.setattr(SorterRepository, "undo_duplicate_group", fail)
    with pytest.raises(sqlite3.IntegrityError):
        client.post(P + "/undo")
    assert (library / "tree/work/chosen.txt").read_bytes() == CONTENT
    assert not (library / "dump/incoming.txt").exists()
    assert len(list((library / "tree/_discarded").iterdir())) == 2
    assert not SorterRepository(database).pending_duplicates()


def test_later_correction_must_be_undone_before_duplicate_group(setup: Any) -> None:
    client, library, database = setup
    seed_copies(library)
    assert resolve(client, group(client)).status_code == 200
    repository = SorterRepository(database)
    kept = next(d for d in repository.tree_decisions("tree") if d.action == "sort")
    # A second project's later action supersedes a member of the group.
    repository.record(
        project_id=2,
        target="tree",
        action="sort",
        kind="file",
        original_name=kept.original_name,
        final_name=kept.final_name,
        label=kept.label,
        destination=kept.destination,
        sha256=kept.sha256,
        size=kept.size,
        replaces_id=kept.id,
        previous_destination=kept.destination,
        document_id=kept.document_id,
    )
    assert client.post(P + "/undo").status_code == 409
    assert (library / "tree/work/chosen.txt").exists()


@pytest.mark.parametrize("moved", [0, 1, 2, 3])
@pytest.mark.parametrize("action", ["resolve", "undo"])
def test_journal_recovers_every_interruption_point(
    setup: Any, moved: int, action: str
) -> None:
    client, library, database = setup
    seed_copies(library)
    repository = SorterRepository(database)
    if action == "resolve":
        (library / "tree/_discarded").mkdir()
        copies = [
            {
                "area": "dump",
                "path": "incoming.txt",
                "destination": "_discarded/incoming.txt",
            },
            {
                "area": "tree",
                "path": "work/old-name.txt",
                "destination": "_discarded/old-name.txt",
            },
            {
                "area": "tree",
                "path": "school/other-name.txt",
                "destination": "work/old-name.txt",
            },
        ]
    else:
        assert (
            resolve(
                client, group(client), filename="old-name.txt", keeper=2
            ).status_code
            == 200
        )
        active = repository.tree_decisions("tree")
        copies = [
            {
                "area": "tree",
                "path": d.destination,
                "destination": d.previous_destination or d.original_name,
                "destination_area": "tree"
                if d.previous_destination is not None
                else "dump",
            }
            for d in active
        ]
    before = {
        p.relative_to(library).as_posix(): p.read_bytes()
        for p in library.rglob("*")
        if p.is_file()
    }
    operation = repository.prepare_duplicates(
        1,
        "tree",
        hashlib.sha256(CONTENT).hexdigest(),
        json.dumps({"action": action, "source": "dump", "copies": copies}),
    )
    for copy in copies[:moved]:
        source = library / ("dump" if copy["area"] == "dump" else "tree") / copy["path"]
        target = (
            library
            / ("dump" if copy.get("destination_area") == "dump" else "tree")
            / copy["destination"]
        )
        if source != target:
            rename_no_replace(source, target)
    response = client.post(P + "/duplicates/recover")
    assert response.status_code == 200, response.text
    assert response.json()["recovered"] == 1
    assert {
        p.relative_to(library).as_posix(): p.read_bytes()
        for p in library.rglob("*")
        if p.is_file()
    } == before
    assert not repository.pending_duplicates()
    assert client.get("/ready").status_code == 200
    assert next(
        r
        for r in rows(client, "decision-log.jsonl")
        if r.get("record_type") == "duplicate_resolution" and r["id"] == operation
    )["cancelled_at"]


def test_recovery_refuses_external_content_changes(setup: Any) -> None:
    client, library, database = setup
    seed_copies(library)
    repository = SorterRepository(database)
    (library / "tree/_discarded").mkdir()
    repository.prepare_duplicates(
        1,
        "tree",
        hashlib.sha256(CONTENT).hexdigest(),
        json.dumps(
            {
                "copies": [
                    {
                        "area": "dump",
                        "path": "incoming.txt",
                        "destination": "_discarded/incoming.txt",
                    }
                ]
            }
        ),
    )
    rename_no_replace(
        library / "dump/incoming.txt", library / "tree/_discarded/incoming.txt"
    )
    (library / "tree/_discarded/incoming.txt").write_bytes(b"changed externally")
    assert client.post(P + "/duplicates/recover").status_code == 409
    assert repository.pending_duplicates()
    assert not (library / "dump/incoming.txt").exists()


def test_duplicate_routes_require_administrator(tmp_path: Path) -> None:
    library = tmp_path / "library"
    (library / "dump").mkdir(parents=True)
    (library / "tree").mkdir()
    app = main.create_app(
        tmp_path / "sorter.db",
        library_root=library,
        seed=Seed("Review", "dump", "tree"),
        allow_dev_identity=False,
        admin_account_id="admin",
        session_validator=lambda s: main.Identity("member", "member", "MEMBER"),
    )
    with TestClient(app) as client:
        headers = {
            "Tailscale-User-Login": "owner@example.test",
            "Cookie": "home_platform_dashboard=valid.sig",
        }
        for route in ("duplicate?path=a", "duplicates"):
            assert client.get(P + "/" + route, headers=headers).status_code == 403
        for route in ("duplicates/resolve", "duplicates/recover"):
            assert (
                client.post(P + "/" + route, headers=headers, json={}).status_code
                == 403
            )
        assert client.get(P + "/duplicates").status_code == 401

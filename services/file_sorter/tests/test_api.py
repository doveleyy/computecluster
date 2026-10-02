from __future__ import annotations

import io
import json
import sqlite3
import urllib.error
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.file_sorter import main
from services.file_sorter.labels import decisions_for
from services.file_sorter.library import STOP, LibraryError, rename_no_replace
from services.file_sorter.repository import SCHEMA, Seed, SorterRepository

Library = tuple[Path, Path]
P = "/api/projects/1"
SEED = Seed("Downloads", "dump", "sorted")


@pytest.fixture
def library(tmp_path: Path) -> Library:
    dump = tmp_path / "lib" / "dump"
    sorted_root = tmp_path / "lib" / "sorted"
    dump.mkdir(parents=True)
    (sorted_root / "school" / "economics").mkdir(parents=True)
    (sorted_root / "work").mkdir()
    (dump / "b-notes.txt").write_text("EC2101 problem set")
    (dump / "a-report.pdf").write_bytes(b"%PDF-1.4 fake")
    (dump / ".DS_Store").write_bytes(b"x")
    (dump / "movie.mp4.crdownload").write_bytes(b"partial")
    project = dump / "c-project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "main.py").write_text("print('hi')")
    return dump, sorted_root


@pytest.fixture
def client(tmp_path: Path, library: Library) -> Iterator[TestClient]:
    dump, _ = library
    app = main.create_app(
        tmp_path / "sorter.db",
        library_root=dump.parent,
        seed=SEED,
        allow_dev_identity=True,
    )
    with TestClient(app) as test_client:
        yield test_client


def current(client: TestClient, name: str | None = None) -> dict[str, Any]:
    params = {"path": name} if name else {}
    response = client.get(P + "/current", params=params)
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


def guard(entry: dict[str, Any]) -> dict[str, Any]:
    return {"path": entry["path"], "size": entry["size"], "mtime_ns": entry["mtime_ns"]}


def classify(
    client: TestClient, entry: dict[str, Any], folder: str, filename: str | None = None
) -> Any:
    return client.post(
        P + "/classify",
        json={**guard(entry), "folder": folder, "filename": filename or entry["name"]},
    )


def test_queue_is_alphabetical_and_ignores_hidden_and_partial_files(
    client: TestClient,
) -> None:
    payload = current(client)
    assert payload["entry"]["name"] == "a-report.pdf"
    assert payload["entry"]["preview"]["mode"] == "pdf"
    assert payload["progress"]["remaining"] == 3


def test_classify_moves_entry_and_logs_three_routing_decisions(
    client: TestClient, library: Library
) -> None:
    dump, sorted_root = library
    entry = current(client, "b-notes.txt")["entry"]
    assert (
        client.post(
            P + "/folders",
            json={
                "parent": "school/economics",
                "name": "EC2101",
                "description": "Micro",
            },
        ).status_code
        == 201
    )
    response = classify(client, entry, "school/economics/EC2101")
    assert response.status_code == 200, response.text
    assert not (dump / "b-notes.txt").exists()
    moved = sorted_root / "school/economics/EC2101/b-notes.txt"
    assert moved.read_text() == "EC2101 problem set"

    rows = [
        json.loads(line) for line in client.get(P + "/labels.jsonl").text.splitlines()
    ]
    row = next(row for row in rows if row["label_source"] == "owner_sorted")
    assert row["label"] == "school/economics/EC2101"
    assert row["sha256"]
    assert row["decisions"] == [
        {"parent": "", "choice": "school"},
        {"parent": "school", "choice": "economics"},
        {"parent": "school/economics", "choice": "EC2101"},
    ]


def test_label_in_folder_with_children_adds_stop_decision() -> None:
    folders = ["school", "school/economics", "work"]
    assert decisions_for("school", folders) == [
        {"parent": "", "choice": "school"},
        {"parent": "school", "choice": STOP},
    ]
    assert decisions_for("work", folders) == [{"parent": "", "choice": "work"}]


def test_skip_moves_entry_to_back_of_queue(client: TestClient) -> None:
    first = current(client)["entry"]["name"]
    assert client.post(P + "/skip", json={"path": first}).status_code == 204
    payload = current(client)
    assert payload["entry"]["name"] == "b-notes.txt"
    assert payload["progress"]["skipped"] == 1
    for _ in range(2):
        client.post(P + "/skip", json={"path": current(client)["entry"]["name"]})
    assert current(client)["entry"]["name"] == first


def test_classify_refuses_to_overwrite_existing_file(
    client: TestClient, library: Library
) -> None:
    dump, sorted_root = library
    (sorted_root / "work" / "b-notes.txt").write_text("keep me")
    entry = current(client, "b-notes.txt")["entry"]
    response = classify(client, entry, "work")
    assert response.status_code == 409
    assert (sorted_root / "work" / "b-notes.txt").read_text() == "keep me"
    assert (dump / "b-notes.txt").exists()
    assert classify(client, entry, "work", "b-notes (2).txt").status_code == 200


@pytest.mark.parametrize("kind", ["file", "folder", "symlink"])
def test_filesystem_rename_refuses_existing_destination(
    tmp_path: Path, kind: str
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    if kind == "folder":
        source.mkdir()
        (source / "keep.txt").write_text("source")
        target.mkdir()
    else:
        source.write_text("source")
        if kind == "symlink":
            target.symlink_to(tmp_path / "missing")
        else:
            target.write_text("destination")
    # No caller-side exists check: the filesystem itself must reject this.
    with pytest.raises(LibraryError) as error:
        rename_no_replace(source, target)
    assert error.value.status == 409
    assert source.exists()
    if kind == "file":
        assert target.read_text() == "destination"
    elif kind == "symlink":
        assert target.is_symlink()


def test_classify_refuses_changed_entry(client: TestClient, library: Library) -> None:
    dump, _ = library
    entry = current(client, "b-notes.txt")["entry"]
    (dump / "b-notes.txt").write_text("still downloading, now longer")
    assert classify(client, entry, "work").status_code == 409
    assert (dump / "b-notes.txt").exists()


@pytest.mark.parametrize(
    ("folder", "filename"),
    [
        ("../outside", "b-notes.txt"),
        ("work", "../escape.txt"),
        ("work", "sub/escape.txt"),
        ("school//economics", "b-notes.txt"),
        ("_discarded", "b-notes.txt"),
        ("missing", "b-notes.txt"),
    ],
)
def test_classify_rejects_unsafe_paths(
    client: TestClient, library: Library, folder: str, filename: str
) -> None:
    dump, _ = library
    entry = current(client, "b-notes.txt")["entry"]
    response = classify(client, entry, folder, filename)
    assert response.status_code in {400, 404}
    assert (dump / "b-notes.txt").exists()


def test_symlinked_folder_is_refused(
    client: TestClient, library: Library, tmp_path: Path
) -> None:
    _, sorted_root = library
    outside = tmp_path / "outside"
    outside.mkdir()
    (sorted_root / "linked").symlink_to(outside, target_is_directory=True)
    assert all(
        f["path"] != "linked" for f in client.get(P + "/folders").json()["folders"]
    )
    entry = current(client, "b-notes.txt")["entry"]
    assert classify(client, entry, "linked").status_code == 400
    assert not any(outside.iterdir())


def test_symlinked_dump_entry_is_not_queued(
    client: TestClient, library: Library, tmp_path: Path
) -> None:
    dump, _ = library
    (tmp_path / "secret.txt").write_text("secret")
    (dump / "aa-link.txt").symlink_to(tmp_path / "secret.txt")
    assert current(client)["entry"]["name"] == "a-report.pdf"
    assert client.get(P + "/current", params={"path": "aa-link.txt"}).status_code == 404


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "__stop__", " padded "])
def test_folder_names_are_validated(client: TestClient, name: str) -> None:
    response = client.post(P + "/folders", json={"parent": "", "name": name})
    assert response.status_code in {400, 422}


def test_folder_unit_moves_intact_and_is_not_offered_as_category(
    client: TestClient, library: Library
) -> None:
    _, sorted_root = library
    entry = current(client, "c-project")["entry"]
    assert entry["kind"] == "folder"
    assert entry["preview"]["mode"] == "listing"
    assert {item["path"] for item in entry["preview"]["items"]} == {
        "src",
        "src/main.py",
    }
    assert classify(client, entry, "work").status_code == 200
    assert (sorted_root / "work/c-project/src/main.py").read_text() == "print('hi')"
    paths = [folder["path"] for folder in client.get(P + "/folders").json()["folders"]]
    assert "work/c-project" not in paths
    assert "work/c-project/src" not in paths
    entry = current(client, "b-notes.txt")["entry"]
    assert classify(client, entry, "work/c-project/src").status_code == 400


def test_discard_and_undo_restore_entries(client: TestClient, library: Library) -> None:
    dump, sorted_root = library
    (sorted_root / "_discarded").mkdir()
    (sorted_root / "_discarded" / "a-report.pdf").write_bytes(b"older junk")
    entry = current(client, "a-report.pdf")["entry"]
    response = client.post(P + "/discard", json=guard(entry))
    assert response.json()["destination"] == "_discarded/a-report (2).pdf"
    assert "_discarded" not in [
        folder["path"] for folder in client.get(P + "/folders").json()["folders"]
    ]
    assert current(client)["progress"]["discarded"] == 1

    undone = client.post(P + "/undo")
    assert undone.json()["path"] == "a-report.pdf"
    assert (dump / "a-report.pdf").read_bytes() == b"%PDF-1.4 fake"
    assert current(client)["progress"]["discarded"] == 0
    assert client.post(P + "/undo").status_code == 404


def test_undo_refuses_when_dump_name_is_taken(
    client: TestClient, library: Library
) -> None:
    dump, sorted_root = library
    entry = current(client, "b-notes.txt")["entry"]
    classify(client, entry, "work")
    (dump / "b-notes.txt").write_text("a new download with the same name")
    assert client.post(P + "/undo").status_code == 409
    assert (sorted_root / "work/b-notes.txt").exists()


def test_duplicate_content_points_at_sorted_copy(
    client: TestClient, library: Library
) -> None:
    dump, _ = library
    entry = current(client, "b-notes.txt")["entry"]
    classify(client, entry, "work")
    (dump / "b-notes (1).txt").write_text("EC2101 problem set")
    response = client.get(P + "/duplicate", params={"path": "b-notes (1).txt"})
    assert response.json()["sorted_at"] == "sorted/work/b-notes.txt"


def test_export_includes_pre_existing_sorted_files(
    client: TestClient, library: Library
) -> None:
    _, sorted_root = library
    (sorted_root / "school" / "economics" / "old.pdf").write_bytes(b"%PDF")
    rows = [
        json.loads(line) for line in client.get(P + "/labels.jsonl").text.splitlines()
    ]
    assert rows == [
        {
            "document_id": None,
            "decision_id": None,
            "replaces_decision_id": None,
            "tree": "sorted",
            "relative_path": "school/economics/old.pdf",
            "file_path": "sorted/school/economics/old.pdf",
            "file_status": "present",
            "name": "old.pdf",
            "original_name": "old.pdf",
            "kind": "file",
            "sha256": None,
            "size": None,
            "duplicate_eligible": True,
            "training_eligible": False,
            "classification_provenance": "discovered",
            "needs_review": True,
            "content_verified": False,
            "label": "school/economics",
            "decided_label": "school/economics",
            "decisions": [
                {"parent": "", "choice": "school"},
                {"parent": "school", "choice": "economics"},
            ],
            "label_source": "pre_existing",
            "decided_at": None,
            "project": None,
            "source": None,
            "source_path": None,
        }
    ]


def test_raw_preview_is_limited_to_safe_inline_types(
    client: TestClient, library: Library
) -> None:
    dump, _ = library
    (dump / "page.html").write_text("<script>alert(1)</script>")
    raw = client.get(P + "/raw", params={"path": "a-report.pdf"})
    assert raw.status_code == 200
    assert raw.headers["content-type"] == "application/pdf"
    assert raw.headers["x-content-type-options"] == "nosniff"
    assert raw.headers["content-disposition"].startswith("inline")
    # HTML is served only under a sandboxing policy: no scripts, no network.
    page = client.get(P + "/raw", params={"path": "page.html"})
    assert page.status_code == 200
    policy = page.headers["content-security-policy"]
    assert policy.startswith("sandbox;") and "default-src 'none'" in policy
    html = current(client, "page.html")["entry"]["preview"]
    assert html["mode"] == "html"
    assert html["source"] == "<script>alert(1)</script>"
    (dump / "c.zip").write_bytes(b"not really a zip")
    assert client.get(P + "/raw", params={"path": "c.zip"}).status_code == 415


def test_office_documents_preview_as_text(client: TestClient, library: Library) -> None:
    dump, _ = library
    document = io.BytesIO()
    with zipfile.ZipFile(document, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="w"><w:body><w:p><w:r><w:t>Hello</w:t></w:r>'
            "</w:p><w:p><w:r><w:t>World</w:t></w:r></w:p></w:body></w:document>",
        )
    (dump / "letter.docx").write_bytes(document.getvalue())
    preview = current(client, "letter.docx")["entry"]["preview"]
    assert preview["mode"] == "text"
    assert preview["text"] == "Hello\nWorld"


def test_empty_dump_reports_done(client: TestClient, library: Library) -> None:
    for _ in range(3):
        entry = current(client)["entry"]
        assert client.post(P + "/discard", json=guard(entry)).status_code == 200
    payload = current(client)
    assert payload["entry"] is None
    assert payload["progress"]["discarded"] == 3


def test_ready_requires_the_library_root(tmp_path: Path, library: Library) -> None:
    dump, _ = library
    app = main.create_app(tmp_path / "ready.db", library_root=tmp_path / "missing")
    with TestClient(app) as test_client:
        assert test_client.get("/ready").status_code == 503
    app = main.create_app(tmp_path / "ready.db", library_root=dump.parent)
    with TestClient(app) as test_client:
        assert test_client.get("/ready").status_code == 200


def test_missing_identity_is_rejected(tmp_path: Path, library: Library) -> None:
    dump, _ = library
    app = main.create_app(
        tmp_path / "s.db",
        library_root=dump.parent,
        seed=SEED,
        allow_dev_identity=False,
    )
    with TestClient(app) as test_client:
        assert test_client.get(P + "/current").status_code == 401
        assert test_client.get("/").status_code == 401


ADMIN_ID = "00000000-0000-0000-0000-000000000001"


def session_app(
    tmp_path: Path,
    library: Library,
    validator: Any,
    admin_account_id: str | None = ADMIN_ID,
) -> TestClient:
    dump, _ = library
    return TestClient(
        main.create_app(
            tmp_path / "s.db",
            library_root=dump.parent,
            seed=SEED,
            allow_dev_identity=False,
            admin_account_id=admin_account_id,
            session_validator=validator,
        )
    )


def as_caller(
    client: TestClient, path: str, session: str | None, method: str = "GET"
) -> int:
    headers = {"Tailscale-User-Login": "owner@example.com"}
    if session is not None:
        headers["Cookie"] = f"home_platform_dashboard={session}"
    if method == "POST":
        response = client.post(path, headers=headers, json={"path": "b-notes.txt"})
    else:
        response = client.get(path, headers=headers)
    return response.status_code


def test_only_the_administrator_session_may_sort(
    tmp_path: Path, library: Library
) -> None:
    sessions = {
        "admin.sig": main.Identity(ADMIN_ID, "admin", "ADMIN"),
        "member.sig": main.Identity("15002e91", "shawn", "MEMBER"),
        "other-admin.sig": main.Identity("another-admin", "root2", "ADMIN"),
    }
    seen: list[str] = []

    def validate(session: str) -> main.Identity | None:
        seen.append(session)
        return sessions.get(session)

    with session_app(tmp_path, library, validate) as client:
        for method, path in (("GET", P + "/current"), ("POST", P + "/skip")):
            assert as_caller(client, path, "member.sig", method) == 403
            assert as_caller(client, path, "other-admin.sig", method) == 403
            assert as_caller(client, path, "expired.sig", method) == 401
            assert as_caller(client, path, None, method) == 401
        assert as_caller(client, P + "/current", "admin.sig") == 200
        assert as_caller(client, P + "/skip", "admin.sig", "POST") == 204
        # A malformed cookie is refused before it reaches the control plane.
        seen.clear()
        assert as_caller(client, P + "/current", "bad value;x=1") == 401
        assert seen == []
        # Even a valid administrator session needs the private tailnet route.
        response = client.get(
            P + "/current", headers={"Cookie": "home_platform_dashboard=admin.sig"}
        )
        assert response.status_code == 401


def test_signed_out_page_links_to_dashboard(tmp_path: Path, library: Library) -> None:
    with session_app(tmp_path, library, lambda session: None) as client:
        response = client.get("/", headers={"Tailscale-User-Login": "x@example.com"})
        assert response.status_code == 401
        assert 'href="/dashboard"' in response.text
        assert 'id="folders"' not in response.text


def test_sign_in_failures_fail_closed(tmp_path: Path, library: Library) -> None:
    def unavailable(session: str) -> main.Identity | None:
        raise main.IdentityServiceUnavailableError

    with session_app(tmp_path, library, unavailable) as client:
        assert as_caller(client, P + "/current", "admin.sig") == 503
    with session_app(
        tmp_path, library, lambda s: None, admin_account_id=None
    ) as client:
        assert as_caller(client, P + "/current", "admin.sig") == 503


def test_session_validator_forwards_cookie_to_control_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[Any] = []

    def fake_urlopen(request: Any, timeout: float) -> io.BytesIO:
        requests.append(request)
        cookie = request.get_header("Cookie")
        if cookie == "home_platform_dashboard=expired.sig":
            raise urllib.error.HTTPError(request.full_url, 401, "no", {}, None)  # type: ignore[arg-type]
        if cookie == "home_platform_dashboard=broken.sig":
            raise urllib.error.HTTPError(request.full_url, 500, "no", {}, None)  # type: ignore[arg-type]
        return io.BytesIO(
            json.dumps({"id": ADMIN_ID, "username": "admin", "role": "ADMIN"}).encode()
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    validate = main._session_validator("http://control/jobs-ui/api/session")
    assert validate("admin.sig") == main.Identity(ADMIN_ID, "admin", "ADMIN")
    assert requests[0].full_url == "http://control/jobs-ui/api/session"
    assert validate("expired.sig") is None
    with pytest.raises(main.IdentityServiceUnavailableError):
        validate("broken.sig")


def test_untidy_existing_names_can_be_sorted(
    client: TestClient, library: Library
) -> None:
    dump, sorted_root = library
    (dump / " leading space project").mkdir()
    (dump / " leading space project" / "notes.txt").write_text("x")
    payload = current(client)
    entry = payload["entry"]
    assert entry["name"] == " leading space project"
    assert client.post(P + "/skip", json={"path": entry["name"]}).status_code == 204
    assert current(client)["entry"]["name"] == "a-report.pdf"
    entry = current(client, " leading space project")["entry"]
    # New names stay strict: the page trims, and an untrimmed name is refused.
    assert classify(client, entry, "work", " untrimmed").status_code == 400
    response = classify(client, entry, "work", "leading space project")
    assert response.status_code == 200
    assert (sorted_root / "work/leading space project/notes.txt").exists()
    assert client.post(P + "/undo").json()["path"] == " leading space project"
    assert (dump / " leading space project" / "notes.txt").exists()


def export(client: TestClient) -> list[dict[str, Any]]:
    response = client.get(P + "/labels.jsonl")
    return [json.loads(line) for line in response.text.splitlines()]


def test_group_inserts_a_level_and_relabels_earlier_decisions(
    client: TestClient, library: Library
) -> None:
    dump, sorted_root = library
    (sorted_root / "school" / "DSA3102").mkdir()
    (sorted_root / "school" / "DSA4212").mkdir()
    client.post(
        P + "/folders",
        json={"parent": "school", "name": "CS2040", "description": "Algorithms"},
    )
    entry = current(client, "b-notes.txt")["entry"]
    assert classify(client, entry, "school/DSA3102").status_code == 200

    response = client.post(
        P + "/folders/group",
        json={
            "paths": ["school/DSA3102", "school/DSA4212"],
            "name": "data_science",
            "description": "Data science modules",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["moved"] == [
        "school/data_science/DSA3102",
        "school/data_science/DSA4212",
    ]
    assert (sorted_root / "school/data_science/DSA3102/b-notes.txt").exists()
    assert not (sorted_root / "school" / "DSA3102").exists()

    folders = {f["path"]: f for f in client.get(P + "/folders").json()["folders"]}
    assert folders["school/data_science"]["description"] == "Data science modules"
    assert folders["school/data_science"]["subfolders"] == 2
    assert folders["school/data_science/DSA3102"]["items"] == 1

    (row,) = [r for r in export(client) if r["label_source"] == "owner_sorted"]
    assert row["label"] == "school/data_science/DSA3102"
    assert row["decided_label"] == "school/DSA3102"
    assert [step["choice"] for step in row["decisions"]] == [
        "school",
        "data_science",
        "DSA3102",
    ]
    # Undo still finds the file at its new home.
    assert client.post(P + "/undo").json()["path"] == "b-notes.txt"
    assert (dump / "b-notes.txt").exists()


def test_moves_chain_and_only_apply_to_earlier_decisions(
    client: TestClient, library: Library
) -> None:
    _, sorted_root = library
    (sorted_root / "school" / "DSA3102").mkdir()
    first = current(client, "b-notes.txt")["entry"]
    classify(client, first, "school/DSA3102")
    client.post(P + "/folders/group", json={"paths": ["school/DSA3102"], "name": "ds"})
    second = current(client, "a-report.pdf")["entry"]
    classify(client, second, "school/ds/DSA3102")
    # Rename the inserted level: both decisions follow, each exactly once.
    response = client.post(
        P + "/folders/move",
        json={"path": "school/ds", "parent": "school", "name": "data_science"},
    )
    assert response.json() == {"path": "school/data_science"}
    labels = {
        r["name"]: r for r in export(client) if r["label_source"] != "pre_existing"
    }
    assert labels["b-notes.txt"]["label"] == "school/data_science/DSA3102"
    assert labels["b-notes.txt"]["decided_label"] == "school/DSA3102"
    assert labels["a-report.pdf"]["label"] == "school/data_science/DSA3102"
    assert labels["a-report.pdf"]["decided_label"] == "school/ds/DSA3102"
    # Moving a folder to the top level and back works like any other move.
    assert client.post(
        P + "/folders/move",
        json={"path": "school/data_science", "parent": "", "name": "data_science"},
    ).json() == {"path": "data_science"}
    assert (sorted_root / "data_science/DSA3102/a-report.pdf").exists()


@pytest.mark.parametrize(
    ("path", "parent", "name", "status"),
    [
        ("school", "school/economics", "school", 400),  # inside itself
        ("school/economics", "work", "economics", 200),
        ("school/economics", "", "work", 409),  # name taken
        ("_discarded", "work", "_discarded", 400),
        ("missing", "work", "missing", 404),
        ("school/economics", "missing", "economics", 404),
        ("school/economics", "", "_discarded", 400),
        ("school/economics", "school", "bad/name", 400),
    ],
)
def test_folder_move_rules(
    client: TestClient,
    library: Library,
    path: str,
    parent: str,
    name: str,
    status: int,
) -> None:
    _, sorted_root = library
    (sorted_root / "_discarded").mkdir()
    response = client.post(
        P + "/folders/move", json={"path": path, "parent": parent, "name": name}
    )
    assert response.status_code == status, response.text
    if status != 200:
        assert (sorted_root / "school" / "economics").is_dir()


def test_group_and_move_refuse_unsafe_requests(
    client: TestClient, library: Library
) -> None:
    _, sorted_root = library
    entry = current(client, "c-project")["entry"]
    classify(client, entry, "work")  # a folder unit, not a category
    for body in (
        {"paths": ["school/economics", "work"], "name": "mixed"},  # two parents
        {"paths": ["school/economics"], "name": "economics"},  # same name
        {"paths": ["school/missing"], "name": "x"},
        {"paths": ["work/c-project"], "name": "x"},
    ):
        assert client.post(P + "/folders/group", json=body).status_code in {400, 404}
    assert not (sorted_root / "x").exists()
    assert not (sorted_root / "school" / "x").exists()
    response = client.post(
        P + "/folders/move",
        json={"path": "work/c-project", "parent": "school", "name": "c-project"},
    )
    assert response.status_code == 400
    response = client.post(
        P + "/folders/move",
        json={"path": "school", "parent": "work/c-project", "name": "school"},
    )
    assert response.status_code == 400
    assert (sorted_root / "work/c-project/src/main.py").exists()


def test_current_lists_upcoming_entries(client: TestClient) -> None:
    payload = current(client)
    assert payload["entry"]["name"] == "a-report.pdf"
    assert payload["upcoming"] == ["b-notes.txt", "c-project"]


# ---------- projects (0.3.0) ----------


def make_project(
    client: TestClient,
    name: str,
    source: str,
    target: str,
    mode: str = "top",
    create_target: bool = False,
) -> Any:
    return client.post(
        "/api/projects",
        json={
            "name": name,
            "source": source,
            "target": target,
            "mode": mode,
            "create_target": create_target,
        },
    )


@pytest.fixture
def phone(library: Library) -> Path:
    """A second dump with nested folders, as from a phone backup."""
    dump, _ = library
    phone = dump.parent / "phone"
    (phone / "2019" / "Camera").mkdir(parents=True)
    (phone / "2019" / "Camera" / "img-1.jpg").write_bytes(b"jpeg one")
    (phone / "2019" / "Camera" / ".DS_Store").write_bytes(b"finder")
    (phone / "2019" / "notes.txt").write_text("todo")
    (phone / "Downloads").mkdir()
    (phone / "Downloads" / "receipt.pdf").write_bytes(b"%PDF receipt")
    (phone / ".thumbnails").mkdir()
    (phone / ".thumbnails" / "t.jpg").write_bytes(b"hidden")
    return phone


def test_seed_project_is_listed(client: TestClient) -> None:
    (project,) = client.get("/api/projects").json()["projects"]
    assert project["name"] == "Downloads"
    assert (project["source"], project["target"], project["mode"]) == (
        "dump",
        "sorted",
        "top",
    )
    assert project["status"] == "ok"


@pytest.mark.parametrize(
    ("source", "target", "create", "status"),
    [
        ("phone", "sorted", False, 201),  # shares the seed project's tree
        ("phone", "photos", True, 201),  # a brand-new tree
        ("phone", "photos", False, 404),  # tree missing without create
        ("missing", "photos", True, 404),
        ("dump", "photos", True, 400),  # already another project's dump
        ("phone", "dump", False, 400),  # another project's dump as tree
        ("sorted", "photos", True, 400),  # another project's tree as dump
        ("sorted/work", "photos", True, 400),  # inside another tree
        ("phone", "sorted/work", False, 400),  # nested inside another tree
        ("phone", "phone/2019", False, 400),  # tree inside its own dump
        ("phone/2019", "phone", False, 400),  # dump inside its own tree
        ("../outside", "photos", True, 400),
        (".hidden", "photos", True, 400),
        ("phone", "_discarded", True, 400),
        ("", "photos", True, 422),
    ],
)
def test_project_folders_are_validated(
    client: TestClient,
    phone: Path,
    source: str,
    target: str,
    create: bool,
    status: int,
) -> None:
    (phone.parent / ".hidden").mkdir()
    response = make_project(client, "Phone", source, target, "files", create)
    assert response.status_code == status, response.text
    created = (phone.parent / "photos").exists()
    assert created == (status == 201 and target == "photos")


def test_project_names_are_unique_and_archiving_hides(
    client: TestClient, phone: Path
) -> None:
    assert make_project(client, "Downloads", "phone", "sorted").status_code == 409
    created = make_project(client, "Phone", "phone", "sorted").json()
    assert (
        client.patch(
            f"/api/projects/{created['id']}", json={"name": "Old phone"}
        ).json()["name"]
        == "Old phone"
    )
    assert client.delete(f"/api/projects/{created['id']}").status_code == 204
    names = [p["name"] for p in client.get("/api/projects").json()["projects"]]
    assert names == ["Downloads"]
    assert client.get(f"/api/projects/{created['id']}/current").status_code == 404
    # The dump is free again once its project is archived.
    assert make_project(client, "Phone again", "phone", "sorted").status_code == 201


def test_files_mode_queues_every_file_and_tidies_emptied_folders(
    client: TestClient, library: Library, phone: Path
) -> None:
    _, sorted_root = library
    project = make_project(client, "Phone", "phone", "sorted", "files").json()
    base = f"/api/projects/{project['id']}"
    payload = client.get(base + "/current").json()
    # Hidden folders and Finder metadata never enter the queue.
    assert [payload["entry"]["path"], *payload["upcoming"]] == [
        "2019/Camera/img-1.jpg",
        "2019/notes.txt",
        "Downloads/receipt.pdf",
    ]
    entry = payload["entry"]
    assert (entry["name"], entry["folder"]) == ("img-1.jpg", "2019/Camera")
    raw = client.get(base + "/raw", params={"path": entry["path"]})
    assert raw.status_code == 200 and raw.content == b"jpeg one"

    response = client.post(
        base + "/classify",
        json={**guard(entry), "folder": "work", "filename": "img.jpg"},
    )
    assert response.status_code == 200, response.text
    assert (sorted_root / "work" / "img.jpg").read_bytes() == b"jpeg one"
    # The emptied Camera folder (only .DS_Store left) is tidied away.
    assert not (phone / "2019" / "Camera").exists()
    assert (phone / "2019" / "notes.txt").exists()
    assert client.get(base + "/current").json()["progress"]["remaining"] == 2

    # Undo recreates the tidied folder and restores the file into it.
    assert client.post(base + "/undo").json()["path"] == "2019/Camera/img-1.jpg"
    assert (phone / "2019" / "Camera" / "img-1.jpg").read_bytes() == b"jpeg one"
    assert client.get(base + "/current").json()["progress"]["remaining"] == 3

    receipt = client.get(base + "/current", params={"path": "Downloads/receipt.pdf"})
    response = client.post(base + "/discard", json=guard(receipt.json()["entry"]))
    assert response.json()["destination"] == "_discarded/receipt.pdf"
    assert not (phone / "Downloads").exists()


@pytest.mark.parametrize(
    "path",
    [
        "../dump/b-notes.txt",
        "/etc/passwd",
        "2019/../../dump/b-notes.txt",
        "2019",
        ".thumbnails/t.jpg",
    ],
)
def test_files_mode_refuses_paths_outside_the_queue(
    client: TestClient, phone: Path, path: str
) -> None:
    project = make_project(client, "Phone", "phone", "sorted", "files").json()
    base = f"/api/projects/{project['id']}"
    assert client.get(base + "/raw", params={"path": path}).status_code in {400, 404}
    assert client.post(base + "/skip", json={"path": path}).status_code in {400, 404}


def test_skips_and_undo_belong_to_their_project(
    client: TestClient, library: Library, phone: Path
) -> None:
    dump, _ = library
    project = make_project(client, "Phone", "phone", "sorted", "files").json()
    base = f"/api/projects/{project['id']}"
    client.post(base + "/skip", json={"path": "2019/notes.txt"})
    assert current(client)["progress"]["skipped"] == 0
    assert client.get(base + "/current").json()["progress"]["skipped"] == 1
    entry = current(client, "b-notes.txt")["entry"]
    classify(client, entry, "work")
    # Undo in the phone project must not reach the downloads decision.
    assert client.post(base + "/undo").status_code == 404
    assert not (dump / "b-notes.txt").exists()
    assert client.post(P + "/undo").json()["path"] == "b-notes.txt"


def test_projects_sharing_a_tree_share_folders_moves_and_labels(
    client: TestClient, library: Library, phone: Path
) -> None:
    _, sorted_root = library
    (sorted_root / "school" / "DSA3102").mkdir()
    phone_project = make_project(client, "Phone", "phone", "sorted", "files").json()
    base = f"/api/projects/{phone_project['id']}"
    client.post(
        P + "/folders", json={"parent": "", "name": "photos", "description": "Pics"}
    )
    entry = current(client, "b-notes.txt")["entry"]
    classify(client, entry, "school/DSA3102")
    notes = client.get(base + "/current", params={"path": "2019/notes.txt"})
    client.post(
        base + "/classify",
        json={
            **guard(notes.json()["entry"]),
            "folder": "school/DSA3102",
            "filename": "notes.txt",
        },
    )
    # The phone project sees the downloads project's folders and notes.
    folders = {f["path"]: f for f in client.get(base + "/folders").json()["folders"]}
    assert folders["photos"]["description"] == "Pics"
    # Grouping from either project relabels both projects' decisions.
    client.post(
        base + "/folders/group", json={"paths": ["school/DSA3102"], "name": "ds"}
    )
    rows = [
        json.loads(line) for line in client.get(P + "/labels.jsonl").text.splitlines()
    ]
    sorted_rows = {
        r["source_path"]: r for r in rows if r["label_source"] == "owner_sorted"
    }
    assert sorted_rows["b-notes.txt"]["label"] == "school/ds/DSA3102"
    assert sorted_rows["b-notes.txt"]["project"] == "Downloads"
    assert sorted_rows["2019/notes.txt"]["label"] == "school/ds/DSA3102"
    assert sorted_rows["2019/notes.txt"]["source"] == "phone"
    assert sorted_rows["2019/notes.txt"]["original_name"] == "notes.txt"
    assert (
        client.get(base + "/labels.jsonl").text == client.get(P + "/labels.jsonl").text
    )


def test_separate_trees_stay_isolated(
    client: TestClient, library: Library, phone: Path
) -> None:
    _, sorted_root = library
    project = make_project(client, "Phone", "phone", "photos", "files", True).json()
    base = f"/api/projects/{project['id']}"
    assert client.get(base + "/folders").json()["folders"] == []
    client.post(base + "/folders", json={"parent": "", "name": "work"})
    entry = client.get(base + "/current").json()["entry"]
    client.post(
        base + "/classify", json={**guard(entry), "folder": "work", "filename": "a.jpg"}
    )
    assert (phone.parent / "photos" / "work" / "a.jpg").exists()
    assert not (sorted_root / "work" / "a.jpg").exists()
    # A folder move in one tree never relabels the other tree's decisions.
    downloads = current(client, "b-notes.txt")["entry"]
    classify(client, downloads, "work")
    client.post(
        base + "/folders/move", json={"path": "work", "parent": "", "name": "jobs"}
    )
    rows = [
        json.loads(line) for line in client.get(P + "/labels.jsonl").text.splitlines()
    ]
    assert [r["label"] for r in rows if r["label_source"] == "owner_sorted"] == ["work"]
    photo_rows = client.get(base + "/labels.jsonl").text.splitlines()
    assert [json.loads(r)["label"] for r in photo_rows] == ["jobs"]


def test_queue_cache_follows_moves_and_rescan(
    client: TestClient, library: Library
) -> None:
    dump, _ = library
    assert current(client)["progress"]["remaining"] == 3
    (dump / "0-new-download.txt").write_text("new")
    # Outside changes wait for the cache window or an explicit rescan.
    assert current(client)["progress"]["remaining"] == 3
    assert client.post(P + "/rescan").json() == {"remaining": 4}
    assert current(client)["entry"]["path"] == "0-new-download.txt"


def test_browse_lists_visible_folders_with_roles(
    client: TestClient, phone: Path
) -> None:
    (phone.parent / "#recycle").mkdir()
    folders = {f["path"]: f for f in client.get("/api/browse").json()["folders"]}
    assert set(folders) == {"dump", "phone", "sorted"}
    assert folders["dump"]["role"] == "dump of Downloads"
    assert folders["sorted"]["role"] == "tree of Downloads"
    assert folders["phone"]["folders"] == 2
    inner = client.get("/api/browse", params={"path": "phone"}).json()["folders"]
    assert [f["path"] for f in inner] == ["phone/2019", "phone/Downloads"]
    assert client.get("/api/browse", params={"path": "../.."}).status_code == 400


def test_migrates_a_single_library_database(tmp_path: Path, library: Library) -> None:
    import sqlite3

    dump, _ = library
    database = tmp_path / "old.db"
    with sqlite3.connect(database) as old:
        old.executescript(
            """
            CREATE TABLE skips (name TEXT PRIMARY KEY, skipped_at TEXT NOT NULL);
            CREATE TABLE hashes (name TEXT, size INTEGER, mtime_ns INTEGER,
                sha256 TEXT, PRIMARY KEY (name, size, mtime_ns));
            CREATE TABLE folders (path TEXT PRIMARY KEY,
                description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
            CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT,
                decided_at TEXT NOT NULL, action TEXT NOT NULL, kind TEXT NOT NULL,
                original_name TEXT NOT NULL, final_name TEXT NOT NULL,
                label TEXT NOT NULL, destination TEXT NOT NULL, sha256 TEXT,
                size INTEGER, undone_at TEXT);
            CREATE TABLE folder_moves (id INTEGER PRIMARY KEY AUTOINCREMENT,
                moved_at TEXT NOT NULL, old_path TEXT NOT NULL,
                new_path TEXT NOT NULL, through_decision_id INTEGER NOT NULL);
            INSERT INTO skips VALUES ('b-notes.txt', '2026-10-01T00:00:00');
            INSERT INTO folders VALUES ('school/economics', 'Econ', 'x');
            INSERT INTO decisions VALUES (1, 'x', 'sort', 'file', 'old.txt',
                'old.txt', 'school/economics', 'school/economics/old.txt',
                NULL, 3, NULL);
            INSERT INTO folder_moves VALUES (1, 'x', 'school/econ',
                'school/economics', 0);
            """
        )
    (library[1] / "school/economics/old.txt").write_text("old")
    app = main.create_app(
        database, library_root=dump.parent, seed=SEED, allow_dev_identity=True
    )
    with TestClient(app) as migrated:
        (project,) = migrated.get("/api/projects").json()["projects"]
        assert (project["name"], project["sorted"]) == ("Downloads", 1)
        assert migrated.get(P + "/current").json()["progress"]["skipped"] == 1
        folders = {f["path"]: f for f in migrated.get(P + "/folders").json()["folders"]}
        assert folders["school/economics"]["description"] == "Econ"
        (row,) = [
            json.loads(line)
            for line in migrated.get(P + "/labels.jsonl").text.splitlines()
        ]
        assert (row["label"], row["project"]) == ("school/economics", "Downloads")
        assert migrated.post(P + "/undo").json()["path"] == "old.txt"
    with sqlite3.connect(database) as upgraded:
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == 5


def test_migration_without_seed_changes_nothing(tmp_path: Path) -> None:
    import sqlite3

    from services.file_sorter.repository import SorterRepository

    database = tmp_path / "old.db"
    with sqlite3.connect(database) as old:
        old.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY, label TEXT)")
    with pytest.raises(RuntimeError):
        SorterRepository(database).initialize(None)
    with sqlite3.connect(database) as untouched:
        tables = {row[0] for row in untouched.execute("SELECT name FROM sqlite_master")}
        assert tables == {"decisions"}
        assert untouched.execute("PRAGMA user_version").fetchone()[0] == 0


# ---------- richer previews ----------


def zip_bytes(members: dict[str, bytes | str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def preview_of(client: TestClient, path: str) -> dict[str, Any]:
    result: dict[str, Any] = current(client, path)["entry"]["preview"]
    return result


def test_csv_becomes_a_table_and_load_more_raises_the_budget(
    client: TestClient, library: Library
) -> None:
    dump, _ = library
    rows = "\n".join(f"{i};name {i};{i * 2}" for i in range(400))
    (dump / "data.csv").write_text("id;name;double\n" + rows)
    table = preview_of(client, "data.csv")
    assert table["mode"] == "table"
    (sheet,) = table["sheets"]
    assert sheet["rows"][0] == ["id", "name", "double"]
    assert sheet["rows"][1] == ["0", "name 0", "0"]
    assert len(sheet["rows"]) == 300 and sheet["truncated"]
    full = client.get(P + "/preview", params={"path": "data.csv"}).json()
    assert len(full["sheets"][0]["rows"]) == 401
    assert not full["sheets"][0]["truncated"]


def test_text_preview_truncates_and_loads_more(
    client: TestClient, library: Library
) -> None:
    dump, _ = library
    (dump / "big.log").write_text("line\n" * 30_000)
    small = preview_of(client, "big.log")
    assert small["mode"] == "text" and small["truncated"]
    assert len(small["text"]) == 20_000
    full = client.get(P + "/preview", params={"path": "big.log"}).json()
    assert len(full["text"]) == 150_000 and not full["truncated"]


def test_excel_shows_every_sheet_as_a_table(
    client: TestClient, library: Library
) -> None:
    dump, _ = library
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel = (
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    )
    (dump / "book.xlsx").write_bytes(
        zip_bytes(
            {
                "xl/workbook.xml": f"<workbook {ns} {rel}><sheets>"
                '<sheet name="Budget" sheetId="1" r:id="rId1"/>'
                '<sheet name="Notes" sheetId="2" r:id="rId2"/></sheets></workbook>',
                "xl/_rels/workbook.xml.rels": "<Relationships>"
                '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                '<Relationship Id="rId2" Target="/xl/worksheets/sheet2.xml"/>'
                "</Relationships>",
                "xl/sharedStrings.xml": f"<sst {ns}><si><t>Item</t></si>"
                "<si><r><t>Co</t></r><r><t>ffee</t></r></si></sst>",
                "xl/worksheets/sheet1.xml": f"<worksheet {ns}><sheetData>"
                '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1"><v>4.5</v></c></row>'
                '<row r="2"><c r="B2" t="s"><v>1</v></c>'
                '<c r="C2" t="b"><v>1</v></c></row>'
                "</sheetData></worksheet>",
                "xl/worksheets/sheet2.xml": f"<worksheet {ns}><sheetData>"
                '<row r="1"><c r="A1" t="inlineStr"><is><t>hello</t></is></c></row>'
                "</sheetData></worksheet>",
            }
        )
    )
    table = preview_of(client, "book.xlsx")
    assert table["mode"] == "table"
    budget, notes = table["sheets"]
    assert budget["name"] == "Budget"
    assert budget["rows"] == [["Item", "", "4.5"], ["", "Coffee", "TRUE"]]
    assert notes == {"name": "Notes", "rows": [["hello"]], "truncated": False}


def test_documents_show_their_embedded_preview(
    client: TestClient, library: Library
) -> None:
    dump, _ = library
    document = (
        '<w:document xmlns:w="w"><w:body><w:p><w:r><w:t>Body</w:t></w:r>'
        "</w:p></w:body></w:document>"
    )
    (dump / "memo.docx").write_bytes(
        zip_bytes({"word/document.xml": document, "docProps/thumbnail.jpeg": b"JPEG"})
    )
    (dump / "plan.pages").write_bytes(
        zip_bytes({"Index/Document.iwa": b"x", "preview.jpg": b"PAGES-JPEG"})
    )
    bundle = dump / "old.key"
    (bundle / "QuickLook").mkdir(parents=True)
    (bundle / "QuickLook" / "Preview.pdf").write_bytes(b"%PDF bundle")
    (dump / "notes.odt").write_bytes(
        zip_bytes(
            {
                "content.xml": '<office:document-content xmlns:office="o" '
                'xmlns:text="t"><text:p>Open text</text:p></office:document-content>',
                "Thumbnails/thumbnail.png": b"PNG",
            }
        )
    )
    memo = preview_of(client, "memo.docx")
    assert (memo["mode"], memo["member"], memo["text"]) == (
        "embedded",
        "docProps/thumbnail.jpeg",
        "Body",
    )
    image = client.get(
        P + "/embedded", params={"path": "memo.docx", "member": memo["member"]}
    )
    assert image.content == b"JPEG"
    assert image.headers["content-type"] == "image/jpeg"
    assert preview_of(client, "plan.pages")["member"] == "preview.jpg"
    key = preview_of(client, "old.key")
    assert (key["mode"], key["media_type"]) == ("embedded", "application/pdf")
    pdf = client.get(
        P + "/embedded", params={"path": "old.key", "member": key["member"]}
    )
    assert pdf.content == b"%PDF bundle"
    odt = preview_of(client, "notes.odt")
    assert (odt["member"], odt["text"]) == ("Thumbnails/thumbnail.png", "Open text")
    for member in ("word/document.xml", "../../etc/passwd", "preview.jpg"):
        response = client.get(
            P + "/embedded", params={"path": "memo.docx", "member": member}
        )
        assert response.status_code == 404


def test_svg_and_markdown_previews(client: TestClient, library: Library) -> None:
    dump, _ = library
    (dump / "logo.svg").write_text("<svg><script>alert(1)</script></svg>")
    (dump / "README.md").write_text("# Title\n\n- item")
    svg = preview_of(client, "logo.svg")
    assert (svg["mode"], svg["media_type"]) == ("image", "image/svg+xml")
    assert "script" in svg["source"]
    raw = client.get(P + "/raw", params={"path": "logo.svg"})
    assert raw.headers["content-security-policy"].startswith("sandbox;")
    markdown = preview_of(client, "README.md")
    assert (markdown["mode"], markdown["text"]) == ("markdown", "# Title\n\n- item")


def test_unknown_extensions_are_sniffed(client: TestClient, library: Library) -> None:
    dump, _ = library
    (dump / "1101_29.SJ.out").write_text("chr1\t100\t200\n")
    (dump / "blob.seb").write_bytes(b"\x00\x01binary\xff" * 20)
    assert preview_of(client, "1101_29.SJ.out")["text"] == "chr1\t100\t200\n"
    assert preview_of(client, "blob.seb") == {"mode": "none"}


def test_archives_mail_and_rich_text(client: TestClient, library: Library) -> None:
    import gzip
    import tarfile

    dump, _ = library
    with tarfile.open(dump / "backup.tar.gz", "w:gz") as archive:
        data = b"inside"
        info = tarfile.TarInfo("folder/file.txt")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    (dump / "log.txt.gz").write_bytes(gzip.compress(b"plain log line\n"))
    (dump / "note.eml").write_bytes(
        b"From: a@example.com\r\nTo: b@example.com\r\nSubject: Hi\r\n"
        b"Content-Type: text/plain\r\n\r\nHello there\r\n"
    )
    (dump / "letter.rtf").write_text(
        r"{\rtf1\ansi{\fonttbl{\f0 Arial;}}\f0 Dear caf\'e9,\par Thanks}"
    )
    (dump / "book.epub").write_bytes(
        zip_bytes(
            {
                "META-INF/container.xml": "<container><rootfiles><rootfile "
                'full-path="OEBPS/content.opf"/></rootfiles></container>',
                "OEBPS/content.opf": '<package><manifest><item id="c1" '
                'href="ch1.xhtml"/></manifest><spine><itemref idref="c1"/>'
                "</spine></package>",
                "OEBPS/ch1.xhtml": "<html><body><p>Chapter one</p></body></html>",
            }
        )
    )
    listing = preview_of(client, "backup.tar.gz")
    assert listing["mode"] == "listing"
    assert listing["items"][0]["path"] == "folder/file.txt"
    gz = preview_of(client, "log.txt.gz")
    assert (gz["mode"], gz["text"]) == ("text", "plain log line\n")
    mail = preview_of(client, "note.eml")["text"]
    assert "Subject: Hi" in mail and "Hello there" in mail
    assert preview_of(client, "letter.rtf")["text"] == "Dear café,\nThanks"
    assert preview_of(client, "book.epub")["text"] == "Chapter one"


def test_large_files_wait_for_an_explicit_load(
    client: TestClient, library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.file_sorter import preview

    dump, _ = library
    monkeypatch.setattr(preview, "LARGE_RAW_BYTES", 10)
    monkeypatch.setattr(main, "DUPLICATE_CHECK_BYTES", 10)
    (dump / "big.mp4").write_bytes(b"0" * 64)
    pdf = preview_of(client, "a-report.pdf")
    assert pdf == {"mode": "pdf", "media_type": "application/pdf", "large": True}
    # Audio and video stream with range requests, so they are never held back.
    assert "large" not in preview_of(client, "big.mp4")
    duplicate = client.get(P + "/duplicate", params={"path": "a-report.pdf"}).json()
    assert duplicate == {"sha256": None, "sorted_at": None, "skipped": "large"}
    forced = client.get(
        P + "/duplicate", params={"path": "a-report.pdf", "force": "true"}
    ).json()
    assert forced["sha256"]


def test_zip_containers_under_other_names_are_listed(
    client: TestClient, library: Library
) -> None:
    dump, _ = library
    (dump / "arrays.npz").write_bytes(zip_bytes({"x.npy": b"x", "y.npy": b"y"}))
    (dump / "photo.heif").write_bytes(b"heif")
    listing = preview_of(client, "arrays.npz")
    assert [item["path"] for item in listing["items"]] == ["x.npy", "y.npy"]
    assert preview_of(client, "photo.heif")["media_type"] == "image/heif"


def sorted_entry(client: TestClient, path: str, base: str = P) -> dict[str, Any]:
    response = client.get(base + "/sorted-entry", params={"path": path})
    assert response.status_code == 200, response.text
    entry: dict[str, Any] = response.json()["entry"]
    return entry


def reclassify(
    client: TestClient,
    entry: dict[str, Any],
    folder: str,
    filename: str | None = None,
    base: str = P,
) -> Any:
    return client.post(
        base + "/reclassify",
        json={
            **guard(entry),
            "folder": folder,
            "filename": filename or entry["name"],
        },
    )


def decision_log(client: TestClient, base: str = P) -> list[dict[str, Any]]:
    response = client.get(base + "/decision-log.jsonl")
    assert response.status_code == 200
    return [json.loads(line) for line in response.text.splitlines()]


def assert_rollback_log(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> None:
    assert len(after) == len(before)
    for original, current in zip(before, after, strict=True):
        original, current = original.copy(), current.copy()
        if original["record_type"] == "indexed_file":
            # A successful pre-move content check survives a failed label write.
            assert current.pop("checked_at") >= original.pop("checked_at")
        assert current == original


def test_reclassification_preserves_history_and_document_identity(
    client: TestClient,
    library: Library,
) -> None:
    import hashlib

    dump, tree = library
    first = classify(client, current(client, "b-notes.txt")["entry"], "work").json()
    original = next(r for r in decision_log(client) if r["record_type"] == "decision")
    assert (
        reclassify(
            client,
            sorted_entry(client, first["destination"]),
            "school/economics",
            "notes.txt",
        ).status_code
        == 200
    )
    assert (tree / "school/economics/notes.txt").read_text() == "EC2101 problem set"
    assert not (tree / "work/b-notes.txt").exists()
    assert not (dump / "b-notes.txt").exists()
    (row,) = export(client)
    assert row["document_id"] == first["decision_id"]
    assert row["replaces_decision_id"] == first["decision_id"]
    assert row["label"] == "school/economics"
    assert row["file_path"] == "sorted/school/economics/notes.txt"
    assert row["relative_path"] == "school/economics/notes.txt"
    assert row["file_status"] == "present"
    assert row["original_name"] == "b-notes.txt"
    assert row["source_path"] == "b-notes.txt"
    assert (
        row["sha256"]
        == hashlib.sha256((tree / row["relative_path"]).read_bytes()).hexdigest()
    )
    decisions = [r for r in decision_log(client) if r["record_type"] == "decision"]
    assert decisions[0] == original  # No rewrite or deletion of the earlier choice.
    assert decisions[1]["previous_destination"] == "work/b-notes.txt"
    assert decisions[1]["replaces_id"] == original["id"]
    assert current(client)["progress"]["sorted"] == 1
    # Undo restores the preceding tree location and its exact earlier label.
    undone = client.post(P + "/undo").json()
    assert undone["area"] == "tree"
    assert undone["path"] == "work/b-notes.txt"
    (row,) = export(client)
    assert (row["label"], row["decision_id"]) == ("work", first["decision_id"])
    assert (tree / "work/b-notes.txt").exists()
    assert client.post(P + "/undo").json()["path"] == "b-notes.txt"
    assert (dump / "b-notes.txt").exists()


def test_corrections_and_undo_follow_later_folder_moves(
    client: TestClient,
    library: Library,
) -> None:
    first = classify(client, current(client, "b-notes.txt")["entry"], "work").json()
    reclassify(client, sorted_entry(client, first["destination"]), "school/economics")
    assert (
        client.post(
            P + "/folders/move",
            json={
                "path": "work",
                "parent": "",
                "name": "office",
            },
        ).status_code
        == 200
    )
    assert (
        client.post(
            P + "/folders/move",
            json={
                "path": "school/economics",
                "parent": "",
                "name": "econ",
            },
        ).status_code
        == 200
    )
    (row,) = export(client)
    assert row["label"] == "econ"
    assert row["decided_label"] == "school/economics"
    assert row["file_path"] == "sorted/econ/b-notes.txt"
    reclassify(client, sorted_entry(client, "econ/b-notes.txt"), "office")
    (row,) = export(client)
    assert row["document_id"] == first["decision_id"]
    assert row["decided_label"] == "office"
    assert client.post(P + "/undo").json()["path"] == "econ/b-notes.txt"
    assert client.post(P + "/undo").json()["path"] == "office/b-notes.txt"
    (row,) = export(client)
    assert row["document_id"] == first["decision_id"]
    assert row["decided_label"] == "work"
    assert row["label"] == "office"


def test_pre_existing_file_correction_and_undo_keep_untidy_name(
    client: TestClient,
    library: Library,
) -> None:
    tree = library[1]
    (tree / "work/ old.txt ").write_text("keep")
    assert (
        reclassify(
            client, sorted_entry(client, "work/ old.txt "), "", "tidy.txt"
        ).status_code
        == 200
    )
    (row,) = export(client)
    assert row["label_source"] == "owner_reclassified"
    assert row["document_id"] == row["decision_id"]
    assert row["file_path"] == "sorted/tidy.txt"
    assert row["source_path"] is None
    assert client.post(P + "/undo").json()["path"] == "work/ old.txt "
    response = client.get(P + "/sorted-entry", params={"path": "work/ old.txt "})
    assert response.json()["progress"]["sorted"] == 0
    assert (tree / "work/ old.txt ").read_text() == "keep"
    (row,) = export(client)
    assert row["label_source"] == "pre_existing"
    assert row["document_id"] is not None
    assert row["needs_review"] and not row["training_eligible"]


def test_reclassify_a_discarded_file(client: TestClient, library: Library) -> None:
    entry = current(client, "b-notes.txt")["entry"]
    client.post(P + "/discard", json=guard(entry))
    assert client.get(P + "/sorted", params={"folder": "_discarded"}).json()["entries"]
    assert (
        reclassify(
            client, sorted_entry(client, "_discarded/b-notes.txt"), "work"
        ).status_code
        == 200
    )
    assert current(client)["progress"]["discarded"] == 0
    assert current(client)["progress"]["sorted"] == 1
    assert client.post(P + "/undo").json()["path"] == "_discarded/b-notes.txt"
    (row,) = export(client)
    assert row["label"] == "_discarded"
    assert (library[1] / "_discarded/b-notes.txt").exists()


def test_discarded_folder_units_are_present_in_label_export(client: TestClient) -> None:
    entry = current(client, "c-project")["entry"]
    assert client.post(P + "/discard", json=guard(entry)).status_code == 200
    (row,) = export(client)
    assert row["kind"] == "folder"
    assert row["file_status"] == "present"
    assert row["relative_path"] == "_discarded/c-project"
    assert (
        client.get(P + "/sorted", params={"folder": "_discarded/c-project"}).status_code
        == 400
    )


def test_shared_tree_corrections_preserve_original_source_project(
    client: TestClient,
    library: Library,
) -> None:
    (library[0].parent / "other-dump").mkdir()
    project = make_project(client, "Other", "other-dump", "sorted", "files").json()
    base = f"/api/projects/{project['id']}"
    first = classify(client, current(client, "b-notes.txt")["entry"], "work").json()
    assert (
        reclassify(
            client,
            sorted_entry(client, first["destination"], base),
            "school",
            base=base,
        ).status_code
        == 200
    )
    assert (
        client.get(base + "/labels.jsonl").text == client.get(P + "/labels.jsonl").text
    )
    (row,) = export(client)
    assert row["project"] == "Downloads"
    assert row["source"] == "dump"
    assert row["source_path"] == "b-notes.txt"
    assert row["document_id"] == first["decision_id"]
    assert client.post(P + "/undo").status_code == 404
    assert client.post(base + "/undo").json()["path"] == "work/b-notes.txt"
    assert client.post(P + "/undo").json()["path"] == "b-notes.txt"


def test_reclassification_refuses_stale_preview_noop_and_overwrite(
    client: TestClient,
    library: Library,
) -> None:
    tree = library[1]
    (tree / "work/old.txt").write_text("old")
    entry = sorted_entry(client, "work/old.txt")
    before = decision_log(client)
    assert reclassify(client, entry, "work").status_code == 400
    (tree / "school/old.txt").write_text("keep")
    assert reclassify(client, entry, "school").status_code == 409
    (tree / "work/old.txt").write_text("new contents")
    assert reclassify(client, entry, "school/economics").status_code == 409
    assert decision_log(client) == before
    assert (tree / "school/old.txt").read_text() == "keep"


@pytest.mark.parametrize(
    "path",
    [
        "../dump/b-notes.txt",
        "/work/old.txt",
        "work/../work/old.txt",
        "work/.secret",
        "_routing/secret",
        "work/link",
    ],
)
def test_tree_preview_and_reclassification_refuse_unsafe_sources(
    client: TestClient,
    library: Library,
    path: str,
) -> None:
    tree = library[1]
    (tree / "work/old.txt").write_text("old")
    (tree / "work/.secret").write_text("hidden")
    (tree / "_routing").mkdir()
    (tree / "_routing/secret").write_text("private")
    (tree / "work/link").symlink_to(library[0] / "b-notes.txt")
    for route in ("sorted-entry", "preview", "raw"):
        response = client.get(
            P + "/" + route,
            params={"path": path, "area": "tree"}
            if route != "sorted-entry"
            else {"path": path},
        )
        assert response.status_code in {400, 404}
    entry = {"path": path, "size": 3, "mtime_ns": "1", "name": "old.txt"}
    assert reclassify(client, entry, "school").status_code in {400, 404}
    assert not any(r["record_type"] == "decision" for r in decision_log(client))


def test_tree_browser_is_paged_and_never_opens_sorted_folder_units(
    client: TestClient,
    library: Library,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.file_sorter import library as fs

    monkeypatch.setattr(fs, "MAX_BROWSE", 2)
    tree = library[1]
    for name in ("a.txt", "b.txt", "c.txt"):
        (tree / "work" / name).write_text(name)
    first = client.get(P + "/sorted", params={"folder": "work"}).json()
    assert [r["name"] for r in first["entries"]] == ["a.txt", "b.txt"]
    assert first["more"] is True
    second = client.get(P + "/sorted", params={"folder": "work", "offset": 2}).json()
    assert [r["name"] for r in second["entries"]] == ["c.txt"]
    assert second["more"] is False
    filtered = client.get(
        P + "/sorted", params={"folder": "work", "search": "C.TXT"}
    ).json()
    assert [r["name"] for r in filtered["entries"]] == ["c.txt"]
    classify(client, current(client, "c-project")["entry"], "work")
    assert (
        client.get(P + "/sorted", params={"folder": "work/c-project"}).status_code
        == 400
    )
    assert (
        client.get(
            P + "/sorted-entry", params={"path": "work/c-project/src/main.py"}
        ).status_code
        == 400
    )
    assert (
        reclassify(
            client, sorted_entry(client, "work/a.txt"), "work/c-project"
        ).status_code
        == 400
    )


def test_correction_rolls_back_move_when_label_write_fails(
    client: TestClient,
    library: Library,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    classify(client, current(client, "b-notes.txt")["entry"], "work")
    entry = sorted_entry(client, "work/b-notes.txt")
    before = decision_log(client)

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.IntegrityError("fixture label failure")

    monkeypatch.setattr(SorterRepository, "record", fail)
    with pytest.raises(sqlite3.IntegrityError):
        reclassify(client, entry, "school")
    assert (library[1] / "work/b-notes.txt").exists()
    assert not (library[1] / "school/b-notes.txt").exists()
    assert_rollback_log(before, decision_log(client))


def test_undo_correction_rolls_back_when_log_write_fails(
    client: TestClient,
    library: Library,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    classify(client, current(client, "b-notes.txt")["entry"], "work")
    reclassify(client, sorted_entry(client, "work/b-notes.txt"), "school")
    before = decision_log(client)

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.IntegrityError("fixture undo failure")

    monkeypatch.setattr(SorterRepository, "mark_undone", fail)
    with pytest.raises(sqlite3.IntegrityError):
        client.post(P + "/undo")
    assert (library[1] / "school/b-notes.txt").exists()
    assert not (library[1] / "work/b-notes.txt").exists()
    assert_rollback_log(before, decision_log(client))


def test_missing_logged_file_is_flagged_without_changing_its_label(
    client: TestClient,
    library: Library,
) -> None:
    classify(client, current(client, "b-notes.txt")["entry"], "work")
    before = decision_log(client)
    (library[1] / "work/b-notes.txt").rename(library[1] / "school/b-notes.txt")
    rows = export(client)
    logged = next(r for r in rows if r["decision_id"])
    assert logged["file_status"] == "missing"
    assert logged["label"] == "work"
    assert (
        next(r for r in rows if r["label_source"] == "pre_existing")["document_id"]
        is None
    )
    assert decision_log(client) == before


def test_migrates_version_one_without_rewriting_decisions(
    tmp_path: Path, library: Library
) -> None:
    database = tmp_path / "v1.db"
    with sqlite3.connect(database) as old:
        old.executescript(SCHEMA)
        old.execute("PRAGMA user_version = 1")
        old.execute(
            "INSERT INTO projects VALUES "
            "(1, 'Downloads', 'dump', 'sorted', 'top', 'before', NULL)"
        )
        old.execute(
            "INSERT INTO decisions VALUES (1, 'before', 'sort', 'file', "
            "'b-notes.txt', 'b-notes.txt', 'work', 'work/b-notes.txt', "
            "NULL, 18, NULL, 1, 'sorted')"
        )
        original = old.execute("SELECT * FROM decisions").fetchone()
    (library[0] / "b-notes.txt").rename(library[1] / "work/b-notes.txt")
    app = main.create_app(
        database, library_root=library[0].parent, allow_dev_identity=True
    )
    for _ in range(2):
        with TestClient(app) as migrated:
            (row,) = export(migrated)
            assert row["document_id"] == 1
            assert row["file_status"] == "present"
        with sqlite3.connect(database) as upgraded:
            assert (
                upgraded.execute("SELECT * FROM decisions").fetchone()[: len(original)]
                == original
            )
            assert upgraded.execute("PRAGMA user_version").fetchone()[0] == 5


def test_tree_routes_still_require_admin_session(
    tmp_path: Path, library: Library
) -> None:
    with session_app(
        tmp_path, library, lambda s: main.Identity("member", "member", "MEMBER")
    ) as client:
        for route in ("sorted", "sorted-entry?path=work/old.txt", "decision-log.jsonl"):
            assert as_caller(client, P + "/" + route, "member.sig") == 403
        assert as_caller(client, P + "/reclassify", "member.sig", "POST") == 403


def test_sorted_previews_keep_same_sandbox_and_bounds(
    client: TestClient, library: Library
) -> None:
    (library[1] / "work/page.html").write_text("<script>alert(1)</script>")
    entry = sorted_entry(client, "work/page.html")
    assert entry["preview"]["mode"] == "html"
    response = client.get(P + "/raw", params={"path": entry["path"], "area": "tree"})
    assert response.status_code == 200
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert (
        client.get(
            P + "/preview", params={"path": entry["path"], "area": "tree"}
        ).status_code
        == 200
    )

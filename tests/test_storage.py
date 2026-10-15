import hashlib
import zipfile
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.storage import (
    StoragePolicyError,
    copy_workspace_entry,
    delete_workspace_entry,
    is_logical_storage_path,
    member_storage_entries,
    member_storage_path,
    member_storage_path_allowed,
    member_workspace_path,
    move_workspace_entry,
    resolve_logical_storage_path,
    resolve_storage_path,
)


def test_storage_routes_browse_reference_download_and_package_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = tmp_path / "nas"
    project = storage / "projects" / "demo"
    inputs = storage / "inputs"
    project.mkdir(parents=True)
    inputs.mkdir()
    (project / "submit.hp").write_text(
        """#!/usr/bin/env bash
#HP --version 1
#HP --name "NAS array"
#HP --runtime scientific-python:1
#HP --cpus 1
#HP --memory-mb 512
#HP --time-limit 00:05:00
#HP --input dataset
#HP --array 1-2
echo "$HOME_PLATFORM_ARRAY_INDEX"
"""
    )
    content = b"sample,value\na,1\n"
    (inputs / "cohort.csv").write_bytes(content)
    monkeypatch.setenv("HOME_PLATFORM_STORAGE_DIR", str(storage))
    monkeypatch.setenv("HOME_PLATFORM_UPLOAD_DIR", str(tmp_path / "uploads"))

    with TestClient(create_app(tmp_path / "jobs.db")) as client:
        listing = client.get("/storage", params={"path": "inputs"})
        assert listing.status_code == 200
        assert listing.json()["entries"] == [
            {
                "name": "cohort.csv",
                "path": "inputs/cohort.csv",
                "kind": "file",
                "size_bytes": len(content),
            }
        ]

        reference = client.post(
            "/storage/references",
            json={"path": "inputs/cohort.csv"},
        )
        assert reference.status_code == 200
        assert reference.json() == {
            "storage_id": "home-storage",
            "path": "inputs/cohort.csv",
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
        }

        downloaded = client.get("/storage/files/inputs/cohort.csv")
        assert downloaded.status_code == 200
        assert downloaded.content == content

        packaged = client.post(
            "/storage/project-uploads",
            json={"path": "projects/demo"},
        )
        assert packaged.status_code == 201
        archive = (
            tmp_path / "uploads" / "projects" / f"{packaged.json()['upload_id']}.zip"
        )
        with zipfile.ZipFile(archive) as bundle:
            assert bundle.namelist() == ["submit.hp"]

        client.post(
            "/workers/heartbeat",
            json={"worker_id": "worker-a", "supported_types": ["batch"]},
        )
        client.put(
            "/workers/worker-a/capacity",
            json={"max_job_cpu": 2, "max_job_memory_mb": 1024},
        )
        submitted = client.post(
            "/batch-submissions",
            json={
                "project": packaged.json(),
                "entrypoint": "submit.hp",
                "inputs": {"dataset": reference.json()},
            },
        )
        assert submitted.status_code == 201, submitted.text
        assert len(submitted.json()["tasks"]) == 2
        assert submitted.json()["tasks"][0]["parameters"]["inputs"]["dataset"] == (
            reference.json()
        )


def test_storage_resolution_rejects_escape_and_symlink(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "nas"
    storage.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    (storage / "link").symlink_to(outside)

    with pytest.raises(StoragePolicyError, match=r"escapes|symbolic"):
        resolve_storage_path(storage, "link")
    with pytest.raises(StoragePolicyError, match="stay inside"):
        resolve_storage_path(storage, "../outside.txt")


def test_member_storage_maps_only_home_and_shared(tmp_path: Path) -> None:
    user_id = UUID("00000000-0000-0000-0000-000000000123")
    root = tmp_path / "nas"
    home = root / "users" / str(user_id)
    shared = root / "shared"
    home.mkdir(parents=True)
    shared.mkdir()
    (home / "private.txt").write_text("private")
    (shared / "common.txt").write_text("shared")

    assert member_storage_path(user_id, "Home/private.txt") == (
        f"users/{user_id}/private.txt"
    )
    assert member_storage_path(user_id, "Shared/common.txt") == "shared/common.txt"
    assert [entry.path for entry in member_storage_entries(root, user_id)] == [
        "Home",
        "Shared",
    ]
    assert [entry.path for entry in member_storage_entries(root, user_id, "Home")] == [
        "Home/private.txt"
    ]
    assert member_storage_path_allowed(user_id, f"users/{user_id}/private.txt")
    assert member_storage_path_allowed(user_id, "shared/common.txt")
    assert not member_storage_path_allowed(
        user_id, "users/00000000-0000-0000-0000-000000000999/private.txt"
    )
    with pytest.raises(StoragePolicyError, match="Home or Shared"):
        member_storage_path(user_id, "users/someone-else/private.txt")

    assert member_workspace_path(user_id, "Home/Workspace/Projects/demo") == (
        f"users/{user_id}/Workspace/Projects/demo"
    )
    with pytest.raises(StoragePolicyError, match="Home/Workspace"):
        member_workspace_path(user_id, "Home/Personal/demo")


def test_logical_paths_resolve_the_same_way_for_members_and_administrators() -> None:
    user_id = UUID("2f1b8c34-9a6d-4d2e-8a51-1c0f7b3d9e42")

    # A member's Home is their own tree and needs no further qualification.
    assert resolve_logical_storage_path("Home/notes.csv", user_id=user_id) == (
        f"users/{user_id}/notes.csv"
    )
    # An administrator reaches every tree, so Home alone is ambiguous and the
    # account has to be named in the path.
    assert resolve_logical_storage_path(f"Home/{user_id}/notes.csv", user_id=None) == (
        f"users/{user_id}/notes.csv"
    )
    with pytest.raises(StoragePolicyError, match="must name the account"):
        resolve_logical_storage_path("Home", user_id=None)
    with pytest.raises(StoragePolicyError, match="stable user ID"):
        resolve_logical_storage_path("Home/not-a-uuid/notes.csv", user_id=None)

    # Shared means the same directory to both callers.
    for caller in (user_id, None):
        assert resolve_logical_storage_path("Shared/ref.fa", user_id=caller) == (
            "shared/ref.fa"
        )

    # Traversal is rejected before any filesystem access.
    with pytest.raises(StoragePolicyError, match="stay inside Home or Shared"):
        resolve_logical_storage_path("Shared/../users/other", user_id=None)


def test_only_home_and_shared_prefixes_count_as_logical_paths() -> None:
    assert is_logical_storage_path("Home/notes.csv")
    assert is_logical_storage_path("shared/ref.fa")
    assert is_logical_storage_path("SHARED")
    # The transitional Pi share's own directories must keep working untouched.
    assert not is_logical_storage_path("inputs/cohort.csv")
    assert not is_logical_storage_path("projects/demo")
    assert not is_logical_storage_path("users/abc/notes.csv")
    assert not is_logical_storage_path("")


def test_workspace_file_operations_stay_inside_the_member_tree(tmp_path: Path) -> None:
    user_id = UUID("00000000-0000-0000-0000-000000000123")
    other_id = UUID("00000000-0000-0000-0000-000000000999")
    root = tmp_path / "nas"
    workspace = root / "users" / str(user_id) / "Workspace"
    other = root / "users" / str(other_id) / "Workspace"
    (workspace / "project").mkdir(parents=True)
    other.mkdir(parents=True)
    (workspace / "project" / "run.py").write_text("print('ok')\n")
    (other / "private.txt").write_text("secret")

    move_workspace_entry(
        root,
        user_id,
        "Home/Workspace/project/run.py",
        "Home/Workspace/project/train.py",
    )
    copied = copy_workspace_entry(
        root,
        user_id,
        "Home/Workspace/project",
        "Home/Workspace/project-copy",
        max_bytes=1024,
    )
    freed = delete_workspace_entry(root, user_id, "Home/Workspace/project-copy")

    assert copied == len("print('ok')\n")
    assert freed == copied
    assert (workspace / "project" / "train.py").is_file()
    assert not (workspace / "project-copy").exists()
    assert (other / "private.txt").read_text() == "secret"

    with pytest.raises(StoragePolicyError, match="Home/Workspace"):
        move_workspace_entry(
            root,
            user_id,
            f"Home/Workspace/../../{other_id}/Workspace/private.txt",
            "Home/Workspace/stolen.txt",
        )
    with pytest.raises(StoragePolicyError, match="cannot be changed"):
        delete_workspace_entry(root, user_id, "Home/Workspace")

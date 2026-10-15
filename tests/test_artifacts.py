"""Tests for the readable artifact layout.

The directory name is built from a job name, which is user-supplied and neither
unique nor path-safe. These tests exist because every one of those properties is
a way to escape the owner's artifact root or to collide with another run.
"""

from datetime import UTC, datetime
from pathlib import PurePosixPath
from uuid import UUID

import pytest

from app.artifacts import (
    FALLBACK_SLUG,
    MAX_SLUG_LENGTH,
    artifact_slug,
    job_artifact_relative_path,
    run_directory_name,
)
from contracts.models import JobRead, JobStatus, JobType, SleepParameters

JOB_ID = UUID("7dcf9099-4204-42d9-928e-b31929cb0a0e")
GROUP_ID = UUID("3b2e91c4-1f5a-4b7e-9d20-6c8a4e2f0b13")


def job(
    name: str | None,
    *,
    job_id: UUID = JOB_ID,
    group_id: UUID | None = None,
    task_id: str | None = None,
) -> JobRead:
    now = datetime.now(UTC)
    return JobRead(
        id=job_id,
        name=name,
        type=JobType.SLEEP,
        parameters=SleepParameters(seconds=1),
        group_id=group_id,
        task_id=task_id,
        status=JobStatus.QUEUED,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("SVM_model", "SVM_model"),
        ("SVM model", "SVM-model"),
        ("cohort analysis  v2", "cohort-analysis-v2"),
        # Path separators must never survive into a directory name.
        ("../../etc/passwd", "etc-passwd"),
        ("nested/run", "nested-run"),
        # Transliterate rather than discard, so the owner still recognises it.
        ("café-training", "cafe-training"),
        # Leading dots would hide the directory or alias its parent.
        (".hidden", "hidden"),
        ("...", FALLBACK_SLUG),
        ("..", FALLBACK_SLUG),
        (".", FALLBACK_SLUG),
        # Nothing usable survives, so fall back rather than emit an empty name.
        ("", FALLBACK_SLUG),
        ("   ", FALLBACK_SLUG),
        ("///", FALLBACK_SLUG),
        ("日本語", FALLBACK_SLUG),
        (None, FALLBACK_SLUG),
    ],
)
def test_slug_reduces_any_name_to_one_safe_segment(
    name: str | None, expected: str
) -> None:
    slug = artifact_slug(name)
    assert slug == expected
    assert "/" not in slug
    assert slug not in {"", ".", ".."}
    assert not slug.startswith(".")


def test_slug_is_bounded_so_the_directory_name_always_fits() -> None:
    slug = artifact_slug("x" * 500)
    assert len(slug) == MAX_SLUG_LENGTH
    # The suffix still has room inside any 255-byte filesystem limit.
    assert len(run_directory_name("x" * 500, JOB_ID)) < 100


def test_two_runs_sharing_one_name_get_separate_directories() -> None:
    other_id = UUID("1a4be012-55c7-4f8b-9a3e-2d7c6b0f4188")
    first = job_artifact_relative_path(job("SVM_model"))
    second = job_artifact_relative_path(job("SVM_model", job_id=other_id))

    assert first == PurePosixPath("SVM_model-7dcf9099")
    assert second == PurePosixPath("SVM_model-1a4be012")
    assert first != second


def test_array_children_nest_under_their_submission() -> None:
    """One run reads as one directory, not four unrelated siblings."""
    paths = [
        job_artifact_relative_path(
            job("child task", group_id=GROUP_ID, task_id=str(index)),
            group_name="cohort analysis",
        )
        for index in range(1, 5)
    ]

    assert paths == [
        PurePosixPath("cohort-analysis-3b2e91c4", str(index)) for index in range(1, 5)
    ]
    # Every child shares one parent, so eviction removes the run as a whole.
    assert {path.parent for path in paths} == {
        PurePosixPath("cohort-analysis-3b2e91c4")
    }


def test_array_children_are_keyed_by_group_not_by_child_id() -> None:
    """Children differ by id but must still land in the same run directory."""
    sibling = UUID("9a2f11bc-4d3e-4a10-b6c5-8e7f2d1a0c34")
    first = job_artifact_relative_path(
        job("first child", group_id=GROUP_ID, task_id="1"), group_name="run"
    )
    second = job_artifact_relative_path(
        job("second child", job_id=sibling, group_id=GROUP_ID, task_id="2"),
        group_name="run",
    )

    assert first.parent == second.parent


def test_an_unsafe_task_id_is_refused_rather_than_sanitised() -> None:
    # The API validates TaskId on the way in, so reaching this means something
    # bypassed it. Refuse instead of quietly writing somewhere else.
    unsafe = job("run", group_id=GROUP_ID, task_id="1")
    object.__setattr__(unsafe, "task_id", "../escape")
    with pytest.raises(ValueError, match="unsafe task id"):
        job_artifact_relative_path(unsafe, group_name="run")


def test_grouped_job_requires_the_persisted_parent_name() -> None:
    grouped = job("child name", group_id=GROUP_ID, task_id="1")

    with pytest.raises(ValueError, match="group name is required"):
        job_artifact_relative_path(grouped)

"""Human-readable placement for published job results.

Results used to land in `<owner-id>/<job-id>/`. That is unambiguous but
unreadable: over SMB an owner sees a wall of UUIDs and cannot tell which
directory holds the SVM run they started this morning. This module derives a
directory name from the job's own name instead, keeping the UUID only as a
short disambiguating suffix.

The suffix is not decoration. Job names are neither unique nor trusted — two
runs may legitimately share one name, and `JobName` permits spaces, slashes and
unicode — so the name alone can be neither a key nor a path segment. Deriving
the whole path from the job record keeps it deterministic: no directory scan, no
collision counter, and no ordering dependency between concurrent array children.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath
from uuid import UUID

from contracts.models import JobRead

# Long enough that a collision within one owner is implausible, short enough
# that the readable part of the name still leads.
SHORT_ID_LENGTH = 8

# Bounds the directory name well inside the 255-byte limit every target
# filesystem shares, leaving room for the suffix.
MAX_SLUG_LENGTH = 60

FALLBACK_SLUG = "job"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_REPEATED_DASH = re.compile(r"-{2,}")
_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def artifact_slug(name: str | None) -> str:
    """Reduce an arbitrary job name to one safe, readable path segment.

    `JobName` only forbids control characters, so this receives spaces, path
    separators, leading dots and arbitrary unicode. Transliterate rather than
    strip, so a non-ASCII name still yields something its owner recognises, and
    allow-list the result rather than blocklisting the characters we happened to
    think of.
    """
    decomposed = unicodedata.normalize("NFKD", name or "")
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    slug = _REPEATED_DASH.sub("-", _UNSAFE.sub("-", ascii_only)).strip("-._")
    slug = slug[:MAX_SLUG_LENGTH].strip("-._")
    # "." and ".." survive every rule above and would escape or alias the
    # parent directory, so they are rejected by value rather than by pattern.
    if not slug or set(slug) <= {"."}:
        return FALLBACK_SLUG
    return slug


def run_directory_name(name: str | None, run_id: UUID) -> str:
    """Name the directory holding one submission's results."""
    return f"{artifact_slug(name)}-{run_id.hex[:SHORT_ID_LENGTH]}"


def job_artifact_relative_path(
    job: JobRead, *, group_name: str | None = None
) -> PurePosixPath:
    """Locate one job's results beneath its owner's artifact root.

    A standalone job owns its directory outright. An array child instead nests
    under the directory of the submission that created it, so the four results
    of a four-child array read as one run rather than four unrelated siblings.
    The parent name must come from the persisted group: the general group API
    permits children to have distinct display names.
    """
    if job.group_id is None or job.task_id is None:
        return PurePosixPath(run_directory_name(job.name, job.id))
    if group_name is None:
        raise ValueError("group name is required for a grouped job")
    task_id = job.task_id
    if _SAFE_TASK_ID.match(task_id) is None:
        # Unreachable through the API, which validates TaskId on the way in.
        # Checked anyway because this value builds a path.
        raise ValueError(f"unsafe task id {task_id!r}")
    return PurePosixPath(run_directory_name(group_name, job.group_id), task_id)


def legacy_artifact_relative_path(job_id: UUID) -> PurePosixPath:
    """The pre-0.33.0 layout, still read so existing results stay reachable."""
    return PurePosixPath(str(job_id))

"""Persistent content inventory; decisions remain the classification history.

Only sorter-recorded moves establish identity across paths. Observed edits and
external moves are discoveries, never guesses based on matching hashes.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from services.file_sorter.duplicates import Copy, DuplicateReview, Problem, vanished
from services.file_sorter.library import LOCK, LibraryError, LibraryRoot
from services.file_sorter.repository import Decision, Project, SorterRepository


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def allocate(connection: sqlite3.Connection) -> int:
    cursor = connection.execute("INSERT INTO documents(created_at) VALUES(?)", (now(),))
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def event(
    connection: sqlite3.Connection,
    base: str,
    path: str,
    document_id: int,
    kind: str,
    digest: str | None,
    previous: int | None = None,
) -> None:
    connection.execute(
        "INSERT INTO document_events(observed_at,target,path,document_id,"
        "previous_document_id,event,sha256) VALUES(?,?,?,?,?,?,?)",
        (now(), base, path, document_id, previous, kind, digest),
    )


def bind_decision(connection: sqlite3.Connection, decision: Decision) -> None:
    """Called inside the decision transaction, after the actual rename."""
    if decision.kind != "file":
        return
    if decision.previous_destination is not None:
        source = f"{decision.target}/{decision.previous_destination}"
    else:
        project = connection.execute(
            "SELECT source FROM projects WHERE id = ?", (decision.project_id,)
        ).fetchone()
        source = f"{project['source']}/{decision.original_name}" if project else ""
    destination = f"{decision.target}/{decision.destination}"
    previous = connection.execute(
        "SELECT * FROM file_index WHERE path = ?", (source,)
    ).fetchone()
    digest = decision.sha256
    if (
        digest is None
        and previous
        and previous["document_id"] == decision.document_id
        and previous["verified"]
        and previous["size"] == decision.size
        and previous["mtime_ns"] == decision.file_mtime_ns
    ):
        digest = previous["sha256"]
    if source != destination:
        connection.execute(
            "DELETE FROM file_index WHERE path = ? AND document_id = ?",
            (source, decision.document_id),
        )
    connection.execute(
        "INSERT OR REPLACE INTO file_index VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            destination,
            decision.target,
            decision.destination,
            decision.document_id,
            decision.id,
            decision.size,
            decision.file_mtime_ns,
            digest,
            int(digest is not None and decision.file_mtime_ns is not None),
            1,
            "manual",
            1,
            "manual",
            now(),
        ),
    )
    event(
        connection,
        decision.target,
        destination,
        decision.document_id or decision.id,
        "manual_decision",
        digest,
    )
    connection.execute("UPDATE index_state SET epoch = epoch + 1 WHERE id = 1")


def restore_binding(
    connection: sqlite3.Connection, decision: Decision, prior: Decision | None
) -> None:
    source = f"{decision.target}/{decision.destination}"
    row = connection.execute(
        "SELECT * FROM file_index WHERE path = ?", (source,)
    ).fetchone()
    if row is None or row["document_id"] != decision.document_id:
        return
    if decision.previous_destination is not None:
        base = decision.target
        relative = decision.previous_destination
    else:
        project = connection.execute(
            "SELECT source FROM projects WHERE id = ?", (decision.project_id,)
        ).fetchone()
        if project is None:
            return
        base, relative = project["source"], decision.original_name
    destination = f"{base}/{relative}"
    connection.execute("DELETE FROM file_index WHERE path = ?", (source,))
    values = dict(row)
    values.update(
        path=destination,
        base=base,
        relative_path=relative,
        decision_id=prior.id if prior else None,
        provenance="manual" if prior else "discovered",
        reviewed=int(prior is not None),
        reason="manual" if prior else "new_document",
    )
    connection.execute(
        "INSERT OR REPLACE INTO file_index VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        tuple(values.values()),
    )
    event(
        connection,
        decision.target,
        destination,
        row["document_id"],
        "undo",
        row["sha256"],
    )
    connection.execute("UPDATE index_state SET epoch = epoch + 1 WHERE id = 1")


class ScanPaused(Exception):
    """A whole-scan stop (duplicate recovery pending, shutdown)."""


class FileIndex:
    settle_seconds = 1.0
    # Files that were mid-write are retried soon rather than in five minutes.
    retry_seconds = 60.0
    progress_seconds = 1.0

    def __init__(self, repository: SorterRepository, root: LibraryRoot) -> None:
        self.repository = repository
        self.root = root
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._audit = threading.Event()
        self._scan_lock = threading.Lock()
        self._retry_soon = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="sorter-index", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)

    def request(self, verify_all: bool = False) -> None:
        if verify_all:
            self._audit.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            verify_all = self._audit.is_set()
            self._audit.clear()
            try:
                self.scan(verify_all=verify_all)
            except Exception as error:  # the worker must survive anything
                # Nothing may end this thread: record it and try again later.
                self._finish_with_error(f"Unexpected index error: {error!r}")
            self._wake.wait(self.retry_seconds if self._retry_soon else 300)

    def _finish_with_error(self, message: str) -> None:
        try:
            with self.repository._connect() as connection:
                connection.execute(
                    "UPDATE index_state SET running=0,error=? WHERE id=1", (message,)
                )
        except sqlite3.Error:
            pass

    def lookup(self, base: str, relative: str) -> dict[str, Any] | None:
        with self.repository._connect() as connection:
            row = connection.execute(
                "SELECT * FROM file_index WHERE path = ?", (f"{base}/{relative}",)
            ).fetchone()
        return dict(row) if row else None

    def observe(
        self, base: str, copy: Copy, digest: str | None, prior: Decision | None = None
    ) -> dict[str, Any]:
        """Publish a stable observation, retaining identity only at this path."""
        path = f"{base}/{copy.path}"
        with self.repository._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT * FROM file_index WHERE path = ?", (path,)
            ).fetchone()
            # Legacy bindings lack snapshot mtime; their recorded hash is evidence.
            same = bool(
                previous
                and previous["present"]
                and (
                    (previous["sha256"] is not None and previous["sha256"] == digest)
                    or (previous["sha256"] is None and previous["mtime_ns"] is None)
                    or (
                        previous["sha256"] is None
                        and previous["size"] == copy.size
                        and previous["mtime_ns"] == copy.mtime_ns
                    )
                )
            )
            bootstrap = previous is None and prior is not None
            mismatch = (
                bootstrap
                and prior is not None
                and prior.sha256 is not None
                and prior.sha256 != digest
            )
            if same:
                assert previous is not None
                document_id = previous["document_id"]
                decision_id = previous["decision_id"]
                provenance, reviewed, reason = (
                    previous["provenance"],
                    previous["reviewed"],
                    previous["reason"],
                )
            elif bootstrap and not mismatch:
                assert prior is not None
                document_id, decision_id = prior.document_id or prior.id, prior.id
                # Without a recorded hash (or a matching size and time), the
                # owner's choice still stands, but nothing proves these are the
                # bytes that were labelled: a baseline, not verified content.
                confirmed = prior.sha256 is not None or (
                    prior.file_mtime_ns is not None
                    and prior.file_mtime_ns == copy.mtime_ns
                    and prior.size == copy.size
                )
                provenance, reviewed = "manual", 1
                reason = "manual" if confirmed else "legacy_baseline"
            else:
                document_id, decision_id = allocate(connection), None
                provenance, reviewed = "discovered", 0
                reason = "content_changed" if previous or mismatch else "new_document"
            if not same:
                old_id = (
                    previous["document_id"]
                    if previous
                    else (prior.document_id if mismatch and prior else None)
                )
                event(connection, base, path, document_id, reason, digest, old_id)
            elif previous and previous["sha256"] is None and digest is not None:
                event(connection, base, path, document_id, "content_verified", digest)
            connection.execute(
                "INSERT OR REPLACE INTO file_index VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    path,
                    base,
                    copy.path,
                    document_id,
                    decision_id,
                    copy.size,
                    copy.mtime_ns,
                    digest,
                    int(digest is not None),
                    1,
                    provenance,
                    reviewed,
                    reason,
                    now(),
                ),
            )
        result = self.lookup(base, copy.path)
        assert result is not None
        return result

    def ensure(self, project: Project, area: str, path: str) -> dict[str, Any]:
        """Fresh observation for a human action, called with the rename lock."""
        library = self.root.project(project.source, project.target, project.mode)
        units = {
            d.destination
            for d in self.repository.tree_decisions(project.target)
            if d.kind == "folder"
        }
        review = DuplicateReview(project, library, self.repository, units)
        copy = review.copy(area, path)
        digest = review.digest(copy, fresh=True)
        base = project.source if area == "dump" else project.target
        # The latest decision at this path, as the background scan uses.
        prior = next(
            (
                d
                for d in reversed(self.repository.tree_decisions(project.target))
                if area == "tree" and d.destination == path
            ),
            None,
        )
        return self.observe(base, copy, digest, prior)

    def tree_files(self, project: Project) -> list[dict[str, Any]] | None:
        """Present tree files from the index; None until a scan has covered it.

        Sorted review lists these rather than walking the NAS on every request
        (the walk costs one metadata round-trip per path segment of every
        file). The sorter's own sorts, corrections, confirmations, undos and
        folder moves update these rows in the same transaction; outside
        changes arrive with the next scan.
        """
        with self.repository._connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM index_roots WHERE base = ?", (project.target,)
            ).fetchone():
                return None
            rows = connection.execute(
                "SELECT * FROM file_index WHERE base = ? AND present = 1",
                (project.target,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def indexed_review_state(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "document_id": row["document_id"],
            "decision_id": row["decision_id"],
            "reviewed": bool(row["reviewed"] and row["present"]),
            "review_reason": row["reason"],
            "classification_provenance": row["provenance"],
        }

    def review_state(self, project: Project, copy: Copy) -> dict[str, Any]:
        row = self.lookup(project.target, copy.path)
        if row is None:
            prior = next(
                (
                    d
                    for d in self.repository.tree_decisions(project.target)
                    if d.destination == copy.path
                ),
                None,
            )
            return {
                "document_id": prior.document_id if prior else None,
                "decision_id": prior.id if prior else None,
                "reviewed": bool(
                    prior
                    and (
                        prior.file_mtime_ns is None
                        or (
                            prior.file_mtime_ns == copy.mtime_ns
                            and prior.size == copy.size
                        )
                    )
                ),
                "review_reason": "index_pending",
                "classification_provenance": "manual" if prior else "discovered",
            }
        changed = row["size"] != copy.size or row["mtime_ns"] != copy.mtime_ns
        return {
            "document_id": row["document_id"],
            "decision_id": row["decision_id"],
            "reviewed": bool(row["reviewed"] and row["present"] and not changed),
            "review_reason": "content_check_pending" if changed else row["reason"],
            "classification_provenance": row["provenance"],
        }

    def status(self, project: Project | None = None) -> dict[str, Any]:
        with self.repository._connect() as connection:
            state = dict(
                connection.execute("SELECT * FROM index_state WHERE id=1").fetchone()
            )
            query = (
                "SELECT COUNT(*) total, COALESCE(SUM(verified),0) hashed "
                "FROM file_index WHERE present=1 "
                "AND relative_path NOT GLOB '_discarded/*'"
            )
            params: tuple[str, ...] = ()
            if project:
                query += " AND base IN (?,?)"
                params = (project.source, project.target)
            counts = dict(connection.execute(query, params).fetchone())
            covered = (
                not project
                or connection.execute(
                    "SELECT COUNT(*) FROM index_roots WHERE base IN (?,?)", params
                ).fetchone()[0]
                == 2
            )
        return {
            **state,
            "indexed_files": counts["total"],
            "hashed_files": counts["hashed"],
            "pending_files": counts["total"] - counts["hashed"],
            "complete": bool(
                state["last_completed_at"]
                and covered
                and not state["running"]
                and not state["error"]
                and counts["total"] == counts["hashed"]
            ),
        }

    def scan(self, *, verify_all: bool = False) -> None:
        """Resume from persisted hashes; never hold the rename lock during I/O.

        Failures are contained: a project whose folders cannot be read, or a
        file that cannot be read, is reported and skipped while everything else
        is indexed. A dump or tree is declared complete, and documents in it
        missing, only after a walk that read every folder; and a document is
        marked missing only if its path is absent on disk at that moment.
        Unchanged walk snapshots publish nothing. New, edited and audited files
        are rechecked under the rename lock before an observation is published.
        """
        if not self._scan_lock.acquire(blocking=False):
            self.request()
            return
        problems: list[str] = []
        self._retry_soon = False
        try:
            with LOCK:
                if self.repository.pending_duplicates():
                    raise ScanPaused("Index paused: duplicate recovery required")
                with self.repository._connect() as connection:
                    connection.execute(
                        "UPDATE index_state SET running=1,error=NULL,"
                        "checked=0,total=0 WHERE id=1"
                    )
                    snapshots = {
                        row["path"]: dict(row)
                        for row in connection.execute(
                            "SELECT path,present,verified,size,mtime_ns,checked_at "
                            "FROM file_index WHERE present=1"
                        )
                    }
            observations: dict[
                str, tuple[Project, DuplicateReview, Copy, Decision | None]
            ] = {}
            walked: set[str] = set()
            trees_walked: set[str] = set()
            incomplete: set[str] = set()
            for project in self.repository.projects():
                try:
                    library = self.root.project(
                        project.source, project.target, project.mode
                    )
                    decisions = self.repository.tree_decisions(project.target)
                    units = {d.destination for d in decisions if d.kind == "folder"}
                    review = DuplicateReview(project, library, self.repository, units)
                    found: list[Problem] = []
                    copies = review.candidates(
                        include_tree=project.target not in trees_walked,
                        problems=found,
                    )
                except (OSError, LibraryError, ValueError) as error:
                    # One project's missing or unreadable folders must not
                    # stop the others; nothing in it is declared missing.
                    problems.append(f"{project.name}: {error}")
                    incomplete.update((project.source, project.target))
                    continue
                walked.update((project.source, project.target))
                trees_walked.add(project.target)
                for problem in found:
                    problems.append(f"{project.name}: {problem.detail}")
                    if problem.incomplete:
                        incomplete.add(
                            project.source if problem.area == "dump" else project.target
                        )
                priors = {d.destination: d for d in decisions}
                for copy in copies:
                    base = project.source if copy.area == "dump" else project.target
                    observations.setdefault(
                        f"{base}/{copy.path}",
                        (
                            project,
                            review,
                            copy,
                            priors.get(copy.path) if copy.area == "tree" else None,
                        ),
                    )
            with self.repository._connect() as connection:
                connection.execute(
                    "UPDATE index_state SET total=? WHERE id=1", (len(observations),)
                )
            # One quiet interval for the batch, rather than sleeping per file.
            # Recheck snapshots below so active saves never publish partial bytes.
            needs_settle = False
            for project, _, copy, _ in observations.values():
                base = project.source if copy.area == "dump" else project.target
                row = snapshots.get(f"{base}/{copy.path}")
                if (
                    not row
                    or row["size"] != copy.size
                    or row["mtime_ns"] != copy.mtime_ns
                ):
                    needs_settle = True
                    break
            if needs_settle and self._stop.wait(self.settle_seconds):
                raise ScanPaused("Index scan interrupted")
            audited = 0
            progress_at = time.monotonic()
            for number, (project, review, copy, prior) in enumerate(
                observations.values(), 1
            ):
                if self._stop.is_set():
                    raise ScanPaused("Index scan interrupted")
                base = project.source if copy.area == "dump" else project.target
                try:
                    audited += self._observe_one(
                        review,
                        base,
                        copy,
                        prior,
                        verify_all,
                        audited,
                        snapshots.get(f"{base}/{copy.path}"),
                    )
                except ScanPaused:
                    raise
                except _Changing:
                    self._retry_soon = True
                    problems.append(f"{base}/{copy.path}: still changing; retried soon")
                except (OSError, LibraryError, ValueError) as error:
                    # Moved or deleted while the scan ran: simply not there now.
                    if not vanished(error):
                        problems.append(f"{base}/{copy.path}: {error}")
                # Progress is display state, not document or decision truth.
                # Bound its durable updates by elapsed time, not file count.
                if time.monotonic() - progress_at >= self.progress_seconds:
                    with self.repository._connect() as connection:
                        connection.execute(
                            "UPDATE index_state SET checked=? WHERE id=1", (number,)
                        )
                    progress_at = time.monotonic()
            complete = walked - incomplete
            with LOCK, self.repository._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if self.repository.pending_duplicates():
                    raise ScanPaused("Index paused: duplicate recovery required")
                for row in connection.execute(
                    "SELECT * FROM file_index WHERE present=1"
                ).fetchall():
                    if (
                        row["base"] not in complete
                        or row["path"] in observations
                        or row["relative_path"].split("/")[0] == "_discarded"
                    ):
                        continue
                    # Sorter moves during the scan create rows the walk never
                    # saw; only a path that is really absent now is missing.
                    if os.path.lexists(self.root.root / row["path"]):
                        continue
                    connection.execute(
                        "UPDATE file_index SET present=0,verified=0 WHERE path=?",
                        (row["path"],),
                    )
                    event(
                        connection,
                        row["base"],
                        row["path"],
                        row["document_id"],
                        "missing",
                        row["sha256"],
                    )
                # "Completed" means every dump and tree was read in full.
                connection.execute(
                    "UPDATE index_state SET running=0,checked=total,error=?,"
                    "last_completed_at="
                    "CASE WHEN ? THEN ? ELSE last_completed_at END WHERE id=1",
                    (summarise(problems), not incomplete, now()),
                )
                connection.executemany(
                    "INSERT OR REPLACE INTO index_roots VALUES(?,?)",
                    [(base, now()) for base in complete],
                )
                connection.executemany(
                    "DELETE FROM index_roots WHERE base=?",
                    [(base,) for base in incomplete],
                )
        except (ScanPaused, OSError, LibraryError, sqlite3.Error, ValueError) as error:
            self._finish_with_error(str(error))
        finally:
            self._scan_lock.release()

    def _observe_one(
        self,
        review: DuplicateReview,
        base: str,
        copy: Copy,
        prior: Decision | None,
        verify_all: bool,
        audited: int,
        row: dict[str, Any] | None,
    ) -> int:
        """Hash one file if needed and publish it; returns 1 if it was audited."""
        cached = bool(
            row
            and row["present"]
            and row["verified"]
            and row["size"] == copy.size
            and row["mtime_ns"] == copy.mtime_ns
        )
        overdue = bool(
            row
            and row["checked_at"]
            and datetime.fromisoformat(row["checked_at"])
            < datetime.now(UTC) - timedelta(days=7)
        )
        audit = cached and (verify_all or (overdue and audited < 16))
        if cached and not audit:
            # The walk just observed these metadata. Nothing is published for
            # an unchanged file, so a concurrent rename cannot overwrite its
            # newer binding. Inventory is a snapshot, never authorization for
            # a move: human actions always recheck the actual file via ensure().
            return 0
        if review.copy(copy.area, copy.path) != copy:
            raise _Changing
        try:
            digest = review.digest(copy, fresh=True)
        except LibraryError as error:
            if "changed" in str(error):
                raise _Changing from error
            raise
        if self._stop.is_set():
            raise ScanPaused("Index scan interrupted")
        with LOCK:
            if self.repository.pending_duplicates():
                raise ScanPaused("Index paused: duplicate recovery required")
            # The sorter may have moved or edited this file meanwhile; publish
            # only an observation that still matches what is on disk.
            if review.copy(copy.area, copy.path) != copy:
                raise _Changing
            self.observe(base, copy, digest, prior)
        return int(audit)

    def known_copies(self, project: Project, include_dump: bool) -> list[Copy] | None:
        """Kept files as last fully indexed, or None if not yet covered.

        Lets a sort check for duplicates without walking the NAS while holding
        the rename lock; callers still re-read every candidate they rely on.
        """
        bases = (project.source, project.target) if include_dump else (project.target,)
        placeholders = ",".join("?" for _ in bases)
        with self.repository._connect() as connection:
            covered = connection.execute(
                f"SELECT COUNT(*) FROM index_roots WHERE base IN ({placeholders})",
                bases,
            ).fetchone()[0]
            if covered != len(set(bases)):
                return None
            rows = connection.execute(
                "SELECT base, relative_path, size, mtime_ns FROM file_index "
                f"WHERE base IN ({placeholders}) AND present=1 AND size IS NOT NULL",
                bases,
            ).fetchall()
        return [
            Copy(
                "dump" if row["base"] == project.source else "tree",
                row["relative_path"],
                row["size"],
                row["mtime_ns"] or "0",
            )
            for row in rows
            if row["relative_path"].split("/")[0] != "_discarded"
        ]

    def groups(self, project: Project, include_dump: bool) -> dict[str, Any]:
        bases = (project.source, project.target) if include_dump else (project.target,)
        placeholders = ",".join("?" for _ in bases)
        with self.repository._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM file_index WHERE base IN ({placeholders}) "
                "AND present=1 AND verified=1 AND sha256 IN ("
                f"SELECT sha256 FROM file_index WHERE base IN ({placeholders}) "
                "AND present=1 AND verified=1 GROUP BY sha256 "
                "HAVING COUNT(*)>1) ORDER BY path",
                (*bases, *bases),
            ).fetchall()
        hashes: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            # Indexed discards are retained for history but never kept candidates;
            # empty files have no content to duplicate.
            if row["relative_path"].split("/")[0] == "_discarded" or not row["size"]:
                continue
            area = "dump" if row["base"] == project.source else "tree"
            hashes[row["sha256"]].append(
                Copy(area, row["relative_path"], row["size"], row["mtime_ns"]).json()
            )
        status = self.status(project)
        return {
            "groups": [
                {"sha256": digest, "copies": copies}
                for digest, copies in hashes.items()
                if len(copies) > 1
            ],
            "files": status["indexed_files"],
            "checked": status["hashed_files"],
            "skipped_large": 0,
            "index": status,
            "complete": status["complete"],
        }


class _Changing(Exception):
    """The file's size or time moved under the scan; try it again soon."""


def summarise(problems: list[str]) -> str | None:
    """A short, honest note of what the last scan could not read."""
    if not problems:
        return None
    shown = "; ".join(problems[:5])
    more = f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
    return f"Indexed with {len(problems)} skipped: {shown}{more}"

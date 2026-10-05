import errno
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from functools import wraps
from html import escape
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import (
    Body,
    Cookie,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
)
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Scope, Send

from services.common.web import cache_versioned_assets
from services.file_sorter import labels, preview
from services.file_sorter.duplicates import AUTOMATIC_BYTES, Copy, DuplicateReview
from services.file_sorter.index import FileIndex
from services.file_sorter.library import (
    DISCARD_FOLDER,
    LOCK,
    Library,
    LibraryError,
    LibraryRoot,
    check_file_name,
    entry_parts,
    file_sha256,
    folder_parts,
    rename_no_replace,
    tree_path,
)
from services.file_sorter.repository import Project, Seed, SorterRepository

logger = logging.getLogger(__name__)

SERVICE_VERSION = "0.9.6"
# How long a validated administrator session is trusted before asking the
# control plane again. Signing out deletes the browser's cookie at once; a
# server-side revocation applies within this time.
SESSION_CACHE_SECONDS = 60.0
SERVICE_DIR = Path(__file__).parent
DEFAULT_DATABASE_PATH = Path("data/file_sorter.db")
TEMPLATE = (SERVICE_DIR / "templates" / "sorter.html").read_text(encoding="utf-8")
# The control plane's signed dashboard session. The sorter never sees the
# signing secret: it forwards the cookie to the control plane for validation.
SESSION_COOKIE = "home_platform_dashboard"
SESSION_VALUE = re.compile(r"^[A-Za-z0-9_\-.=]{1,4096}$")
# Walking a large dump over SMB is slow; the sorter's own moves update the
# cached queue directly, and outside changes appear after this many seconds.
QUEUE_TTL_SECONDS = 45.0
READY_PROBE_SECONDS = 3.0
ENTRY_PATH = Annotated[str, Query(min_length=1, max_length=4096)]
EntryArea = Literal["dump", "tree"]
DuplicateScope = Literal["library", "tree"]
# Files above this size are not hashed just because they are on screen.
DUPLICATE_CHECK_BYTES = AUTOMATIC_BYTES
# Journaled operations that rename one entry and are settled from the disk
# alone; duplicate plans ("resolve", "undo") keep their own recovery.
MOVE_ACTIONS = {"classify", "discard", "reclassify", "folder_move", "undo_decision"}


@dataclass(frozen=True)
class Move:
    """One journaled rename in library-relative terms.

    `size` and `mtime_ns` identify a file after the rename (a rename keeps
    both); a folder has neither.
    """

    from_area: str
    from_path: str
    to_area: str
    to_path: str
    kind: str
    size: int | None
    mtime_ns: int | None

    def arrived(self, path: Path) -> bool:
        if path.is_symlink():
            return False
        if self.kind == "folder":
            return path.is_dir()
        if not path.is_file():
            return False
        status = path.stat()
        return (self.size is None or status.st_size == self.size) and (
            self.mtime_ns is None or status.st_mtime_ns == self.mtime_ns
        )


def journaled_path(library: Library, area: str, relative: str) -> Path:
    """Where a journaled move's entry would be, refusing symlinked parents."""
    path = library.dump if area == "dump" else library.sorted_root
    for part in folder_parts(relative):
        path = path / part
        if path.is_symlink():
            raise LibraryError("Recovery refuses symlinks", 409)
    return path


def move_plan(action: str, project: Project, move: Move, **log: Any) -> dict[str, Any]:
    return {"action": action, "source": project.source, "move": asdict(move), **log}


@dataclass(frozen=True)
class Settings:
    database_path: Path
    library_root: Path
    seed: Seed | None
    base_path: str
    admin_account_id: str | None
    allow_dev_identity: bool
    session_url: str | None


@dataclass(frozen=True)
class Identity:
    account_id: str
    display_name: str
    role: str


class IdentityServiceUnavailableError(Exception):
    pass


class EntryAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)
    size: int | None = None
    # Nanosecond mtimes exceed JavaScript's exact integer range: pass a string.
    mtime_ns: str = Field(pattern=r"^[0-9]{1,20}$")


class ClassifyRequest(EntryAction):
    folder: str = Field(min_length=1, max_length=1024)
    filename: str = Field(min_length=1, max_length=255)


class ReclassifyRequest(EntryAction):
    folder: str = Field(default="", max_length=1024)
    filename: str = Field(min_length=1, max_length=255)
    review: bool = False


class DuplicateCopy(EntryAction):
    area: EntryArea
    size: int = Field(ge=0)


class ResolveDuplicates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    copies: list[DuplicateCopy] = Field(min_length=2, max_length=1000)
    keeper: int = Field(ge=0)
    folder: str = Field(default="", max_length=1024)
    filename: str = Field(min_length=1, max_length=255)
    scope: DuplicateScope = "library"


class UndoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # When set, Undo applies only if this is still the project's latest
    # decision: a notification's UNDO must never reverse something newer.
    expect_decision_id: int | None = None


class SkipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)


class FolderMove(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=1024)
    parent: str = Field(default="", max_length=1024)
    name: str = Field(min_length=1, max_length=255)


class FolderGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paths: list[str] = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=500)


class FolderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parent: str = Field(default="", max_length=1024)
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=500)


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    source: str = Field(min_length=1, max_length=1024)
    target: str = Field(min_length=1, max_length=1024)
    mode: str = Field(pattern=r"^(top|files)$")
    create_target: bool = False


class ProjectRename(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)


def _seed_from_environment() -> Seed | None:
    source = os.environ.get("SORTER_SEED_SOURCE", "").strip().strip("/")
    target = os.environ.get("SORTER_SEED_TARGET", "").strip().strip("/")
    if not source or not target:
        return None
    name = os.environ.get("SORTER_SEED_NAME", "").strip() or source.rsplit("/")[-1]
    return Seed(name, source, target)


def load_settings() -> Settings:
    base_path = os.environ.get("SORTER_BASE_PATH", "/sorter").rstrip("/")
    if base_path and not base_path.startswith("/"):
        raise RuntimeError("SORTER_BASE_PATH must be an absolute URL path")
    return Settings(
        database_path=Path(
            os.environ.get("SORTER_DB_PATH", str(DEFAULT_DATABASE_PATH))
        ),
        library_root=Path(os.environ.get("SORTER_LIBRARY_DIR", "data/sorter")),
        seed=_seed_from_environment(),
        base_path=base_path,
        admin_account_id=os.environ.get("SORTER_ADMIN_ACCOUNT_ID", "").strip() or None,
        allow_dev_identity=os.environ.get("SORTER_ALLOW_DEV_IDENTITY", "false").lower()
        in {"1", "true", "yes"},
        session_url=os.environ.get("SORTER_SESSION_URL", "").strip() or None,
    )


def _session_validator(session_url: str) -> Callable[[str], Identity | None]:
    def validate(session: str) -> Identity | None:
        request = urllib.request.Request(
            session_url, headers={"Cookie": f"{SESSION_COOKIE}={session}"}
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                return None
            raise IdentityServiceUnavailableError from error
        except (OSError, ValueError) as error:
            raise IdentityServiceUnavailableError from error
        return Identity(
            str(payload["id"]), str(payload["username"]), str(payload["role"])
        )

    return validate


class CachedSessionValidator:
    """Remembers valid sessions briefly, keyed by a hash of the cookie.

    Every sorter request, including each preview's file fetch, used to ask
    the control plane. Refused and unavailable answers are never cached.
    """

    MAX_ENTRIES = 32

    def __init__(
        self,
        validate: Callable[[str], Identity | None],
        ttl: float = SESSION_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._validate = validate
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, Identity]] = {}

    def __call__(self, session: str) -> Identity | None:
        key = hashlib.sha256(session.encode()).hexdigest()
        now = self._clock()
        with self._lock:
            cached = self._entries.get(key)
        if cached and now - cached[0] < self._ttl:
            return cached[1]
        identity = self._validate(session)
        with self._lock:
            if identity is None:
                self._entries.pop(key, None)
            else:
                if len(self._entries) >= self.MAX_ENTRIES:
                    self._entries.clear()
                self._entries[key] = (now, identity)
        return identity


class TextGZip:
    """Compress pages, scripts and JSON; never file bytes.

    Previews of PDFs, images and media are already compressed, would cost
    the half-CPU container for nothing, and video seeking needs byte ranges.
    """

    BINARY_SUFFIXES = ("/raw", "/embedded")

    def __init__(self, app: ASGIApp) -> None:
        self._plain = app
        self._gzip = GZipMiddleware(app, minimum_size=1000)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        binary = scope["type"] == "http" and scope["path"].endswith(
            self.BINARY_SUFFIXES
        )
        await (self._plain if binary else self._gzip)(scope, receive, send)


def _modified(mtime_ns: int) -> str:
    return datetime.fromtimestamp(mtime_ns / 1e9, UTC).isoformat(timespec="seconds")


def create_app(
    database_path: Path | None = None,
    *,
    library_root: Path | None = None,
    seed: Seed | None = None,
    allow_dev_identity: bool | None = None,
    admin_account_id: str | None = None,
    session_validator: Callable[[str], Identity | None] | None = None,
    background_index: bool = True,
    session_clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    settings = load_settings()
    settings = replace(
        settings,
        database_path=database_path or settings.database_path,
        library_root=library_root or settings.library_root,
        seed=seed or settings.seed,
        allow_dev_identity=settings.allow_dev_identity
        if allow_dev_identity is None
        else allow_dev_identity,
        admin_account_id=admin_account_id or settings.admin_account_id,
    )
    if session_validator is None and settings.session_url:
        session_validator = _session_validator(settings.session_url)
    if session_validator is not None:
        session_validator = CachedSessionValidator(
            session_validator, clock=session_clock
        )
    repository = SorterRepository(settings.database_path)
    root = LibraryRoot(settings.library_root)
    index = FileIndex(repository, root)
    queue_cache: dict[int, tuple[float, list[str]]] = {}
    # Bumped by every sorter change to a project's queue, so a slow walk that
    # started before a change cannot store a list that undoes it.
    queue_versions: dict[int, int] = {}

    def serialized[**ActionArgs, ActionResult](
        operation: Callable[ActionArgs, ActionResult],
    ) -> Callable[ActionArgs, ActionResult]:
        @wraps(operation)
        def run(*args: ActionArgs.args, **kwargs: ActionArgs.kwargs) -> ActionResult:
            with LOCK:
                if (
                    operation.__name__ not in {"export_labels", "recover_duplicates"}
                    and unsettled()
                ):
                    raise HTTPException(409, "An interrupted operation needs recovery")
                return operation(*args, **kwargs)

        return run

    def settle_moves(project: Project) -> tuple[int, list[str]]:
        """Settle journaled renames from what the disk shows; never rename.

        An entry that arrived is logged exactly as its request would have
        logged it, and an intent whose entry never left is dropped. Anything
        else stays journaled, so writes stay blocked until the owner puts
        the entry at exactly one of the two places. Safe to run repeatedly.
        """
        intents = [
            (operation, plan)
            for operation in repository.incomplete_duplicates(project.id)
            for plan in [json.loads(operation["plan_json"])]
            if plan.get("action") in MOVE_ACTIONS
        ]
        if not intents:
            return 0, []
        try:
            library = root.project(project.source, project.target, project.mode)
        except LibraryError as error:
            return 0, [f"{project.name}: {error}"]
        settled = 0
        problems = []
        for operation, plan in intents:
            move = Move(**plan["move"])
            try:
                source = journaled_path(library, move.from_area, move.from_path)
                target = journaled_path(library, move.to_area, move.to_path)
            except LibraryError as error:
                problems.append(f"{move.to_path}: {error}")
                continue
            at_source = source.exists() or source.is_symlink()
            at_target = target.exists() or target.is_symlink()
            if at_target and not at_source and move.arrived(target):
                log_settled(project, plan, operation["id"])
                queue_cache.pop(project.id, None)
                settled += 1
            elif at_source and not at_target:
                repository.cancel_duplicates(operation["id"])
                settled += 1
            else:
                problems.append(
                    f"The interrupted {plan['action']} of {move.from_path} cannot "
                    f"be settled: put it at exactly one of {move.from_path} and "
                    f"{move.to_path}, unchanged, then recover"
                )
        return settled, problems

    def log_settled(project: Project, plan: dict[str, Any], operation: int) -> None:
        if plan["action"] == "folder_move":
            repository.record_folder_move(
                project.target,
                plan["folder_move"]["old_path"],
                plan["folder_move"]["new_path"],
                operation_id=operation,
            )
        elif plan["action"] == "undo_decision":
            repository.mark_undone(plan["decision_id"], operation_id=operation)
        else:
            repository.record(**plan["record"], operation_id=operation)

    def unsettled() -> bool:
        """True while an interrupted operation still blocks writes.

        The lock is taken only once a journal row exists, so the common
        path stays one cheap query that never waits behind a NAS operation.
        """
        if not repository.pending_duplicates():
            return False
        with LOCK:
            for project in repository.projects(include_archived=True):
                try:
                    _, problems = settle_moves(project)
                except (sqlite3.Error, OSError) as error:
                    problems = [f"{project.name}: {error}"]
                for problem in problems:
                    logger.warning("Unsettled journaled move: %s", problem)
            return repository.pending_duplicates()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        repository.initialize(settings.seed)
        unsettled()
        if background_index:
            index.start()
        try:
            yield
        finally:
            index.stop()

    app = FastAPI(
        title="Home Platform File Sorter",
        version=SERVICE_VERSION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings

    @app.exception_handler(OSError)
    def filesystem_error(request: Request, error: OSError) -> JSONResponse:
        """Disk and NAS failures get a plain answer, never a bare 500."""
        if error.errno in {errno.ENAMETOOLONG, errno.EINVAL, errno.EILSEQ}:
            return JSONResponse(
                {"detail": "That path is too long or not valid"}, status_code=400
            )
        return JSONResponse(
            {"detail": "The NAS could not be read just now; try again shortly"},
            status_code=503,
        )

    app.state.index = index
    app.add_middleware(TextGZip)
    app.mount("/static", StaticFiles(directory=SERVICE_DIR / "static"))

    cache_versioned_assets(app, SERVICE_VERSION)

    @app.middleware("http")
    async def guard_incomplete_operation(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and not request.url.path.endswith("/duplicates/recover")
            and await run_in_threadpool(unsettled)
        ):
            return JSONResponse(
                {
                    "detail": "An interrupted operation needs recovery. "
                    "Export the decision log and follow the recovery runbook."
                },
                status_code=409,
            )
        return await call_next(request)

    def current_identity(
        tailscale_login: Annotated[
            str | None, Header(alias="Tailscale-User-Login")
        ] = None,
        session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        dev_user: Annotated[str | None, Header(alias="X-Sorter-Dev-User")] = None,
    ) -> Identity:
        if not tailscale_login:
            if settings.allow_dev_identity:
                clean = (dev_user or "owner").strip().lower()
                return Identity(f"development:{clean}", clean, "ADMIN")
            raise HTTPException(
                401, "Open this service through the private Tailscale URL"
            )
        # A tailnet identity only proves the request came through the private
        # route. Access is the administrator's dashboard session, checked by
        # the control plane at most SESSION_CACHE_SECONDS before each request.
        if session_validator is None or settings.admin_account_id is None:
            raise HTTPException(503, "Administrator sign-in is not configured")
        if session is None or not SESSION_VALUE.fullmatch(session):
            raise HTTPException(401, "Sign in to the dashboard as administrator")
        try:
            identity = session_validator(session)
        except IdentityServiceUnavailableError as error:
            raise HTTPException(
                503, "Home Platform sign-in service is unavailable"
            ) from error
        if identity is None:
            raise HTTPException(401, "Sign in to the dashboard as administrator")
        if identity.role != "ADMIN" or identity.account_id != settings.admin_account_id:
            raise HTTPException(
                403, "The file sorter is available to the administrator only"
            )
        return identity

    CurrentUser = Annotated[Identity, Depends(current_identity)]

    def refuse(error: LibraryError) -> HTTPException:
        return HTTPException(error.status, str(error))

    # ---------- project context ----------

    def project_for(project_id: int, user: CurrentUser) -> Project:
        project = repository.project(project_id)
        if project is None:
            raise HTTPException(404, "That project does not exist")
        return project

    CurrentProject = Annotated[Project, Depends(project_for)]

    def library_for(project: Project) -> Library:
        try:
            return root.project(project.source, project.target, project.mode)
        except LibraryError as error:
            raise HTTPException(
                409, f"This project's folders are missing: {error}"
            ) from error

    def folder_units(target: str) -> set[str]:
        return {
            decision.destination
            for decision in repository.tree_decisions(target)
            if decision.kind == "folder"
        }

    def listing(project: Project, library: Library, fresh: bool = False) -> list[str]:
        cached = queue_cache.get(project.id)
        if fresh or cached is None or time.monotonic() - cached[0] > QUEUE_TTL_SECONDS:
            for _ in range(2):
                version = queue_versions.get(project.id, 0)
                paths = library.entries()
                if queue_versions.get(project.id, 0) == version:
                    break
            cached = (time.monotonic(), paths)
            queue_cache[project.id] = cached
        return cached[1]

    def forget(project: Project, path: str) -> None:
        queue_versions[project.id] = queue_versions.get(project.id, 0) + 1
        cached = queue_cache.get(project.id)
        if cached and path in cached[1]:
            cached[1].remove(path)

    def restore(project: Project, path: str) -> None:
        queue_versions[project.id] = queue_versions.get(project.id, 0) + 1
        cached = queue_cache.get(project.id)
        if cached and path not in cached[1]:
            cached[1].append(path)
            cached[1].sort(key=str.casefold)

    def queue(project: Project, library: Library) -> tuple[list[str], int]:
        paths = listing(project, library)
        skipped = repository.skips(project.id)
        fresh = [path for path in paths if path not in skipped]
        later = sorted(
            (path for path in paths if path in skipped), key=lambda path: skipped[path]
        )
        return fresh + later, len(later)

    def entry_hash(project: Project, library: Library, path: str) -> str | None:
        entry = library.entry(path)
        if entry.kind != "file" or entry.size is None:
            return None
        key = f"{project.source}/{path}"
        indexed = index.lookup(project.source, path)
        if (
            indexed
            and indexed["verified"]
            and indexed["size"] == entry.size
            and indexed["mtime_ns"] == str(entry.mtime_ns)
        ):
            return str(indexed["sha256"])
        cached = repository.cached_hash(key, entry.size, entry.mtime_ns)
        if cached:
            return cached
        digest = file_sha256(library.entry_path(path))
        if digest:
            repository.remember_hash(key, entry.size, entry.mtime_ns, digest)
        return digest

    def project_summary(project: Project, counts: dict[str, int]) -> dict[str, Any]:
        cached = queue_cache.get(project.id)
        ok = root.exists(project.source) and root.exists(project.target)
        return {
            "id": project.id,
            "name": project.name,
            "source": project.source,
            "target": project.target,
            "mode": project.mode,
            "status": "ok" if ok else "missing",
            "sorted": counts.get("sorted", 0),
            "discarded": counts.get("discarded", 0),
            "remaining": len(cached[1]) if cached else None,
        }

    # ---------- service ----------

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "healthy", "service": "file-sorter"}

    probe: dict[str, Any] = {"thread": None}

    def library_responsive() -> bool:
        """List the library root in time; a stalled NAS is not ready.

        A plain existence check is answered from the CIFS attribute cache even
        while the share is hung. At most one probe is in flight, so a long
        stall cannot pile up blocked threads.
        """
        running = probe["thread"]
        if running is not None and running.is_alive():
            return False
        result: dict[str, bool] = {}

        def listing() -> None:
            try:
                os.listdir(root.root)
                result["ok"] = True
            except OSError:
                result["ok"] = False

        thread = threading.Thread(target=listing, name="sorter-ready", daemon=True)
        probe["thread"] = thread
        thread.start()
        thread.join(READY_PROBE_SECONDS)
        return result.get("ok", False)

    @app.get("/ready")
    def ready() -> JSONResponse:
        ok = (
            repository.ready()
            and root.ready()
            and library_responsive()
            and not unsettled()
        )
        return JSONResponse(
            {"status": "ready" if ok else "not_ready"}, status_code=200 if ok else 503
        )

    @app.get("/version")
    def version() -> dict[str, str]:
        return {"service": "file-sorter", "version": SERVICE_VERSION}

    @app.get("/", response_class=HTMLResponse)
    def page(
        tailscale_login: Annotated[
            str | None, Header(alias="Tailscale-User-Login")
        ] = None,
        session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
        dev_user: Annotated[str | None, Header(alias="X-Sorter-Dev-User")] = None,
    ) -> HTMLResponse:
        try:
            user = current_identity(tailscale_login, session, dev_user)
        except HTTPException as error:
            # A browser landing here signed out gets a way forward, not JSON.
            return HTMLResponse(
                "<!doctype html><meta charset=utf-8><title>Sorter</title>"
                f"<p>{escape(str(error.detail))}.</p>"
                '<p><a href="/dashboard">Open the dashboard</a></p>',
                status_code=error.status_code,
            )
        config = json.dumps(
            {"basePath": settings.base_path, "displayName": user.display_name}
        ).replace("<", "\\u003c")
        return HTMLResponse(
            TEMPLATE.replace("__BASE_PATH__", escape(settings.base_path, quote=True))
            .replace("__SORTER_CONFIG__", config)
            .replace("__VERSION__", SERVICE_VERSION)
        )

    # ---------- projects ----------

    @app.get("/api/projects")
    def list_projects(user: CurrentUser) -> dict[str, Any]:
        counts = repository.counts()
        return {
            "projects": [
                project_summary(project, counts.get(project.id, {}))
                for project in repository.projects()
            ]
        }

    @app.post("/api/projects", status_code=201)
    @serialized  # the overlap check and the insert must not interleave
    def create_project(payload: ProjectCreate, user: CurrentUser) -> dict[str, Any]:
        source = payload.source.strip("/")
        target = payload.target.strip("/")
        others = [(p.source, p.target) for p in repository.projects()]
        try:
            root.check_project(
                source, target, payload.mode, others, payload.create_target
            )
            if payload.create_target:
                root.create_target(target)
        except LibraryError as error:
            raise refuse(error) from error
        try:
            project = repository.create_project(
                payload.name.strip(), source, target, payload.mode
            )
        except sqlite3.IntegrityError as error:
            raise HTTPException(409, "A project with that name exists") from error
        index.request()
        return project_summary(project, {})

    @app.patch("/api/projects/{project_id}")
    def rename_project(
        payload: ProjectRename, project: CurrentProject
    ) -> dict[str, Any]:
        try:
            repository.rename_project(project.id, payload.name.strip())
        except sqlite3.IntegrityError as error:
            raise HTTPException(409, "A project with that name exists") from error
        renamed = repository.project(project.id)
        assert renamed is not None
        return project_summary(renamed, repository.counts().get(project.id, {}))

    @app.delete("/api/projects/{project_id}", status_code=204)
    @serialized  # the overlap check and the insert must not interleave
    def archive_project(project: CurrentProject) -> None:
        # Files and labels stay where they are; only the shortcut goes away.
        repository.archive_project(project.id)
        queue_cache.pop(project.id, None)

    @app.get("/api/browse")
    def browse(
        user: CurrentUser,
        path: Annotated[str, Query(max_length=1024)] = "",
    ) -> dict[str, Any]:
        path = path.strip("/")
        try:
            folders = root.browse(path)
        except LibraryError as error:
            raise refuse(error) from error
        roles: dict[str, str] = {}
        for project in repository.projects():
            roles[project.source] = f"dump of {project.name}"
            roles.setdefault(project.target, f"tree of {project.name}")
        for folder in folders:
            folder["role"] = roles.get(str(folder["path"]))
        return {"path": path, "folders": folders}

    # ---------- entries ----------

    def entry_location(project: Project, path: str, area: EntryArea) -> Path:
        library = library_for(project)
        if area == "tree":
            return library.sorted_entry_path(path, folder_units(project.target))
        return library.entry_path(path)

    @app.get("/api/projects/{project_id}/sorted")
    def sorted_entries(
        project: CurrentProject,
        folder: Annotated[str, Query(max_length=1024)] = "",
        offset: Annotated[int, Query(ge=0)] = 0,
        search: Annotated[str, Query(max_length=255)] = "",
    ) -> dict[str, Any]:
        try:
            entries, more = library_for(project).browse_sorted(
                folder, folder_units(project.target), offset, search
            )
        except LibraryError as error:
            raise refuse(error) from error
        return {
            "folder": folder,
            "entries": entries,
            "more": more,
            "next_offset": offset + len(entries),
        }

    @app.get("/api/projects/{project_id}/sorted-entry")
    def sorted_entry(project: CurrentProject, path: ENTRY_PATH) -> dict[str, Any]:
        library = library_for(project)
        try:
            units = folder_units(project.target)
            entry = library.sorted_entry(path, units)
            location = library.sorted_entry_path(path, units)
        except LibraryError as error:
            raise refuse(error) from error
        folder, _, name = path.rpartition("/")
        paths, skipped = queue(project, library)
        counts = repository.counts().get(project.id, {})
        return {
            "progress": {
                "remaining": len(paths),
                "skipped": skipped,
                "sorted": counts.get("sorted", 0),
                "discarded": counts.get("discarded", 0),
            },
            "entry": {
                "path": path,
                "name": name,
                "folder": folder,
                "kind": entry.kind,
                "size": entry.size,
                "mtime_ns": str(entry.mtime_ns),
                "modified": _modified(entry.mtime_ns),
                "skipped": False,
                "area": "tree",
                "preview": preview.describe(location),
            },
        }

    @app.get("/api/projects/{project_id}/review")
    def review_files(
        project: CurrentProject,
        search: Annotated[str, Query(max_length=255)] = "",
        folder: Annotated[str, Query(max_length=1024)] = "",
        unreviewed: bool = False,
        recursive: bool = True,
        offset: Annotated[int, Query(ge=0)] = 0,
        anchor: Annotated[str, Query(max_length=4096)] = "",
    ) -> dict[str, Any]:
        library = library_for(project)
        units = folder_units(project.target)
        try:
            if folder:
                library.category_path(folder, units)
            # The index holds the same inventory as a walk (checked live on
            # 2 October: identical file sets) without a NAS round-trip per
            # path segment. Only an uncovered tree is walked.
            indexed = index.tree_files(project)
            if indexed is None:
                copies = DuplicateReview(
                    project, library, repository, units
                ).candidates(include_dump=False)
                states = None
            else:
                rows = [
                    row
                    for row in indexed
                    if row["relative_path"].split("/")[0] != "_discarded"
                    and not any(
                        row["relative_path"] == unit
                        or row["relative_path"].startswith(unit + "/")
                        for unit in units
                    )
                ]
                copies = [
                    Copy("tree", row["relative_path"], row["size"], row["mtime_ns"])
                    for row in rows
                ]
                states = {
                    row["relative_path"]: index.indexed_review_state(row)
                    for row in rows
                }
        except LibraryError as error:
            raise refuse(error) from error
        entries = []
        needle = search.casefold()
        for copy in sorted(copies, key=lambda c: c.path.casefold()):
            if folder and not (
                copy.path.startswith(folder + "/")
                if recursive
                else copy.path.rpartition("/")[0] == folder
            ):
                continue
            if needle not in copy.path.casefold():
                continue
            review_state = (
                states[copy.path]
                if states is not None
                else index.review_state(project, copy)
            )
            if unreviewed and review_state["reviewed"]:
                continue
            entries.append(
                {
                    **copy.json(),
                    "folder": copy.path.rpartition("/")[0],
                    **review_state,
                }
            )
        if anchor:
            anchor_index = next(
                (i for i, entry in enumerate(entries) if entry["path"] == anchor), None
            )
            if anchor_index is not None:
                offset = anchor_index // 500 * 500
        return {
            "entries": entries[offset : offset + 500],
            "total": len(entries),
            "offset": offset,
            "more": len(entries) > offset + 500,
            "next_offset": offset + min(500, len(entries[offset:])),
            "index": index.status(project),
        }

    @app.get("/api/projects/{project_id}/index")
    def index_status(project: CurrentProject) -> dict[str, Any]:
        return index.status(project)

    @app.post("/api/projects/{project_id}/index/refresh", status_code=202)
    def refresh_index(
        project: CurrentProject, verify_all: bool = False
    ) -> dict[str, Any]:
        index.request(verify_all=verify_all)
        return index.status(project)

    @app.post("/api/projects/{project_id}/review/accept")
    @serialized
    def accept_review(payload: EntryAction, project: CurrentProject) -> dict[str, Any]:
        library = library_for(project)
        units = folder_units(project.target)
        try:
            entry = library.sorted_entry(payload.path, units)
            library.check_unchanged(entry, payload.size, int(payload.mtime_ns))
            review = DuplicateReview(project, library, repository, units)
            review.require_single(
                "tree",
                payload.path,
                include_dump=False,
                known=index.known_copies(project, include_dump=False),
            )
            if payload.path.split("/")[0] == DISCARD_FOLDER:
                raise LibraryError("Discarded files are outside sorted review")
            observed = index.ensure(project, "tree", payload.path)
            digest = observed["sha256"]
            library.check_unchanged(
                library.sorted_entry(payload.path, units),
                payload.size,
                int(payload.mtime_ns),
            )
        except LibraryError as error:
            raise refuse(error) from error
        prior = next(
            (
                d
                for d in repository.tree_decisions(project.target)
                if d.destination == payload.path
                and d.document_id == observed["document_id"]
            ),
            None,
        )
        folder, _, name = payload.path.rpartition("/")
        decision = repository.record(
            project_id=project.id,
            target=project.target,
            action="sort",
            kind="file",
            original_name=prior.original_name if prior else name,
            final_name=name,
            label=folder,
            destination=payload.path,
            sha256=digest,
            size=entry.size,
            replaces_id=prior.id if prior else None,
            previous_destination=payload.path,
            document_id=observed["document_id"],
            origin_project_id=(
                prior.origin_project_id
                if prior.previous_destination is not None
                else prior.project_id
            )
            if prior
            else None,
            review_type="accepted",
            file_mtime_ns=str(entry.mtime_ns),
        )
        return {
            "decision_id": decision.id,
            "document_id": decision.document_id,
            "path": payload.path,
        }

    @app.get("/api/projects/{project_id}/current")
    def current(
        project: CurrentProject,
        path: Annotated[str | None, Query(max_length=4096)] = None,
    ) -> dict[str, Any]:
        library = library_for(project)
        paths, skipped = queue(project, library)
        counts = repository.counts().get(project.id, {})
        progress = {
            "remaining": len(paths),
            "skipped": skipped,
            "sorted": counts.get("sorted", 0),
            "discarded": counts.get("discarded", 0),
        }
        if path is not None and path not in paths:
            raise HTTPException(404, "That entry is no longer in the dump")
        while True:
            chosen = path or (paths[0] if paths else None)
            if chosen is None:
                progress["remaining"] = 0
                return {"entry": None, "progress": progress, "upcoming": []}
            try:
                entry = library.entry(chosen)
                location = library.entry_path(chosen)
                break
            except LibraryError as error:
                forget(project, chosen)
                # A queue head moved outside the sorter is skipped, not shown
                # as an error: the next entry takes its place.
                if path is not None or error.status != 404:
                    raise refuse(error) from error
                paths = [item for item in paths if item != chosen]
                progress["remaining"] = len(paths)
        upcoming = [item for item in paths if item != chosen][:6]
        folder, _, name = chosen.rpartition("/")
        return {
            "entry": {
                "path": entry.name,
                "name": name,
                "folder": folder,
                "kind": entry.kind,
                "size": entry.size,
                "mtime_ns": str(entry.mtime_ns),
                "modified": _modified(entry.mtime_ns),
                "skipped": chosen in repository.skips(project.id),
                "preview": preview.describe(location),
            },
            "progress": progress,
            "upcoming": upcoming,
        }

    @app.get("/api/projects/{project_id}/preview")
    def full_preview(
        project: CurrentProject, path: ENTRY_PATH, area: EntryArea = "dump"
    ) -> dict[str, Any]:
        """The same preview with larger budgets, for an explicit "load more"."""
        try:
            location = entry_location(project, path, area)
        except LibraryError as error:
            raise refuse(error) from error
        return preview.describe(location, full=True)

    @app.post("/api/projects/{project_id}/rescan")
    def rescan(project: CurrentProject) -> dict[str, int]:
        return {"remaining": len(listing(project, library_for(project), fresh=True))}

    @app.get("/api/projects/{project_id}/raw")
    def raw(
        project: CurrentProject, path: ENTRY_PATH, area: EntryArea = "dump"
    ) -> FileResponse:
        try:
            location = entry_location(project, path, area)
        except LibraryError as error:
            raise refuse(error) from error
        inline = preview.inline_type(location)
        if location.is_dir() or inline is None:
            raise HTTPException(415, "This entry is previewed as text, not raw bytes")
        return FileResponse(
            location,
            media_type=inline[1],
            filename=location.name,
            content_disposition_type="inline",
            headers=preview.raw_headers(location),
        )

    @app.get("/api/projects/{project_id}/embedded")
    def embedded(
        project: CurrentProject,
        path: ENTRY_PATH,
        member: Annotated[str, Query(max_length=64)],
        area: EntryArea = "dump",
    ) -> Response:
        """A preview image the document itself carries (iWork, Office, ODF)."""
        try:
            location = entry_location(project, path, area)
            data, media_type = preview.embedded_bytes(location, member)
        except LibraryError as error:
            raise refuse(error) from error
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            raise HTTPException(404, "This document has no such preview") from error
        return Response(
            data,
            media_type=media_type,
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
        )

    @app.get("/api/projects/{project_id}/duplicate")
    def duplicate(
        project: CurrentProject,
        path: ENTRY_PATH,
        force: bool = False,
        area: EntryArea = "dump",
        scope: DuplicateScope = "library",
        indexed: bool = False,
    ) -> dict[str, Any]:
        """Hashing reads the whole file over the network, so large files are
        only checked when asked; classify still hashes up to its own limit."""
        library = library_for(project)
        try:
            entry = (
                library.entry(path)
                if area == "dump"
                else library.sorted_entry(path, folder_units(project.target))
            )
            if entry.kind != "file":
                return {"sha256": None, "sorted_at": None, "copies": []}
            if indexed:
                base = project.source if area == "dump" else project.target
                row = index.lookup(base, path)
                groups = index.groups(project, include_dump=scope != "tree")
                valid = bool(
                    row
                    and row["present"]
                    and row["verified"]
                    and row["size"] == entry.size
                    and row["mtime_ns"] == str(entry.mtime_ns)
                )
                group = next(
                    (
                        g
                        for g in groups["groups"]
                        if valid and row and g["sha256"] == row["sha256"]
                    ),
                    None,
                )
                if not valid:
                    index.request()
                copies = group["copies"] if group else []
                return {
                    "sha256": row["sha256"] if valid and row else None,
                    "copies": copies,
                    "scope": scope,
                    "sorted_at": next(
                        (
                            f"{project.target}/{c['path']}"
                            for c in copies
                            if c["area"] == "tree"
                            and (c["area"], c["path"]) != (area, path)
                        ),
                        None,
                    ),
                    "index": groups["index"],
                    "complete": valid and groups["complete"],
                }
            if not force and (entry.size or 0) > DUPLICATE_CHECK_BYTES:
                return {"sha256": None, "sorted_at": None, "skipped": "large"}
            review = DuplicateReview(
                project, library, repository, folder_units(project.target)
            )
            if scope == "tree" and area != "tree":
                raise LibraryError("Sorted review only checks tree files")
            digest, copies = review.matches(
                area, path, force=force, include_dump=scope != "tree"
            )
        except LibraryError as error:
            raise refuse(error) from error
        return {
            "sha256": digest,
            "sorted_at": next(
                (
                    f"{project.target}/{c.path}"
                    for c in copies
                    if c.area == "tree" and (c.area, c.path) != (area, path)
                ),
                None,
            ),
            "copies": [c.json() for c in copies] if len(copies) > 1 else [],
            "scope": scope,
        }

    @app.get("/api/projects/{project_id}/duplicates")
    def duplicate_groups(
        project: CurrentProject,
        force: bool = False,
        scope: DuplicateScope = "library",
        indexed: bool = False,
    ) -> dict[str, Any]:
        try:
            result = (
                index.groups(project, include_dump=scope != "tree")
                if indexed
                else DuplicateReview(
                    project,
                    library_for(project),
                    repository,
                    folder_units(project.target),
                ).scan(force=force, include_dump=scope != "tree")
            )
            result["scope"] = scope
            for group in result["groups"]:
                group["scope"] = scope
            result["pending_recovery"] = bool(
                repository.incomplete_duplicates(project.id)
            )
            return result
        except LibraryError as error:
            raise refuse(error) from error

    @app.post("/api/projects/{project_id}/duplicates/recover")
    @serialized
    def recover_duplicates(project: CurrentProject) -> dict[str, int]:
        library = library_for(project)
        units = folder_units(project.target)
        settled, problems = settle_moves(project)
        if problems:
            raise HTTPException(409, "; ".join(problems))
        pending = repository.incomplete_duplicates(project.id)
        try:
            for operation in pending:
                plan = json.loads(operation["plan_json"])
                moves = []

                def location(area: str, path: str) -> Path:
                    parts = entry_parts(path)
                    if area == "tree":
                        parent = library.category_path(
                            "/".join(parts[:-1]), units, discarded=True
                        )
                    elif area == "dump":
                        parent = library.dump
                        for part in parts[:-1]:
                            parent = parent / part
                            if parent.is_symlink() or not parent.is_dir():
                                raise LibraryError(
                                    "An original dump folder is unavailable; "
                                    "recovery refused",
                                    409,
                                )
                    else:
                        raise LibraryError("Invalid recovery area", 409)
                    path_object = parent / parts[-1]
                    if path_object.is_symlink():
                        raise LibraryError("Recovery refuses symlinks", 409)
                    return path_object

                for copy in plan["copies"]:
                    source = location(copy["area"], copy["path"])
                    target = location(
                        copy.get("destination_area", "tree"), copy["destination"]
                    )
                    moves.append((source, target))
                originals = {source for source, _ in moves}
                # Check every possible location before touching anything. An
                # external replacement or an ambiguous extra copy stays put.
                for source, target in moves:
                    present = [p for p in {source, target} if p.exists()]
                    if not present or any(
                        not p.is_file()
                        or file_sha256(p, 2**63 - 1) != operation["sha256"]
                        for p in present
                    ):
                        raise LibraryError(
                            "A journalled copy is missing or changed; recovery refused",
                            409,
                        )
                present_paths = {p for pair in moves for p in pair if p.exists()}
                rollback = []
                for source, target in reversed(moves):
                    if source == target or source in present_paths:
                        continue
                    if target not in present_paths:
                        raise LibraryError(
                            "Recovery cannot account for every copy", 409
                        )
                    present_paths.remove(target)
                    present_paths.add(source)
                    rollback.append((target, source))
                if present_paths != originals:
                    raise LibraryError(
                        "Recovery found an ambiguous extra copy; review the log",
                        409,
                    )
                for source, target in rollback:
                    rename_no_replace(source, target)
                repository.cancel_duplicates(operation["id"])
                queue_cache.pop(project.id, None)
        except LibraryError as error:
            raise refuse(error) from error
        return {"recovered": settled + len(pending)}

    def journaled_move[Logged](
        project: Project,
        digest: str,
        plan: dict[str, Any],
        move: Callable[[], object],
        reverse: Callable[[], object],
        commit: Callable[[int], Logged],
    ) -> Logged:
        """Rename and log as one operation, with the intent journaled first.

        A refused rename cancels the journal. A failed log write (any
        exception) reverses the rename and cancels; if even the reversal
        fails the journal stays, the reply names both errors and where the
        entry is, and `settle_moves` logs it once the disk confirms it
        arrived. A crash leaves the journal for the next start to settle.
        `move` must leave the disk unchanged when it raises.
        """
        operation = repository.prepare_duplicates(
            project.id, project.target, digest, json.dumps(plan)
        )
        try:
            move()
        except Exception:
            repository.cancel_duplicates(operation)
            raise
        try:
            return commit(operation)
        except Exception as error:
            moved = plan.get("move")
            if plan["action"] == "folder_move" and moved:
                what = f"The move of {moved['from_path']}"
            elif plan["action"] == "undo_decision":
                what = "The undo"
            else:
                what = "The decision"
            try:
                reverse()
            except Exception as undo_error:
                where = f"; it is now at {moved['to_path']}" if moved else ""
                raise HTTPException(
                    500,
                    f"{what} could not be logged ({error}) and moving it back "
                    f"failed ({undo_error}){where}. It will be logged once the "
                    "disk is checked again",
                ) from error
            try:
                repository.cancel_duplicates(operation)
            except sqlite3.Error as cancel_error:
                # The entry is back where it was; the stale intent is dropped
                # by the next settle, so the owner hears the original failure.
                logger.warning(
                    "Could not cancel journal %s: %s", operation, cancel_error
                )
            raise HTTPException(
                500, f"{what} could not be logged ({error}), so it was moved back"
            ) from error

    def apply_duplicate_moves(
        project: Project,
        digest: str,
        plan: dict[str, Any],
        moves: list[tuple[Path, Path]],
        commit: Callable[[int], Any],
    ) -> Any:
        moved: list[tuple[Path, Path]] = []

        def rename_all() -> None:
            try:
                for source, target in moves:
                    if source == target:
                        continue
                    rename_no_replace(source, target)
                    moved.append((source, target))
            except Exception:
                reverse_all()
                raise

        def reverse_all() -> None:
            while moved:
                source, target = moved.pop()
                rename_no_replace(target, source)

        return journaled_move(project, digest, plan, rename_all, reverse_all, commit)

    @app.post("/api/projects/{project_id}/duplicates/resolve")
    @serialized
    def resolve_duplicates(
        payload: ResolveDuplicates, project: CurrentProject
    ) -> dict[str, Any]:
        library = library_for(project)
        units = folder_units(project.target)
        review = DuplicateReview(project, library, repository, units)
        try:
            if payload.keeper >= len(payload.copies):
                raise LibraryError("Choose one copy to keep")
            submitted = [
                Copy(c.area, c.path, c.size, c.mtime_ns) for c in payload.copies
            ]
            if len(set(submitted)) != len(submitted):
                raise LibraryError("The same copy was listed twice")
            chosen = submitted[payload.keeper]
            if payload.scope == "tree" and any(c.area != "tree" for c in submitted):
                raise LibraryError("Sorted duplicate review only accepts tree copies")
            digest, actual = review.matches(
                chosen.area,
                chosen.path,
                force=True,
                fresh=True,
                include_dump=payload.scope != "tree",
            )
            if digest != payload.sha256 or set(actual) != set(submitted):
                raise LibraryError(
                    "The duplicate group changed. Scan again before choosing.", 409
                )
            target_folder = library.category_path(payload.folder, units)
            filename = check_file_name(payload.filename)
            destination = f"{payload.folder}/{filename}" if payload.folder else filename
            target = target_folder / filename
            sources = {review.location(c.area, c.path) for c in actual}
            if (target.exists() or target.is_symlink()) and target not in sources:
                raise LibraryError(
                    "That filename belongs to another entry; choose another name", 409
                )
            bin_path = library.sorted_root / DISCARD_FOLDER
            if bin_path.is_symlink():
                raise LibraryError("The discard folder must not be a symlink")
            bin_path.mkdir(exist_ok=True)
            occupied = {p.name for p in bin_path.iterdir()}
            decisions = {
                d.destination: d for d in repository.tree_decisions(project.target)
            }
            records: list[dict[str, Any]] = []
            moves = []
            plan_copies = []
            # Move redundant copies first, freeing any filename the keeper adopts.
            ordered = [c for c in actual if c != chosen] + [chosen]
            for copy in ordered:
                source = review.location(copy.area, copy.path)
                kept = copy == chosen
                if kept:
                    final = destination
                else:
                    leaf = source.name
                    stem, suffix = os.path.splitext(leaf)
                    name, number = leaf, 1
                    while name in occupied:
                        number += 1
                        name = f"{stem} ({number}){suffix}"
                    occupied.add(name)
                    final = f"{DISCARD_FOLDER}/{name}"
                moves.append((source, library.sorted_root / final))
                prior = decisions.get(copy.path) if copy.area == "tree" else None
                observed = index.observe(
                    project.source if copy.area == "dump" else project.target,
                    copy,
                    digest,
                    prior,
                )
                if prior and prior.document_id != observed["document_id"]:
                    prior = None
                records.append(
                    {
                        "project_id": project.id,
                        "target": project.target,
                        "action": "sort" if kept else "discard",
                        "kind": "file",
                        "original_name": prior.original_name if prior else copy.path,
                        "final_name": final.rsplit("/", 1)[-1],
                        "label": payload.folder if kept else DISCARD_FOLDER,
                        "destination": final,
                        "sha256": digest,
                        "size": copy.size,
                        "replaces_id": prior.id if prior else None,
                        "previous_destination": copy.path
                        if copy.area == "tree"
                        else None,
                        "document_id": observed["document_id"],
                        "review_type": "manual",
                        "file_mtime_ns": copy.mtime_ns,
                        "origin_project_id": (
                            prior.origin_project_id
                            if prior.previous_destination is not None
                            else prior.project_id
                        )
                        if prior
                        else (project.id if copy.area == "dump" else None),
                    }
                )
                plan_copies.append({**copy.json(), "destination": final, "kept": kept})
            if any(review.copy(c.area, c.path) != c for c in actual):
                raise LibraryError("A copy changed during review. Scan again.", 409)
            keeper = apply_duplicate_moves(
                project,
                payload.sha256,
                {"action": "resolve", "source": project.source, "copies": plan_copies},
                moves,
                lambda operation: repository.record_duplicates(
                    operation, records[-1], records[:-1]
                ),
            )
            for copy in actual:
                if copy.area == "dump":
                    forget(project, copy.path)
        except LibraryError as error:
            raise refuse(error) from error
        return {
            "destination": destination,
            "discarded": len(actual) - 1,
            "decision_id": keeper.id,
            "document_id": keeper.document_id,
        }

    # ---------- tree folders ----------

    @app.get("/api/projects/{project_id}/folders")
    def folders(project: CurrentProject) -> dict[str, Any]:
        library = library_for(project)
        notes = repository.folder_notes(project.target)
        return {
            "folders": [
                {
                    "path": path,
                    "description": notes.get(path, ""),
                    "items": items,
                    "subfolders": subfolders,
                }
                for path, items, subfolders in library.folder_stats(
                    folder_units(project.target)
                )
            ]
        }

    @app.post("/api/projects/{project_id}/folders", status_code=201)
    @serialized
    def create_folder(payload: FolderCreate, project: CurrentProject) -> dict[str, str]:
        try:
            path = library_for(project).create_folder(payload.parent, payload.name)
        except LibraryError as error:
            raise refuse(error) from error
        repository.add_folder(project.target, path, payload.description.strip())
        return {"path": path}

    def logged_folder_move(
        project: Project,
        library: Library,
        path: str,
        parent: str,
        name: str,
        units: set[str],
    ) -> str:
        """Rename a folder and log it as one journaled move."""
        new_path = tree_path(parent, name)
        if new_path == path:
            return library.move_folder(path, parent, name, units)
        old_parent, _, old_name = path.rpartition("/")
        journaled_move(
            project,
            "",
            move_plan(
                "folder_move",
                project,
                Move("tree", path, "tree", new_path, "folder", None, None),
                folder_move={"old_path": path, "new_path": new_path},
            ),
            move=lambda: library.move_folder(path, parent, name, units),
            reverse=lambda: library.move_folder(new_path, old_parent, old_name, units),
            commit=lambda operation: repository.record_folder_move(
                project.target, path, new_path, operation_id=operation
            ),
        )
        return new_path

    @app.post("/api/projects/{project_id}/folders/move")
    @serialized
    def move_folder(payload: FolderMove, project: CurrentProject) -> dict[str, str]:
        try:
            path = logged_folder_move(
                project,
                library_for(project),
                payload.path,
                payload.parent,
                payload.name,
                folder_units(project.target),
            )
        except LibraryError as error:
            raise refuse(error) from error
        return {"path": path}

    @app.post("/api/projects/{project_id}/folders/group", status_code=201)
    @serialized
    def group_folders(payload: FolderGroup, project: CurrentProject) -> dict[str, Any]:
        """Insert a new folder level above sibling folders, e.g. school/X ->
        school/data_science/X, and carry their labels along."""
        library = library_for(project)
        paths = list(dict.fromkeys(payload.paths))
        parents = {path.rpartition("/")[0] for path in paths}
        if len(parents) != 1:
            raise HTTPException(400, "Group folders that share the same parent")
        parent = parents.pop()
        units = folder_units(project.target)
        known = set(library.folders(units))
        if any(path not in known for path in paths):
            raise HTTPException(404, "One of those folders no longer exists")
        if payload.name in {path.rpartition("/")[2] for path in paths}:
            raise HTTPException(400, "The new folder needs a different name")
        try:
            group = library.create_folder(parent, payload.name)
        except LibraryError as error:
            raise refuse(error) from error
        repository.add_folder(project.target, group, payload.description.strip())
        moved: list[str] = []
        for path in paths:
            name = path.rpartition("/")[2]
            try:
                new_path = logged_folder_move(
                    project, library, path, group, name, units
                )
            except (LibraryError, HTTPException) as error:
                # Earlier moves stay done and logged; report exactly where it stopped.
                detail = error.detail if isinstance(error, HTTPException) else error
                status = (
                    error.status_code
                    if isinstance(error, HTTPException)
                    else error.status
                )
                raise HTTPException(
                    status,
                    f"{detail}. Created {group}; moved {len(moved)} of {len(paths)}",
                ) from error
            moved.append(new_path)
        return {"path": group, "moved": moved}

    # ---------- decisions ----------

    @app.post("/api/projects/{project_id}/reclassify")
    @serialized
    def reclassify(
        payload: ReclassifyRequest, project: CurrentProject
    ) -> dict[str, Any]:
        library = library_for(project)
        # Keep the filesystem move and its label together across browser tabs.
        with library.lock:
            units = folder_units(project.target)
            try:
                source = library.sorted_entry_path(payload.path, units)
                entry = library.sorted_entry(payload.path, units)
                library.check_unchanged(entry, payload.size, int(payload.mtime_ns))
                DuplicateReview(project, library, repository, units).require_single(
                    "tree",
                    payload.path,
                    include_dump=not payload.review,
                    known=index.known_copies(project, include_dump=not payload.review),
                )
                target = library.category_path(payload.folder, units) / check_file_name(
                    payload.filename
                )
                if target == source:
                    raise LibraryError("Choose a different folder or filename")
                if target.exists() or target.is_symlink():
                    raise LibraryError("That filename already exists", 409)
                observed = index.ensure(project, "tree", payload.path)
                # Several decisions can name one path over time (a file deleted
                # and another sorted there); correct the one about this document.
                prior = next(
                    (
                        d
                        for d in reversed(repository.tree_decisions(project.target))
                        if d.destination == payload.path
                        and d.document_id == observed["document_id"]
                    ),
                    None,
                )
                digest = observed["sha256"]
                destination = tree_path(payload.folder, payload.filename)
                record: dict[str, Any] = {
                    "project_id": project.id,
                    "target": project.target,
                    "action": "sort",
                    "kind": "file",
                    "original_name": prior.original_name if prior else source.name,
                    "final_name": payload.filename,
                    "label": payload.folder,
                    "destination": destination,
                    "sha256": digest,
                    "size": entry.size,
                    "review_type": "corrected" if payload.review else "manual",
                    "file_mtime_ns": str(entry.mtime_ns),
                    "replaces_id": prior.id if prior else None,
                    "previous_destination": payload.path,
                    "document_id": observed["document_id"],
                    "origin_project_id": (
                        prior.origin_project_id
                        if prior.previous_destination is not None
                        else prior.project_id
                    )
                    if prior
                    else None,
                }
                decision = journaled_move(
                    project,
                    digest or "",
                    move_plan(
                        "reclassify",
                        project,
                        Move(
                            "tree",
                            payload.path,
                            "tree",
                            destination,
                            "file",
                            entry.size,
                            entry.mtime_ns,
                        ),
                        record=record,
                    ),
                    move=lambda: library.reclassify(
                        payload.path,
                        payload.folder,
                        payload.filename,
                        payload.size,
                        int(payload.mtime_ns),
                        units,
                    ),
                    reverse=lambda: library.restore_sorted(
                        destination, payload.path, units
                    ),
                    commit=lambda operation: repository.record(
                        **record, operation_id=operation
                    ),
                )
            except LibraryError as error:
                raise refuse(error) from error
        return {
            "destination": destination,
            "decision_id": decision.id,
            "document_id": decision.document_id,
        }

    @app.post("/api/projects/{project_id}/classify")
    @serialized
    def classify(payload: ClassifyRequest, project: CurrentProject) -> dict[str, Any]:
        library = library_for(project)
        units = folder_units(project.target)
        try:
            check_file_name(payload.filename)
            if payload.folder.split("/")[0] == DISCARD_FOLDER:
                raise LibraryError("Use Discard for the discard folder")
            if payload.folder in units or any(
                payload.folder.startswith(f"{unit}/") for unit in units
            ):
                raise LibraryError("That folder is a sorted item, not a category")
            entry = library.entry(payload.path)
            library.check_unchanged(entry, payload.size, int(payload.mtime_ns))
            if entry.kind == "file":
                DuplicateReview(project, library, repository, units).require_single(
                    "dump",
                    payload.path,
                    known=index.known_copies(project, include_dump=True),
                )
            digest = entry_hash(project, library, payload.path)
            observed = (
                index.observe(
                    project.source,
                    Copy("dump", payload.path, entry.size or 0, str(entry.mtime_ns)),
                    digest,
                )
                if entry.kind == "file"
                else None
            )
            library.check_unchanged(
                library.entry(payload.path), payload.size, int(payload.mtime_ns)
            )
            destination = tree_path(payload.folder, payload.filename)
            record: dict[str, Any] = {
                "project_id": project.id,
                "target": project.target,
                "action": "sort",
                "kind": entry.kind,
                "original_name": payload.path,
                "final_name": payload.filename,
                "label": payload.folder,
                "destination": destination,
                "sha256": digest,
                "size": entry.size,
                "document_id": observed["document_id"] if observed else None,
                "review_type": "manual",
                "file_mtime_ns": str(entry.mtime_ns),
            }
            decision = journaled_move(
                project,
                digest or "",
                move_plan(
                    "classify",
                    project,
                    Move(
                        "dump",
                        payload.path,
                        "tree",
                        destination,
                        entry.kind,
                        entry.size,
                        entry.mtime_ns,
                    ),
                    record=record,
                ),
                move=lambda: library.move_in(
                    payload.path, payload.folder, payload.filename
                ),
                reverse=lambda: library.move_back(destination, payload.path),
                commit=lambda operation: repository.record(
                    **record, operation_id=operation
                ),
            )
        except LibraryError as error:
            raise refuse(error) from error
        forget(project, payload.path)
        return {
            "destination": destination,
            "decision_id": decision.id,
            "document_id": decision.document_id,
        }

    @app.post("/api/projects/{project_id}/discard")
    @serialized
    def discard(payload: EntryAction, project: CurrentProject) -> dict[str, Any]:
        library = library_for(project)
        try:
            entry = library.entry(payload.path)
            library.check_unchanged(entry, payload.size, int(payload.mtime_ns))
            digest = entry_hash(project, library, payload.path)
            observed = (
                index.observe(
                    project.source,
                    Copy("dump", payload.path, entry.size or 0, str(entry.mtime_ns)),
                    digest,
                )
                if entry.kind == "file"
                else None
            )
            library.check_unchanged(
                library.entry(payload.path), payload.size, int(payload.mtime_ns)
            )
            destination = library.discard_destination(payload.path)
            record: dict[str, Any] = {
                "project_id": project.id,
                "target": project.target,
                "action": "discard",
                "kind": entry.kind,
                "original_name": payload.path,
                "final_name": destination.rsplit("/", 1)[-1],
                "label": DISCARD_FOLDER,
                "destination": destination,
                "sha256": digest,
                "size": entry.size,
                "document_id": observed["document_id"] if observed else None,
                "review_type": "manual",
                "file_mtime_ns": str(entry.mtime_ns),
            }
            decision = journaled_move(
                project,
                digest or "",
                move_plan(
                    "discard",
                    project,
                    Move(
                        "dump",
                        payload.path,
                        "tree",
                        destination,
                        entry.kind,
                        entry.size,
                        entry.mtime_ns,
                    ),
                    record=record,
                ),
                move=lambda: library.discard(payload.path, destination),
                reverse=lambda: library.move_back(destination, payload.path),
                commit=lambda operation: repository.record(
                    **record, operation_id=operation
                ),
            )
        except LibraryError as error:
            raise refuse(error) from error
        forget(project, payload.path)
        return {"destination": destination, "decision_id": decision.id}

    @app.post("/api/projects/{project_id}/skip", status_code=204)
    def skip(payload: SkipRequest, project: CurrentProject) -> None:
        try:
            library_for(project).entry(payload.path)
        except LibraryError as error:
            raise refuse(error) from error
        repository.skip(project.id, payload.path)

    @app.post("/api/projects/{project_id}/undo")
    @serialized
    def undo(
        project: CurrentProject,
        payload: Annotated[UndoRequest | None, Body()] = None,
    ) -> dict[str, Any]:
        library = library_for(project)
        with library.lock:
            decision = repository.last_active(project.id)
            if decision is None:
                raise HTTPException(404, "There is nothing to undo in this project")
            expected = payload.expect_decision_id if payload else None
            expected_row = (
                repository.decision(expected)
                if expected is not None and decision.id != expected
                else None
            )
            # KEEP ONE writes several decisions; any of them names the group.
            same_group = (
                expected_row is not None
                and expected_row.duplicate_group_id is not None
                and expected_row.duplicate_group_id == decision.duplicate_group_id
            )
            if expected is not None and decision.id != expected and not same_group:
                raise HTTPException(
                    409,
                    "That action is no longer the latest in this project, so it "
                    "was not undone. Use UNDO to step back from the latest",
                )
            if decision.duplicate_group_id is not None:
                return undo_duplicates(project, library, decision.duplicate_group_id)
            try:
                if decision.kind == "file":
                    observed = index.ensure(project, "tree", decision.destination)
                    if observed["document_id"] != decision.document_id:
                        raise LibraryError(
                            "The document changed after this decision; "
                            "review it before Undo",
                            409,
                        )
                units = folder_units(project.target)
                previous = decision.previous_destination
                if previous == decision.destination:
                    # A review confirmation moved nothing; there is no rename
                    # to journal.
                    library.sorted_entry_path(decision.destination, units)
                    repository.mark_undone(decision.id)
                else:
                    size = mtime_ns = None
                    if decision.kind == "file":
                        current = library.sorted_entry(decision.destination, units)
                        size, mtime_ns = current.size, current.mtime_ns
                    move: Callable[[], object]
                    reverse: Callable[[], object]
                    if previous is not None:
                        back = Move(
                            "tree",
                            decision.destination,
                            "tree",
                            previous,
                            decision.kind,
                            size,
                            mtime_ns,
                        )

                        def move() -> None:
                            library.restore_sorted(
                                decision.destination, previous, units
                            )

                        def reverse() -> None:
                            library.restore_sorted(
                                previous, decision.destination, units
                            )
                    else:
                        back = Move(
                            "tree",
                            decision.destination,
                            "dump",
                            decision.original_name,
                            decision.kind,
                            size,
                            mtime_ns,
                        )

                        def move() -> None:
                            library.move_back(
                                decision.destination, decision.original_name
                            )

                        def reverse() -> None:
                            rename_no_replace(
                                library.dump / decision.original_name,
                                library.sorted_root / decision.destination,
                            )

                    journaled_move(
                        project,
                        decision.sha256 or "",
                        move_plan(
                            "undo_decision", project, back, decision_id=decision.id
                        ),
                        move,
                        reverse,
                        lambda operation: repository.mark_undone(
                            decision.id, operation_id=operation
                        ),
                    )
            except LibraryError as error:
                raise refuse(error) from error
        if decision.previous_destination is not None:
            return {
                "path": decision.previous_destination,
                "area": "tree",
                "undone": decision.destination,
            }
        restore(project, decision.original_name)
        return {"path": decision.original_name, "undone": decision.destination}

    def undo_duplicates(
        project: Project, library: Library, group_id: int
    ) -> dict[str, Any]:
        decisions = repository.duplicate_group(group_id)
        active = {d.id for d in repository.tree_decisions(project.target)}
        if any(d.id not in active for d in decisions):
            raise HTTPException(
                409, "A copy was changed afterward. Undo its later decisions first."
            )
        units = folder_units(project.target)
        moves: list[tuple[Path, Path]] = []
        copies = []
        try:
            # The keeper was logged first. Restore it before the discarded
            # copies so adopting another copy's filename is reversible.
            for decision in decisions:
                source = library.sorted_entry_path(decision.destination, units)
                if file_sha256(source, 2**63 - 1) != decision.sha256:
                    raise LibraryError(
                        "A duplicate copy changed after the decision; Undo refused", 409
                    )
                stat = source.stat()
                observed = index.observe(
                    project.target,
                    Copy(
                        "tree",
                        decision.destination,
                        stat.st_size,
                        str(stat.st_mtime_ns),
                    ),
                    decision.sha256,
                    decision,
                )
                if observed["document_id"] != decision.document_id:
                    raise LibraryError(
                        "A duplicate document was replaced; Undo refused", 409
                    )
                area = "tree" if decision.previous_destination is not None else "dump"
                previous = decision.previous_destination or decision.original_name
                parts = entry_parts(previous)
                if area == "tree":
                    parent = library.category_path(
                        "/".join(parts[:-1]), units, discarded=True
                    )
                else:
                    parent = library.dump
                    for part in parts[:-1]:
                        parent = parent / part
                        if parent.is_symlink() or (
                            parent.exists() and not parent.is_dir()
                        ):
                            raise LibraryError(
                                "The original dump folder is no longer usable", 409
                            )
                    parent.mkdir(parents=True, exist_ok=True)
                target = parent / parts[-1]
                moves.append((source, target))
                copies.append(
                    {
                        "area": "tree",
                        "path": decision.destination,
                        "destination_area": area,
                        "destination": previous,
                    }
                )
            sources = {source for source, _ in moves}
            if any(
                (target.exists() or target.is_symlink()) and target not in sources
                for _, target in moves
            ):
                raise LibraryError(
                    "An original filename is now occupied; Undo refused", 409
                )
            apply_duplicate_moves(
                project,
                decisions[0].sha256 or "",
                {
                    "action": "undo",
                    "group_id": group_id,
                    "source": project.source,
                    "copies": copies,
                },
                moves,
                lambda operation: repository.undo_duplicate_group(group_id, operation),
            )
        except LibraryError as error:
            raise refuse(error) from error
        for decision in decisions:
            if decision.previous_destination is None:
                restore(project, decision.original_name)
        kept = decisions[0]
        return {
            "path": kept.previous_destination or kept.original_name,
            "area": "tree" if kept.previous_destination is not None else "dump",
            "undone": kept.destination,
            "restored_copies": len(decisions),
        }

    @app.get(
        "/api/projects/{project_id}/labels.jsonl", response_class=PlainTextResponse
    )
    @serialized
    def export_labels(project: CurrentProject) -> PlainTextResponse:
        """Every label for this project's tree, from every project feeding it."""
        library = library_for(project)
        units = folder_units(project.target)
        folders = library.folders(units)
        sources = {p.id: p for p in repository.projects(include_archived=True)}
        existing = list(library.sorted_files(units))
        inventory = {}
        duplicated_paths = {
            copy["path"]
            for group in index.groups(project, include_dump=False)["groups"]
            for copy in group["copies"]
        }
        for folder, name, kind in existing:
            if kind != "file":
                continue
            relative = f"{folder}/{name}" if folder else name
            indexed = index.lookup(project.target, relative)
            if indexed:
                stat = library.sorted_entry_path(relative, units).stat()
                inventory[relative] = {
                    **indexed,
                    "duplicate": relative in duplicated_paths,
                    "current": indexed["size"] == stat.st_size
                    and indexed["mtime_ns"] == str(stat.st_mtime_ns),
                }
        rows = labels.export_rows(
            repository.tree_decisions(project.target),
            folders,
            existing,
            sources,
            project.target,
            inventory,
        )
        body = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", project.target) or "labels"
        return PlainTextResponse(
            body,
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="{filename}.jsonl"'},
        )

    @app.get(
        "/api/projects/{project_id}/decision-log.jsonl",
        response_class=PlainTextResponse,
    )
    def export_decision_log(project: CurrentProject) -> PlainTextResponse:
        body = "".join(
            json.dumps(row, ensure_ascii=False) + "\n"
            for row in repository.decision_log(project.target)
        )
        return PlainTextResponse(
            body,
            media_type="application/x-ndjson",
            headers={
                "Content-Disposition": 'attachment; filename="decision-log.jsonl"'
            },
        )

    return app


app = create_app()

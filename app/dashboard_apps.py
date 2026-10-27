import asyncio
import hashlib
import hmac
import logging
import os
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal, NamedTuple
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse
from pydantic import ConfigDict

from app.app_contracts import (
    Contract,
    ContractError,
    Identifier,
    PublishedSource,
    Registration,
    Sha256,
    parse_document,
)
from app.app_deployment_contracts import (
    InstalledSnapshot,
    Operation,
    Receipt,
    fingerprint,
)
from app.app_deployments import (
    AppsService,
    RegistrationProposal,
    blocked_operation,
    eligible,
    installed,
    managed,
    pending_change,
    previews_to_retire,
    proposed_registration,
    redacted_error,
    require_idle,
    selection_for,
)
from app.app_discovery import ReleaseDiscovery
from app.app_github import GitHubPublications
from app.app_previews import PreviewResult, PreviewService
from app.app_publication import VerifiedPublication
from app.app_registry import decode_registration
from app.dashboard_auth import (
    SESSION_COOKIE,
    AdminSession,
    same_origin,
    session_claims,
    session_secret,
)
from ops.installer_policy import app_fingerprint, load_policy

logger = logging.getLogger(__name__)
router = APIRouter()
Step = Literal[
    "preview",
    "previewing",
    "preview_failed",
    "check_compatibility",
    "checking_compatibility",
    "compatibility_failed",
    "approve",
]
# One administrator action per app at a time in this process, so a double-click
# cannot interleave the preview pre-check, retirement and staging call.
locks: dict[str, asyncio.Lock] = {}


class Action(Contract):
    model_config = ConfigDict(strict=False, extra="forbid")

    app_id: Identifier
    digest: Sha256
    request_id: UUID
    preview_id: UUID | None = None
    rehearsal_id: UUID | None = None
    registration_sha256: Sha256 | None = None


def service(request: Request) -> AppsService:
    path = os.environ.get("HOME_PLATFORM_INSTALLER_POLICY")
    if path is None:
        raise HTTPException(503, "Application installation is not configured")
    policy = load_policy(Path(path))

    def policy_registration(app_id: str) -> Registration | None:
        try:
            return policy.app(app_id).registration
        except ContractError:
            return None

    return AppsService(
        request.app.state.app_registry.database,
        Path(policy.spool),
        lambda app_id: app_fingerprint(policy, app_id),
        policy_registration=policy_registration,
    )


def csrf(request: Request) -> str:
    cookie = request.cookies.get(SESSION_COOKIE, "")
    return hmac.new(
        session_secret(request).encode(),
        ("apps-csrf:" + cookie).encode(),
        hashlib.sha256,
    ).hexdigest()


def mutation(request: Request) -> None:
    if not same_origin(request) or not hmac.compare_digest(
        request.headers.get("x-csrf-token", ""), csrf(request)
    ):
        raise HTTPException(403, "Same-origin request and CSRF token required")


@router.get("/dashboard/apps", response_class=HTMLResponse)
def page(request: Request, identity: AdminSession) -> str:
    return Path(__file__).with_name("apps.html").read_text()


@dataclass
class Release:
    digest: str
    commit: str
    commit_url: str
    title: str | None
    committed_at: str | None
    version: str | None


@dataclass(frozen=True)
class Activity:
    """One card's status line, shown beside its buttons.

    ``active`` means an operation holds the app, so its actions are locked.
    ``final`` means nothing changes until the administrator acts, so the page
    stops polling for it.
    """

    tone: Literal["progress", "failed"]
    text: str
    active: bool
    final: bool


class OperationFacts(NamedTuple):
    kind: str
    state: str
    error: str | None
    reconcile_error: str | None
    reconcile_failed_at: str | None


PROGRESSIVE = {
    "rehearse": "Checking compatibility",
    "deploy": "Deploying",
    "rollback": "Rolling back",
    "recover": "Recovering",
}
NOUN = {
    "rehearse": "compatibility check",
    "deploy": "deploy",
    "rollback": "rollback",
    "recover": "recovery",
}


def activity(operations: Sequence[OperationFacts], previewing: bool) -> Activity | None:
    """The status line for an app's operations, newest first.

    ``operations`` holds at least every queued, switching or blocked operation
    and the newest one. A running compatibility check and a running preview get
    no text, because the card's update section already names that step.
    """
    running = next(
        (item for item in operations if item.state in ("queued", "switching")), None
    )
    if running is not None:
        doing = PROGRESSIVE[running.kind]
        if running.reconcile_error:
            return Activity(
                "failed",
                f"{doing} · not reconciled since {running.reconcile_failed_at}: "
                f"{running.reconcile_error}",
                True,
                False,
            )
        if running.kind == "rehearse":
            return Activity("progress", "", True, False)
        if running.state == "switching":
            return Activity("progress", doing + " · switching containers…", True, False)
        return Activity("progress", doing + "…", True, False)
    if previewing:
        return Activity("progress", "", False, False)
    blocked = next((item for item in operations if item.state == "blocked"), None)
    if blocked is not None:
        return Activity(
            "failed",
            "Needs recovery" + (": " + words(blocked.error) if blocked.error else ""),
            True,
            True,
        )
    last = operations[0] if operations else None
    if last is None or last.kind == "rehearse" or last.state == "succeeded":
        return None
    return Activity(
        "failed",
        f"Last {NOUN[last.kind]} failed"
        + (": " + words(last.error) if last.error else "")
        + (" · previous release restored" if last.state == "recovered" else ""),
        False,
        True,
    )


def activities(
    connection: sqlite3.Connection, app_id: str | None = None
) -> dict[str, Activity | None]:
    """Every registered app's activity, or only ``app_id``'s.

    Reads operations and preview attempts only, so it is cheap enough to poll.
    """
    names = [
        row[0]
        for row in connection.execute(
            "SELECT app_id FROM app_registrations WHERE ? IS NULL OR app_id = ? "
            "ORDER BY app_id",
            (app_id, app_id),
        )
    ]
    operations: dict[str, list[sqlite3.Row]] = {}
    for row in connection.execute(
        (
            "SELECT app_id, kind, state, receipt_json, reconcile_error, "
            "reconcile_failed_at FROM app_operations AS o WHERE "
            "(? IS NULL OR app_id = ?) AND (state IN ('queued', 'switching', "
            "'blocked') OR rowid = (SELECT rowid FROM app_operations WHERE "
            "app_id = o.app_id ORDER BY created_at DESC, rowid DESC LIMIT 1)) "
            "ORDER BY created_at DESC, rowid DESC"
        ),
        (app_id, app_id),
    ):
        operations.setdefault(row[0], []).append(row)
    previewing = {
        row[0]
        for row in connection.execute(
            "SELECT app_id FROM app_preview_attempts WHERE state = 'installing'"
        )
    }
    found: dict[str, Activity | None] = {}
    for name in names:
        try:
            found[name] = activity(
                [
                    OperationFacts(
                        row[1],
                        row[2],
                        parse_document(Receipt, row[3].encode()).error
                        if row[3]
                        else None,
                        row[4],
                        row[5],
                    )
                    for row in operations.get(name, [])
                ],
                name in previewing,
            )
        except Exception as error:
            logger.warning("Application activity %s is unavailable: %s", name, error)
            found[name] = None
    return found


@router.get("/dashboard/api/apps/activity")
def activity_feed(
    request: Request, identity: AdminSession
) -> dict[str, Activity | None]:
    with service(request).database.connect() as connection:
        return activities(connection)


@dataclass
class Card:
    app_id: str
    label: str
    settings: list[str]
    error: str | None = None
    installed: str | None = None
    running: Release | None = None
    installed_capabilities: list[str] = field(default_factory=list)
    installed_secrets: list[str] = field(default_factory=list)
    pending: dict[str, object] | None = None
    rollback: dict[str, object] | None = None
    restart_notice: str | None = None
    recover_notice: str | None = None
    operations: list[dict[str, object]] = field(default_factory=list)
    activity: Activity | None = None


@router.get("/dashboard/api/apps")
def overview(request: Request, identity: AdminSession) -> dict[str, object]:
    apps = service(request)
    cards = []
    with apps.database.connect() as connection:
        instance = connection.execute(
            "SELECT environment FROM platform_instance"
        ).fetchone()
        environment = instance[0] if instance else "production"
        for row in connection.execute(
            "SELECT app_id, route, definition_json FROM app_registrations "
            "ORDER BY app_id"
        ).fetchall():
            try:
                registration = decode_registration(*row)
                if not managed(registration):
                    continue
                item = jsonable_encoder(
                    card(connection, apps, registration, environment)
                )
            except Exception as error:
                logger.warning(
                    "Application card %s is unavailable: %s",
                    row[0],
                    error,
                    exc_info=not isinstance(error, (ContractError, OSError)),
                )
                item = asdict(Card(row[0], row[0], [], error=redacted_error(error)))
            cards.append(item)
    return {"csrf": csrf(request), "apps": cards}


def heading(registration: Registration) -> tuple[str, str, list[str]]:
    return (
        registration.app_id,
        registration.presentation.label,
        [item.name for item in registration.bindings.settings],
    )


def describe(
    connection: sqlite3.Connection,
    registration: Registration,
    release: VerifiedPublication,
    version: str | None,
) -> Release:
    source = registration.source
    assert isinstance(source, PublishedSource)
    digest = release.publication.manifest_sha256
    title, committed_at = connection.execute(
        "SELECT commit_title, committed_at FROM app_eligible_releases "
        "WHERE app_id = ? AND manifest_sha256 = ?",
        (registration.app_id, digest),
    ).fetchone()
    commit = release.manifest.source_commit
    return Release(
        digest,
        commit,
        f"https://github.com/{source.repository}/commit/{commit}",
        title,
        committed_at,
        version,
    )


def previewed_version(connection: sqlite3.Connection, preview_id: UUID) -> str | None:
    row = connection.execute(
        "SELECT result_json FROM app_preview_attempts WHERE attempt_id = ?",
        (str(preview_id),),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return parse_document(PreviewResult, row[0].encode()).version


MiB = 1024 * 1024
RESOURCES: tuple[tuple[str, str, str], ...] = (
    ("memory_bytes", "memory", " MiB"),
    ("cpu_limit", "CPU", " cores"),
    ("pids_limit", "process limit", ""),
    ("maintenance_timeout_seconds", "maintenance timeout", " s"),
)


def shortened(value: str) -> str:
    return value if len(value) <= 40 else value[:39] + "…"


def named_changes(
    noun: str,
    before: dict[str, str],
    after: dict[str, str],
    join: str = "",
    show: Callable[[str], str] = str,
) -> list[str]:
    """One line per added, removed or changed binding, naming its values."""
    return [
        *(
            f"adds {noun} {name}" + (f" {join} {show(after[name])}" if join else "")
            for name in sorted(after.keys() - before.keys())
        ),
        *(
            f"removes {noun} {name}" + (f" (was {show(before[name])})" if join else "")
            for name in sorted(before.keys() - after.keys())
        ),
        *(
            f"changes {noun} {name} {show(before[name])} → {show(after[name])}"
            for name in sorted(before.keys() & after.keys())
            if before[name] != after[name]
        ),
    ]


def settings_change(before: Registration, after: Registration) -> list[str]:
    old, new = before.bindings, after.bindings
    lines = []
    for name, label, unit in RESOURCES:
        values = [getattr(item.resources, name) for item in (old, new)]
        if name == "memory_bytes":
            values = [value / MiB for value in values]
        if values[0] != values[1]:
            lines.append(f"{label} {values[0]:g} → {values[1]:g}{unit}")
    lines += named_changes(
        "setting",
        {item.name: item.value for item in old.settings},
        {item.name: item.value for item in new.settings},
        "=",
        shortened,
    )
    lines += named_changes(
        "secret file",
        {item.name: item.file_path for item in old.secret_files},
        {item.name: item.file_path for item in new.secret_files},
        "at",
    )
    lines += named_changes(
        "integration",
        dict.fromkeys(old.integration_bindings, ""),
        dict.fromkeys(new.integration_bindings, ""),
    )
    if old.backup_directory != new.backup_directory:
        lines.append(
            f"moves backups {old.backup_directory or 'none'} → "
            f"{new.backup_directory or 'none'}"
        )
    if before.presentation.label != after.presentation.label:
        lines.append(f"renames to {after.presentation.label}")
    if before.presentation.model_copy(update={"label": after.presentation.label}) != (
        after.presentation
    ):
        lines.append("changes its Home tile")
    return lines or ["changes registration"]


def bindings_summary(registration: Registration) -> list[tuple[str, str]]:
    bindings = registration.bindings
    resources = bindings.resources
    return [
        (
            "Proposed resources",
            f"{resources.memory_bytes / MiB:g} MiB memory · "
            f"{resources.cpu_limit:g} CPU · {resources.pids_limit} processes · "
            f"{resources.maintenance_timeout_seconds} s maintenance",
        ),
        (
            "Proposed settings",
            ", ".join(
                f"{item.name}={shortened(item.value)}"
                for item in sorted(bindings.settings, key=lambda item: item.name)
            )
            or "None",
        ),
        (
            "Proposed secret files",
            ", ".join(
                f"{item.name} at {item.file_path}" for item in bindings.secret_files
            )
            or "None",
        ),
        ("Proposed backups", bindings.backup_directory or "None"),
        ("Proposed integrations", ", ".join(bindings.integration_bindings) or "None"),
    ]


def words(name: str) -> str:
    return name.replace("_", " ").replace("-", " ")


def next_step(
    preview_state: str | None,
    result: PreviewResult | None,
    rehearsed: bool,
    latest_rehearsal: tuple[str, str | None] | None,
) -> tuple[Step, str]:
    if preview_state is None:
        return "preview", "Not previewed yet"
    if preview_state == "installing":
        return "previewing", "Preview running…"
    if preview_state != "ready":
        checks = result.checks if result else {}
        failed = ", ".join(words(name) for name, ok in checks.items() if not ok)
        if failed:
            return "preview_failed", "Preview failed · failed checks: " + failed
        error = result.error if result else None
        return (
            "preview_failed",
            "Preview failed: " + error if error else "Preview failed",
        )
    ready = "Preview ready · all checks passed · "
    if rehearsed:
        return "approve", ready + "compatibility passed"
    if latest_rehearsal is None or latest_rehearsal[0] == "succeeded":
        return "check_compatibility", ready + "compatibility not checked yet"
    if latest_rehearsal[0] in {"queued", "switching"}:
        return "checking_compatibility", ready + "checking compatibility…"
    state, error = latest_rehearsal
    reason = (
        "this release would change stored records"
        if error == "records_changed"
        else words(error)
        if error
        else state
    )
    return "compatibility_failed", ready + "compatibility check failed: " + reason


def admits(
    connection: sqlite3.Connection, registration: Registration, digest: str
) -> bool:
    try:
        eligible(connection, registration, digest)
    except ContractError:
        return False
    return True


def card(
    connection: sqlite3.Connection,
    apps: AppsService,
    registration: Registration,
    environment: str,
) -> Card:
    app_id = registration.app_id
    current = installed(connection, app_id, environment)
    previous = installed(connection, app_id, environment, 1)
    installed_digest = current.selection.manifest_sha256 if current else None
    running = (
        eligible(connection, current.registration, installed_digest)
        if current and installed_digest
        else None
    )
    releases = connection.execute(
        (
            "WITH offered AS (SELECT manifest_sha256, release_id, EXISTS "
            "(SELECT 1 FROM app_preview_attempts WHERE app_id = "
            "app_eligible_releases.app_id AND manifest_sha256 = "
            "app_eligible_releases.manifest_sha256 AND state != 'retired') "
            "AS previewed FROM app_eligible_releases WHERE app_id = ? AND "
            "NOT EXISTS (SELECT 1 FROM app_rejections WHERE app_id = "
            "app_eligible_releases.app_id AND manifest_sha256 = "
            "app_eligible_releases.manifest_sha256)) "
            "SELECT manifest_sha256 FROM offered WHERE previewed OR "
            "release_id > COALESCE((SELECT release_id FROM "
            "app_eligible_releases WHERE app_id = ? AND manifest_sha256 = ?), "
            "-1) ORDER BY previewed DESC, release_id DESC"
        ),
        (app_id, app_id, installed_digest),
    ).fetchall()
    change = pending_change(connection, app_id, environment) if current else None
    proposed = change.registration if change else registration
    policy = apps.policy_registration(app_id)
    blocked = blocked_operation(connection, app_id) is not None
    candidate = next(
        (
            digest
            for (digest,) in releases
            if digest != installed_digest and admits(connection, proposed, digest)
        ),
        installed_digest if change else None,
    )
    pending: dict[str, object] | None = None
    if candidate is not None:
        publication = eligible(connection, proposed, candidate)
        source = registration.source
        assert isinstance(source, PublishedSource)
        preview = connection.execute(
            (
                "SELECT attempt_id, state, result_json FROM "
                "app_preview_attempts WHERE app_id = ? AND manifest_sha256 = "
                "? AND state != 'retired'"
            ),
            (app_id, candidate),
        ).fetchone()
        rehearsals = connection.execute(
            (
                "SELECT operation_id, state, receipt_json, operation_json FROM "
                "app_operations WHERE app_id = ? AND kind = 'rehearse' "
                "ORDER BY created_at DESC, rowid DESC"
            ),
            (app_id,),
        ).fetchall()
        offered = (candidate, fingerprint(proposed))

        def offers(operation_json: str) -> bool:
            selected = parse_document(
                Operation, operation_json.encode()
            ).approval.selection
            return (selected.manifest_sha256, selected.registration_sha256) == offered

        latest_rehearsal = next(
            (
                (
                    item[1],
                    parse_document(Receipt, item[2].encode()).error
                    if item[2]
                    else None,
                )
                for item in rehearsals
                if offers(item[3])
            ),
            None,
        )
        match = None
        if preview and preview[1] == "ready":
            try:
                approved = selection_for(
                    connection,
                    app_id,
                    candidate,
                    UUID(preview[0]),
                    apps.policy_fingerprint(app_id),
                )
            except ContractError:
                approved = None
            match = next(
                (
                    item[0]
                    for item in rehearsals
                    if item[1] == "succeeded"
                    and parse_document(Receipt, item[2].encode()).selection == approved
                ),
                None,
            )
        result = (
            parse_document(PreviewResult, preview[2].encode())
            if preview and preview[2]
            else None
        )
        step, status = next_step(
            preview[1] if preview else None,
            result,
            match is not None,
            latest_rehearsal,
        )
        mismatch = policy_notice(
            policy,
            proposed,
            "Update this app's host policy entry to match "
            + ("this settings change" if change else "its registration")
            + ", then check compatibility.",
        )
        if mismatch:
            status = mismatch
        manifest = publication.manifest
        pending = {
            "digest": candidate,
            "release": describe(
                connection,
                registration,
                publication,
                result.version if result else None,
            ),
            "step": step,
            "status": status,
            "image": manifest.image,
            "workflow_url": f"https://github.com/{source.repository}/actions/runs/{publication.publication.run_id}",
            "capabilities": list(manifest.capabilities),
            "secrets": list(manifest.secrets),
            "added_capabilities": sorted(
                set(manifest.capabilities)
                - set(running.manifest.capabilities if running else ())
            ),
            "added_secrets": sorted(
                set(manifest.secrets) - set(running.manifest.secrets if running else ())
            ),
            "preview_id": preview[0] if preview else None,
            "preview_state": preview[1] if preview else None,
            "preview_url": None,
            "preview_checks": {},
            "rehearsal_id": match,
            "settings_change": settings_change(
                current.registration, change.registration
            )
            if current and change
            else [],
            "registration_sha256": fingerprint(change.registration) if change else None,
            "proposed_bindings": bindings_summary(change.registration)
            if change
            else [],
            "policy_mismatch": mismatch is not None,
        }
        if result is not None:
            if preview[1] == "ready":
                pending["preview_url"] = result.preview_url
            pending["preview_checks"] = result.checks
    operations = connection.execute(
        (
            "SELECT operation_id, kind, state, receipt_json, operation_json, "
            "reconcile_error, reconcile_failed_at FROM app_operations WHERE "
            "app_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 5"
        ),
        (app_id,),
    ).fetchall()
    return Card(
        *heading(registration),
        installed=installed_digest,
        running=describe(
            connection,
            current.registration,
            running,
            previewed_version(connection, current.selection.preview_id),
        )
        if current and running
        else None,
        installed_capabilities=list(running.manifest.capabilities) if running else [],
        installed_secrets=list(running.manifest.secrets) if running else [],
        pending=pending,
        rollback=rollback_offer(previous, policy) if previous else None,
        restart_notice=restart_notice(current, change, policy, blocked)
        if current
        else None,
        recover_notice=policy_notice(
            policy,
            registration,
            "Recover needs this app's host policy registration reverted to the "
            "accepted registration first.",
        )
        if blocked
        else None,
        operations=[
            {
                "id": item[0],
                "kind": item[1],
                "state": item[2],
                "error": parse_document(Receipt, item[3].encode()).error
                if item[3]
                else None,
                "digest": parse_document(
                    Operation, item[4].encode()
                ).approval.selection.manifest_sha256,
                "reconcile_error": item[5],
                "reconcile_failed_at": item[6],
            }
            for item in operations
        ],
        activity=activities(connection, app_id)[app_id],
    )


def by_name(registration: Registration) -> Registration:
    bindings = registration.bindings
    return registration.model_copy(
        update={
            "bindings": bindings.model_copy(
                update={
                    "settings": tuple(
                        sorted(bindings.settings, key=lambda item: item.name)
                    ),
                    "secret_files": tuple(
                        sorted(bindings.secret_files, key=lambda item: item.name)
                    ),
                    "integration_bindings": tuple(
                        sorted(bindings.integration_bindings)
                    ),
                }
            )
        }
    )


def policy_notice(
    policy: Registration | None, expected: Registration, notice: str
) -> str | None:
    """``notice`` when the host policy registration is not ``expected``, else None.

    Root compares them exactly, so the same bindings listed in another order
    still differ; the notice then says that order is all there is to fix.
    """
    if policy is None or policy == expected:
        return None
    if by_name(policy) == by_name(expected):
        return (
            notice + " Only the order differs: list its settings, secret files "
            "and integration bindings in the same order as the registration."
        )
    return notice


def rollback_offer(
    target: InstalledSnapshot, policy: Registration | None
) -> dict[str, object]:
    notice = policy_notice(
        policy,
        target.registration,
        "Rollback needs this app's host policy entry returned to the "
        "previous registration first.",
    )
    return {
        "digest": target.selection.manifest_sha256,
        "preview_id": str(target.selection.preview_id),
        "policy_mismatch": notice is not None,
        "notice": notice,
    }


def restart_notice(
    current: InstalledSnapshot,
    change: RegistrationProposal | None,
    policy: Registration | None,
    blocked: bool,
) -> str | None:
    """Root boots only the installed registration, so say when it cannot."""
    return policy_notice(
        policy,
        current.registration,
        "Its host policy registration is not the installed one, so the app "
        "cannot restart until that registration is reverted."
        if blocked
        else "Its host policy entry already has the settings change, so the "
        "running app cannot restart until that change is deployed."
        if change is not None and policy == change.registration
        else "Its host policy entry no longer matches the installed registration, "
        "so the running app cannot restart until they match again.",
    )


@router.post("/dashboard/api/apps/{action}")
async def act(
    action: str, body: Action, request: Request, identity: AdminSession
) -> dict[str, object]:
    mutation(request)
    apps = service(request)
    claims = session_claims(request, request.cookies.get(SESSION_COOKIE))
    assert claims is not None
    async with locks.setdefault(body.app_id, asyncio.Lock()):
        try:
            return await perform(action, body, request, identity, apps, claims[1])
        except ContractError as error:
            raise HTTPException(409, str(error)) from None
        except OSError:
            logger.exception("Application action %s failed", action)
            raise HTTPException(
                409,
                (
                    "The selected operation could not be accepted. Refresh its "
                    "checks and try again."
                ),
            ) from None
        except HTTPException:
            raise
        except Exception:
            logger.exception("Application action %s failed", action)
            raise HTTPException(503, "Application records are unavailable") from None


async def perform(
    action: str,
    body: Action,
    request: Request,
    identity: AdminSession,
    apps: AppsService,
    session_version: int,
) -> dict[str, object]:
    if action == "reject":
        apps.reject(
            body.app_id,
            body.digest,
            identity.id,
            session_version,
            body.registration_sha256,
        )
        return {"state": "rejected"}
    if action == "check":
        async with httpx.AsyncClient(trust_env=False) as client:
            await ReleaseDiscovery(
                apps.database,
                GitHubPublications(
                    client, request.app.state.settings.release_read_token
                ),
            ).discover(body.app_id)
        return {"state": "checked"}
    preview_id = body.preview_id
    if action == "preview":
        origin = os.environ.get("HOME_PLATFORM_PREVIEW_ORIGIN")
        token_file = os.environ.get("HOME_PLATFORM_PREVIEW_TOKEN_FILE")
        if not origin or not token_file:
            raise ContractError("Preview is not configured")
        async with httpx.AsyncClient(trust_env=False) as client:
            previews = PreviewService(
                apps.database, client, origin, Path(token_file).read_text().strip()
            )
            with apps.database.connect() as connection:
                require_idle(connection, body.app_id)
                eligible(
                    connection,
                    proposed_registration(connection, body.app_id),
                    body.digest,
                )
                stale = previews_to_retire(connection, body.app_id, body.digest)
            for attempt in stale:
                await previews.retire(attempt)
            result = await previews.preview(body.app_id, body.digest)
            if result.state != "ready":
                return {"state": result.state}
            preview_id = result.attempt_id
        kind: Literal["rehearse", "deploy", "rollback", "recover"] = "rehearse"
    elif action == "deploy":
        kind = "deploy"
    elif action == "rollback":
        kind = "rollback"
    elif action == "recover":
        kind = "recover"
    else:
        raise HTTPException(404, "Unknown action")
    operation = apps.request(
        kind,
        body.app_id,
        body.digest,
        preview_id,
        identity.id,
        session_version,
        body.request_id,
        body.rehearsal_id,
        body.registration_sha256,
    )
    return {"operation_id": str(operation.operation_id), "state": "queued"}

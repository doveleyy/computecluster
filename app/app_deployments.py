import base64
import json
import logging
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from app.app_contracts import (
    Contract,
    ContractError,
    LegacyInstalled,
    LegacySource,
    ManagedInstalled,
    PublishedSource,
    Registration,
    Sha256,
    parse_document,
)
from app.app_deployment_contracts import (
    KINDS,
    AcceptedSnapshot,
    Approval,
    InstalledSnapshot,
    InstallerCapabilities,
    Kind,
    Operation,
    OperationHint,
    Receipt,
    RetiredSnapshot,
    Selection,
    decode_accepted,
    fingerprint,
    running_of,
)
from app.app_previews import CHECKS, PreviewResult
from app.app_publication import VerifiedPublication, verify_package
from app.app_registry import AppRegistry, decode_registration
from app.app_spool import publish, read_owned
from app.app_workloads import WorkloadStore
from app.database import Database

logger = logging.getLogger(__name__)
RUNTIME_LACKS_RETIRE = "The host installer runtime must be rebuilt before it can retire"


class Authority(Contract):
    operation: Operation
    registration: Registration
    credential_path: str
    credential_verifier: str
    archive_base64: str
    artifact_id: int
    previous: AcceptedSnapshot | None
    app_api_url: str
    timezone: str


def accepted(
    connection: sqlite3.Connection, app_id: str, environment: str, offset: int = 0
) -> AcceptedSnapshot | None:
    """The latest accepted generation, running or retired."""
    row = connection.execute(
        (
            "SELECT snapshot_json FROM app_installed_history WHERE app_id"
            " = ? AND environment = ? ORDER BY generation DESC LIMIT 1 "
            "OFFSET ?"
        ),
        (app_id, environment, offset),
    ).fetchone()
    return decode_accepted(row[0].encode()) if row else None


def installed(
    connection: sqlite3.Connection, app_id: str, environment: str, offset: int = 0
) -> InstalledSnapshot | None:
    """The running installation, or with ``offset`` 1 its rollback target.

    None while retired. A retirement between the running generation and an
    older one is not a rollback target either: its container is gone.
    """
    latest = running_of(accepted(connection, app_id, environment))
    if offset == 0 or latest is None:
        return latest
    return running_of(accepted(connection, app_id, environment, offset))


def retirement(
    connection: sqlite3.Connection, app_id: str, environment: str
) -> RetiredSnapshot | None:
    current = accepted(connection, app_id, environment)
    return current if isinstance(current, RetiredSnapshot) else None


def environment_of(connection: sqlite3.Connection) -> str | None:
    row = connection.execute("SELECT environment FROM platform_instance").fetchone()
    return str(row[0]) if row else None


def require_idle(
    connection: sqlite3.Connection, app_id: str, *, recovering: bool = False
) -> None:
    states = ("queued", "switching") + (() if recovering else ("blocked",))
    if connection.execute(
        "SELECT 1 FROM app_operations WHERE app_id = ? AND state IN ("
        + ",".join("?" * len(states))
        + ")",
        (app_id, *states),
    ).fetchone():
        raise ContractError("An operation is already pending")


def registered(connection: sqlite3.Connection, app_id: str) -> Registration:
    row = connection.execute(
        "SELECT app_id, route, definition_json FROM app_registrations WHERE app_id = ?",
        (app_id,),
    ).fetchone()
    if row is None:
        raise ContractError("Application is not registered")
    return decode_registration(*row)


def registration_for(connection: sqlite3.Connection, app_id: str) -> Registration:
    registration = registered(connection, app_id)
    if not managed(registration):
        raise ContractError("Application is not managed")
    return registration


def managed(registration: Registration) -> bool:
    return isinstance(registration.installation, ManagedInstalled) and isinstance(
        registration.source, PublishedSource
    )


def legacy(registration: Registration) -> bool:
    return isinstance(registration.installation, LegacyInstalled) and isinstance(
        registration.source, LegacySource
    )


@dataclass(frozen=True)
class RegistrationProposal:
    proposal_id: int
    registration: Registration


def pending_change(
    connection: sqlite3.Connection, app_id: str, environment: str
) -> RegistrationProposal | None:
    """The unapproved registration change for the accepted installation.

    A proposal applies to the generation it was made against, so any later
    accepted install, including a rollback, retires it.
    """
    current = installed(connection, app_id, environment)
    if current is None:
        return None
    row = connection.execute(
        "SELECT proposal.proposal_id, proposal.definition_json, "
        "rejection.proposal_id IS NOT NULL FROM app_registration_proposals AS "
        "proposal LEFT JOIN app_registration_rejections AS rejection USING "
        "(proposal_id) WHERE proposal.app_id = ? AND proposal.environment = ? "
        "AND proposal.base_generation = ? ORDER BY proposal.proposal_id DESC "
        "LIMIT 1",
        (app_id, environment, current.generation),
    ).fetchone()
    if row is None or row[2]:
        return None
    registration = decode_registration(app_id, current.registration.route, row[1])
    if registration == current.registration:
        return None
    return RegistrationProposal(row[0], registration)


PREVIEW_SLOTS = 2


def previews_to_evict(
    connection: sqlite3.Connection, app_id: str, digest: str
) -> list[UUID]:
    """The previews to evict so staging can admit ``digest`` for ``app_id``.

    Staging holds two previews, and any preview can run again. So evict those
    no card can still offer (a rejected release, or the installed one with no
    settings change pending), the app's own failed or other-release attempt,
    and then the oldest previews of idle apps while staging would stay full.
    """
    environment = environment_of(connection)
    evict = []
    others = []
    for attempt_id, owner, release, state, rejected in connection.execute(
        "SELECT attempt_id, app_id, manifest_sha256, state, EXISTS (SELECT 1 "
        "FROM app_rejections WHERE app_id = app_preview_attempts.app_id AND "
        "manifest_sha256 = app_preview_attempts.manifest_sha256) FROM "
        "app_preview_attempts WHERE state != 'retired' ORDER BY rowid"
    ).fetchall():
        current = installed(connection, owner, environment) if environment else None
        if (
            rejected
            or (owner == app_id and (release != digest or state == "failed"))
            or (
                environment is not None
                and current is not None
                and current.selection.manifest_sha256 == release
                and pending_change(connection, owner, environment) is None
            )
        ):
            evict.append(UUID(attempt_id))
        elif owner != app_id:
            others.append((UUID(attempt_id), owner, state))
    excess = len(others) - (PREVIEW_SLOTS - 1)
    for attempt, owner, state in others:
        if excess <= 0:
            break
        # Staging refuses to evict an installation still in progress.
        if state != "installing" and idle(connection, owner):
            evict.append(attempt)
            excess -= 1
    return evict


def idle(connection: sqlite3.Connection, app_id: str) -> bool:
    try:
        require_idle(connection, app_id)
    except ContractError:
        return False
    return True


def proposed_registration(connection: sqlite3.Connection, app_id: str) -> Registration:
    registration = registration_for(connection, app_id)
    environment = environment_of(connection)
    change = pending_change(connection, app_id, environment) if environment else None
    return change.registration if change else registration


def approved_registration(
    connection: sqlite3.Connection, app_id: str, kind: Kind
) -> Registration:
    """The registration an operation of ``kind`` binds.

    A recovery binds the accepted registration, never the blocked operation's,
    so reverting the host policy to it is always a way out.
    """
    registration = registration_for(connection, app_id)
    if kind == "recover":
        if blocked_operation(connection, app_id) is None:
            raise ContractError("Recovery target changed")
        return registration
    if kind in {"rollback", "retire"}:
        environment = environment_of(connection)
        offset = 1 if kind == "rollback" else 0
        target = (
            installed(connection, app_id, environment, offset) if environment else None
        )
        if target is None:
            raise ContractError(
                "Rollback target changed"
                if kind == "rollback"
                else "Nothing is installed to retire"
            )
        return target.registration
    return proposed_registration(connection, app_id)


def release_registration(
    connection: sqlite3.Connection, app_id: str, kind: Kind
) -> Registration:
    """The registration that admits the release an operation of ``kind`` selects.

    A recovery selects the blocked operation's release, which may need a
    binding only that operation's registration has.
    """
    if kind == "recover":
        blocked = blocked_operation(connection, app_id)
        if blocked is None:
            raise ContractError("Recovery target changed")
        kind = blocked.kind
    return approved_registration(connection, app_id, kind)


def shown_change(connection: sqlite3.Connection, selection: Selection) -> str | None:
    """The settings change a selection carries, as the card names it.

    That is its registration fingerprint when it differs from the installation
    it was made against, and None otherwise.
    """
    if selection.generation == 0:
        return None
    row = connection.execute(
        "SELECT snapshot_json FROM app_installed_history WHERE app_id = ? "
        "AND environment = ? AND generation = ?",
        (selection.app_id, selection.environment, selection.generation),
    ).fetchone()
    base = decode_accepted(row[0].encode()).registration
    return (
        None
        if fingerprint(base) == selection.registration_sha256
        else selection.registration_sha256
    )


def eligible(
    connection: sqlite3.Connection, registration: Registration, digest: str
) -> VerifiedPublication:
    row = connection.execute(
        "SELECT * FROM app_eligible_releases WHERE app_id = ? AND manifest_sha256 = ?",
        (registration.app_id, digest),
    ).fetchone()
    if row is None or not isinstance(registration.source, PublishedSource):
        raise ContractError("A workflow-verified release is required")
    release = verify_package(
        bytes(row["archive"]),
        registration.source,
        row["artifact_id"],
        row["artifact_sha256"],
    )
    AppRegistry._validate_release(registration, release.manifest)
    if release.publication.manifest_sha256 != digest:
        raise ContractError("Release fingerprint does not match")
    return release


def selection_for(
    connection: sqlite3.Connection,
    app_id: str,
    digest: str,
    preview_id: UUID,
    policy_sha256: str,
    kind: Kind = "deploy",
) -> Selection:
    registration = approved_registration(connection, app_id, kind)
    release = eligible(
        connection, release_registration(connection, app_id, kind), digest
    )
    instance = connection.execute("SELECT * FROM platform_instance").fetchone()
    credential = connection.execute(
        "SELECT * FROM app_credentials WHERE app_id = ? AND revoked_at IS NULL",
        (app_id,),
    ).fetchone()
    if (
        instance is None
        or credential is None
        or credential["environment"] != instance["environment"]
    ):
        raise ContractError("Active application credentials are required")
    if kind == "retire":
        current = installed(connection, app_id, instance["environment"])
        if current is None:
            raise ContractError("Nothing is installed to retire")
        if (
            digest != current.selection.manifest_sha256
            or preview_id != current.selection.preview_id
        ):
            raise ContractError("Retire target changed")
    if kind in {"rehearse", "deploy"}:
        if connection.execute(
            "SELECT 1 FROM app_rejections WHERE app_id = ? AND manifest_sha256 = ?",
            (app_id, digest),
        ).fetchone():
            raise ContractError("Selected release was rejected")
        row = connection.execute(
            (
                "SELECT result_json FROM app_preview_attempts WHERE "
                "attempt_id = ? AND state = 'ready'"
            ),
            (str(preview_id),),
        ).fetchone()
        if row is None:
            raise ContractError("A successful preview is required")
        preview = parse_document(PreviewResult, row[0].encode())
        if (
            preview.app_id != app_id
            or preview.manifest_sha256 != digest
            or preview.bundle_sha256 != release.artifact_sha256
            or set(preview.checks) != CHECKS
            or not all(preview.checks.values())
        ):
            raise ContractError("Preview does not match the selected release")
    previous = accepted(connection, app_id, instance["environment"])
    return Selection(
        app_id=app_id,
        environment=instance["environment"],
        manifest_sha256=digest,
        archive_sha256=release.artifact_sha256,
        registration_sha256=fingerprint(registration),
        policy_sha256=policy_sha256,
        credential_id=credential["credential_id"],
        generation=previous.generation if previous else 0,
        preview_id=preview_id,
    )


def read_authority(
    connection: sqlite3.Connection, operation_id: UUID, policy_sha256: str
) -> Authority:
    row = connection.execute(
        (
            "SELECT operation_json FROM app_operations WHERE operation_id"
            " = ? AND state IN ('queued', 'switching')"
        ),
        (str(operation_id),),
    ).fetchone()
    if row is None:
        raise ContractError("Operation is not active")
    operation = parse_document(Operation, row[0].encode())
    approval = operation.approval
    selected = approval.selection
    user = connection.execute(
        "SELECT role, disabled, session_version FROM users WHERE id = ?",
        (str(approval.administrator_id),),
    ).fetchone()
    if user is None or tuple(user) != ("ADMIN", 0, approval.session_version):
        raise ContractError("Administrator authority changed")
    fresh = selection_for(
        connection,
        selected.app_id,
        selected.manifest_sha256,
        selected.preview_id,
        policy_sha256,
        operation.kind,
    )
    if fresh != selected:
        raise ContractError("Approval authority changed")
    if operation.kind == "recover":
        blocked = blocked_operation(connection, selected.app_id)
        if blocked is None or blocked.operation_id != operation.recovers:
            raise ContractError("Recovery target changed")
    if operation.kind == "deploy":
        rehearsal = connection.execute(
            (
                "SELECT receipt_json FROM app_operations WHERE operation_id ="
                " ? AND kind = 'rehearse' AND state = 'succeeded'"
            ),
            (str(approval.rehearsal_id),),
        ).fetchone()
        if (
            rehearsal is None
            or parse_document(Receipt, rehearsal[0].encode()).selection != selected
        ):
            raise ContractError("Compatibility rehearsal changed")
    previous = accepted(connection, selected.app_id, selected.environment)
    if operation.kind == "rollback":
        target = installed(connection, selected.app_id, selected.environment, 1)
        if (
            target is None
            or target.selection.manifest_sha256 != selected.manifest_sha256
        ):
            raise ContractError("Rollback target changed")
    registration = approved_registration(connection, selected.app_id, operation.kind)
    release = eligible(
        connection,
        release_registration(connection, selected.app_id, operation.kind),
        selected.manifest_sha256,
    )
    credential = connection.execute(
        (
            "SELECT file_path, verifier FROM app_credentials WHERE "
            "credential_id = ? AND revoked_at IS NULL"
        ),
        (selected.credential_id,),
    ).fetchone()
    instance = connection.execute(
        "SELECT app_api_url, timezone FROM platform_instance"
    ).fetchone()
    return Authority(
        operation=operation,
        registration=registration,
        credential_path=credential[0],
        credential_verifier=credential[1],
        archive_base64=base64.b64encode(release.archive).decode(),
        artifact_id=release.artifact_id,
        previous=previous,
        app_api_url=instance[0],
        timezone=instance[1],
    )


def operation_state(connection: sqlite3.Connection, operation_id: UUID) -> str:
    (state,) = connection.execute(
        "SELECT state FROM app_operations WHERE operation_id = ?",
        (str(operation_id),),
    ).fetchone()
    return str(state)


def retire_blocker(connection: sqlite3.Connection, app_id: str) -> str | None:
    """Why the app cannot retire now, or None."""
    (active,) = connection.execute(
        "SELECT count(*) FROM app_runs AS run WHERE app_id = ? AND EXISTS "
        "(SELECT 1 FROM jobs WHERE group_id = run.group_id "
        "AND status IN ('QUEUED', 'RUNNING'))",
        (app_id,),
    ).fetchone()
    if active:
        plural = "" if active == 1 else "s"
        return f"{active} compute run{plural} still queued or running"
    return None


def blocked_operation(connection: sqlite3.Connection, app_id: str) -> Operation | None:
    row = connection.execute(
        "SELECT operation_json FROM app_operations WHERE app_id = ? "
        "AND state = 'blocked'",
        (app_id,),
    ).fetchone()
    return parse_document(Operation, row[0].encode()) if row else None


def snapshot_of(operation: Operation, receipt: Receipt) -> InstalledSnapshot:
    snapshot = receipt.installed
    if (
        snapshot is None
        or snapshot.operation_id != (operation.recovers or operation.operation_id)
        or snapshot.selection != receipt.selection
        or snapshot.generation != receipt.selection.generation + 1
        or fingerprint(snapshot.registration) != receipt.selection.registration_sha256
        or receipt.rehearsal is not None
    ):
        raise ContractError("Installer snapshot is incomplete")
    return snapshot


def retired_of(
    connection: sqlite3.Connection, operation: Operation, receipt: Receipt
) -> RetiredSnapshot:
    """Root's retirement, accepted only if it retires the row core holds current."""
    retired = receipt.retired
    selected = receipt.selection
    if (
        retired is None
        or receipt.rehearsal is not None
        or retired.operation_id != operation.operation_id
        or retired.selection != selected
        or retired.generation != selected.generation + 1
        or retired.last_installed
        != installed(connection, selected.app_id, selected.environment)
        or fingerprint(retired.registration) != selected.registration_sha256
    ):
        raise ContractError("Installer retirement is incomplete")
    return retired


def accepted_row(
    connection: sqlite3.Connection, operation: Operation, receipt: Receipt
) -> AcceptedSnapshot | None:
    """What a succeeded receipt appends to the installed history, if anything."""
    if operation.kind == "rehearse" or (
        operation.kind == "recover" and receipt.installed is None
    ):
        return None
    if operation.kind == "retire":
        return retired_of(connection, operation, receipt)
    return snapshot_of(operation, receipt)


class ReconciliationPending(ContractError):
    pass


class ApplicationRetired(ReconciliationPending):
    """A retired app has no boot authority.

    Shares the deferred exit status so a unit that outlived the retirement
    stops as a success and stays stopped, instead of restarting every 5 s.
    """


def read_installed_authority(connection: sqlite3.Connection, app_id: str) -> Authority:
    if connection.execute(
        "SELECT 1 FROM app_operations WHERE app_id = ? "
        "AND state IN ('switching', 'blocked')",
        (app_id,),
    ).fetchone():
        raise ReconciliationPending("Installation requires reconciliation")
    instance = connection.execute("SELECT * FROM platform_instance").fetchone()
    current = running_of(accepted(connection, app_id, instance["environment"]))
    if current is None:
        if retirement(connection, app_id, instance["environment"]) is not None:
            raise ApplicationRetired("Application is retired")
        raise ContractError("Application has no accepted installation")
    registration = registration_for(connection, app_id)
    if registration != current.registration:
        raise ContractError("Installed registration changed")
    row = connection.execute(
        "SELECT operation_json FROM app_operations WHERE operation_id = ? "
        "AND state = 'succeeded'",
        (str(current.operation_id),),
    ).fetchone()
    credential = connection.execute(
        "SELECT * FROM app_credentials WHERE credential_id = ? AND revoked_at IS NULL",
        (current.selection.credential_id,),
    ).fetchone()
    if (
        row is None
        or credential is None
        or credential["environment"] != current.selection.environment
    ):
        raise ContractError("Installed credential is no longer active")
    release = eligible(connection, registration, current.selection.manifest_sha256)
    return Authority(
        operation=parse_document(Operation, row[0].encode()),
        registration=registration,
        credential_path=credential["file_path"],
        credential_verifier=credential["verifier"],
        archive_base64=base64.b64encode(release.archive).decode(),
        artifact_id=release.artifact_id,
        previous=current,
        app_api_url=instance["app_api_url"],
        timezone=instance["timezone"],
    )


class AppsService:
    def __init__(
        self,
        database: Database,
        spool: Path,
        policy_fingerprint: Callable[[str], str],
        *,
        receipt_uid: int = 0,
        policy_registration: Callable[[str], Registration | None] = lambda _: None,
    ) -> None:
        self.database = database
        self.spool = spool
        self.policy_fingerprint = policy_fingerprint
        self.receipt_uid = receipt_uid
        self.policy_registration = policy_registration

    def request(
        self,
        kind: Kind,
        app_id: str,
        digest: str,
        preview_id: UUID | None,
        administrator_id: UUID,
        session_version: int,
        request_id: UUID,
        rehearsal_id: UUID | None = None,
        registration_sha256: str | None = None,
    ) -> Operation:
        """Record an approved operation, idempotently per ``request_id``.

        A rehearsal or deploy names the settings change the administrator saw
        in ``registration_sha256``, or None when the card showed no change, so
        a change replaced after the page loaded is never approved unseen.
        """
        if kind not in KINDS:
            raise ContractError("Unknown operation")
        if kind == "retire" and kind not in self.installer_kinds():
            raise ContractError(RUNTIME_LACKS_RETIRE)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT operation_json FROM app_operations WHERE request_id = ?",
                (str(request_id),),
            ).fetchone()
            if prior:
                operation = parse_document(Operation, prior[0].encode())
                if (
                    operation.kind != kind
                    or operation.approval.selection.app_id != app_id
                    or operation.approval.selection.manifest_sha256 != digest
                    or operation.approval.administrator_id != administrator_id
                    or operation.approval.session_version != session_version
                    or (
                        kind != "recover"
                        and operation.approval.selection.preview_id != preview_id
                    )
                    or operation.approval.rehearsal_id != rehearsal_id
                    or operation.approval.selection.policy_sha256
                    != self.policy_fingerprint(app_id)
                    or (
                        kind in {"rehearse", "deploy"}
                        and shown_change(connection, operation.approval.selection)
                        != registration_sha256
                    )
                ):
                    raise ContractError("Request ID was already used")
                if (
                    connection.execute(
                        "SELECT 1 FROM users WHERE id = ? AND role = 'ADMIN' "
                        "AND disabled = 0 AND session_version = ?",
                        (str(administrator_id), session_version),
                    ).fetchone()
                    is None
                ):
                    raise ContractError("Administrator authority changed")
                return operation
            recovers = None
            if kind == "recover":
                blocked = blocked_operation(connection, app_id)
                if (
                    blocked is None
                    or blocked.approval.selection.manifest_sha256 != digest
                ):
                    raise ContractError("No blocked operation matches the selection")
                recovers = blocked.operation_id
                preview_id = blocked.approval.selection.preview_id
            if preview_id is None:
                raise ContractError("A selected preview is required")
            if kind == "retire" and (blocker := retire_blocker(connection, app_id)):
                raise ContractError(blocker)
            selected = selection_for(
                connection,
                app_id,
                digest,
                preview_id,
                self.policy_fingerprint(app_id),
                kind,
            )
            if kind in {"rehearse", "deploy"} and (
                shown_change(connection, selected) != registration_sha256
            ):
                raise ContractError("The settings change was replaced")
            operation = Operation(
                operation_id=uuid4(),
                request_id=request_id,
                kind=kind,
                approval=Approval(
                    selection=selected,
                    administrator_id=administrator_id,
                    session_version=session_version,
                    rehearsal_id=rehearsal_id,
                ),
                recovers=recovers,
            )
            require_idle(connection, app_id, recovering=kind == "recover")
            connection.execute(
                (
                    "INSERT INTO "
                    "app_operations(operation_id,request_id,app_id,environment,kind,state,operation_json)"
                    " VALUES (?,?,?,?,?,'queued',?)"
                ),
                (
                    str(operation.operation_id),
                    str(request_id),
                    app_id,
                    selected.environment,
                    kind,
                    operation.model_dump_json(),
                ),
            )
            read_authority(
                connection, operation.operation_id, self.policy_fingerprint(app_id)
            )
        self.reconcile()
        return operation

    def installer_kinds(self) -> frozenset[str]:
        """The operation kinds the host installer runtime last advertised."""
        try:
            payload = read_owned(
                self.spool / "receipts" / "installer.json", self.receipt_uid
            )
            return frozenset(parse_document(InstallerCapabilities, payload).kinds)
        except (OSError, ContractError):
            return frozenset()

    def reject(
        self,
        app_id: str,
        digest: str,
        administrator_id: UUID,
        session_version: int,
        registration_sha256: str | None = None,
    ) -> None:
        """Reject what the card offered: the release, its settings change, or both.

        ``registration_sha256`` names the settings change the administrator saw,
        so a change proposed after the page loaded is never rejected unseen.
        """
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if (
                connection.execute(
                    "SELECT 1 FROM users WHERE id = ? AND role = 'ADMIN' "
                    "AND disabled = 0 AND session_version = ?",
                    (str(administrator_id), session_version),
                ).fetchone()
                is None
            ):
                raise ContractError("Administrator authority changed")
            eligible(connection, proposed_registration(connection, app_id), digest)
            require_idle(connection, app_id)
            environment = environment_of(connection)
            current = (
                installed(connection, app_id, environment) if environment else None
            )
            change = (
                pending_change(connection, app_id, environment)
                if current and environment
                else None
            )
            if (fingerprint(change.registration) if change else None) != (
                registration_sha256
            ):
                raise ContractError("The settings change was replaced")
            if change is not None:
                connection.execute(
                    "INSERT INTO app_registration_rejections"
                    "(proposal_id, administrator_id) VALUES (?,?)",
                    (change.proposal_id, str(administrator_id)),
                )
            if current is not None and current.selection.manifest_sha256 == digest:
                if change is None:
                    raise ContractError("The installed release cannot be rejected")
                return
            connection.execute(
                "INSERT OR IGNORE INTO app_rejections VALUES (?,?,?)",
                (app_id, digest, str(administrator_id)),
            )

    def reconcile(self) -> None:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT operation_id, operation_json FROM app_operations "
                "WHERE state IN ('queued', 'switching')"
            ).fetchall()
        for operation_id, payload in rows:
            try:
                self.reconcile_operation(parse_document(Operation, payload.encode()))
            except Exception as error:
                logger.warning(
                    "Operation %s is not reconciled: %s",
                    operation_id,
                    error,
                    exc_info=not isinstance(error, (ContractError, OSError)),
                )
                self.note_reconcile(operation_id, redacted_error(error))
            else:
                self.note_reconcile(operation_id, None)

    def note_reconcile(self, operation_id: str, error: str | None) -> None:
        with self.database.connect() as connection:
            if error is None:
                connection.execute(
                    "UPDATE app_operations SET reconcile_error = NULL, "
                    "reconcile_failed_at = NULL WHERE operation_id = ? "
                    "AND reconcile_error IS NOT NULL",
                    (operation_id,),
                )
            else:
                connection.execute(
                    "UPDATE app_operations SET reconcile_error = ?, "
                    "reconcile_failed_at = CURRENT_TIMESTAMP WHERE operation_id = ?",
                    (error, operation_id),
                )

    def reconcile_operation(self, operation: Operation) -> None:
        name = str(operation.operation_id) + ".json"
        switch_path = (
            self.spool / "receipts" / (str(operation.operation_id) + ".switch.json")
        )
        try:
            switch = parse_document(
                OperationHint, read_owned(switch_path, self.receipt_uid)
            )
        except FileNotFoundError:
            pass
        else:
            if (
                switch.operation_id != operation.operation_id
                or operation.kind == "rehearse"
            ):
                raise ContractError("Switch intent does not match operation")
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE app_operations SET state = 'switching' "
                    "WHERE operation_id = ? AND state = 'queued'",
                    (str(operation.operation_id),),
                )
        try:
            receipt = parse_document(
                Receipt,
                read_owned(self.spool / "receipts" / name, self.receipt_uid),
            )
        except FileNotFoundError:
            publish(
                self.spool / "requests" / name,
                OperationHint(operation_id=operation.operation_id)
                .model_dump_json()
                .encode(),
            )
            return
        if (
            receipt.operation_id != operation.operation_id
            or receipt.selection != operation.approval.selection
        ):
            raise ContractError("Installer receipt does not match operation")
        if receipt.state == "succeeded":
            with self.database.connect() as connection:
                # One read snapshot, so a reconciler that lost the race sees the
                # operation settled rather than checking the receipt against
                # the history its winner already appended.
                connection.execute("BEGIN")
                if operation_state(connection, operation.operation_id) not in {
                    "queued",
                    "switching",
                }:
                    return
                if operation.kind != "rehearse" and not receipt.authorization_committed:
                    raise ContractError("Installer authorization is incomplete")
                release = eligible(
                    connection,
                    receipt.installed.registration
                    if receipt.installed
                    else release_registration(
                        connection, receipt.selection.app_id, operation.kind
                    ),
                    receipt.selection.manifest_sha256,
                )
                appended = accepted_row(connection, operation, receipt)
            if operation.kind == "rehearse":
                rehearsal = receipt.rehearsal
                if (
                    rehearsal is None
                    or rehearsal.selection != receipt.selection
                    or not rehearsal.passed()
                    or receipt.installed is not None
                ):
                    raise ContractError("Installer rehearsal is incomplete")
            elif (
                isinstance(appended, InstalledSnapshot)
                and appended.image != release.manifest.image
            ):
                raise ContractError("Installer snapshot is incomplete")
        if (
            receipt.state == "succeeded"
            and operation.kind != "rehearse"
            and receipt.installed is not None
        ):
            with self.database.connect() as connection:
                release = eligible(
                    connection,
                    receipt.installed.registration,
                    receipt.selection.manifest_sha256,
                )
                # app_releases_no_replace aborts any insert of a known release,
                # INSERT OR IGNORE included, so a rollback or redeploy has to skip
                # the row rather than rely on conflict resolution.
                connection.execute(
                    "INSERT INTO app_releases (app_id,manifest_sha256,manifest_bytes) "
                    "SELECT ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM app_releases "
                    "WHERE app_id = ? AND manifest_sha256 = ?)",
                    (
                        receipt.selection.app_id,
                        receipt.selection.manifest_sha256,
                        release.files["manifest.json"],
                        receipt.selection.app_id,
                        receipt.selection.manifest_sha256,
                    ),
                )
            with tempfile.TemporaryDirectory() as temporary:
                for workload in release.manifest.workloads:
                    archive = Path(temporary) / workload.asset
                    archive.write_bytes(release.files[workload.asset])
                    WorkloadStore(self.database).install(
                        receipt.selection.app_id,
                        receipt.selection.manifest_sha256,
                        workload.id,
                        archive,
                    )
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = operation_state(connection, operation.operation_id)
            if state not in {"queued", "switching"}:
                return
            if (
                receipt.state == "succeeded"
                and operation.kind != "rehearse"
                and state != "switching"
            ):
                raise ContractError("Installation was not admitted for switching")
            if receipt.state == "succeeded" and operation.kind == "rehearse":
                try:
                    read_authority(
                        connection,
                        operation.operation_id,
                        self.policy_fingerprint(operation.approval.selection.app_id),
                    )
                except ContractError:
                    receipt = Receipt(
                        operation_id=operation.operation_id,
                        selection=receipt.selection,
                        state="failed",
                        error="authority_changed",
                    )
            appended = (
                accepted_row(connection, operation, receipt)
                if receipt.state == "succeeded"
                else None
            )
            if appended is not None:
                connection.execute(
                    "INSERT INTO app_installed_history(operation_id, app_id, "
                    "environment, generation, snapshot_json) VALUES (?,?,?,?,?)",
                    (
                        str(appended.operation_id),
                        receipt.selection.app_id,
                        receipt.selection.environment,
                        appended.generation,
                        appended.model_dump_json(),
                    ),
                )
                # The registration row is the installed boot authority, so it
                # follows each accepted snapshot in the same transaction.
                if isinstance(appended, InstalledSnapshot) and registered(
                    connection, appended.registration.app_id
                ) != (appended.registration):
                    replace_registration(connection, appended.registration)
            if operation.kind == "recover":
                blocked = blocked_operation(connection, receipt.selection.app_id)
                if blocked is None or blocked.operation_id != operation.recovers:
                    raise ContractError("Recovery target changed")
                if receipt.state == "succeeded":
                    resolved = (
                        "succeeded"
                        if receipt.installed is not None
                        else "recovered"
                        if installed(
                            connection,
                            receipt.selection.app_id,
                            receipt.selection.environment,
                        )
                        else "failed"
                    )
                    connection.execute(
                        "UPDATE app_operations SET state = ? WHERE operation_id = ?",
                        (resolved, str(blocked.operation_id)),
                    )
            if (
                receipt.state == "succeeded"
                and operation.kind == "rehearse"
                and (
                    receipt.rehearsal is None
                    or receipt.rehearsal.selection != receipt.selection
                    or not receipt.rehearsal.passed()
                )
            ):
                raise ContractError("Installer rehearsal is incomplete")
            connection.execute(
                (
                    "UPDATE app_operations SET state = ?, receipt_json = ? WHERE "
                    "operation_id = ?"
                ),
                (
                    receipt.state,
                    receipt.model_dump_json(),
                    str(operation.operation_id),
                ),
            )
        (self.spool / "requests" / name).unlink(missing_ok=True)


def redacted_error(error: Exception) -> str:
    if isinstance(error, ContractError):
        return str(error)
    return "Application records are unavailable"


def installed_identity(registration: Registration) -> object:
    return (
        registration.model_dump(exclude={"presentation", "bindings"}),
        registration.bindings.state_directory,
    )


def update_registration(
    database: Database, payload: bytes
) -> Literal["updated", "proposed", "withdrawn", "unchanged"]:
    """Replace an uninstalled registration, or propose a change to an installed one.

    The installed registration is the root helper's boot authority, so a change
    to an installed app waits as a proposal until a deploy carrying it is
    accepted. A retired app keeps the registration its data was archived
    under, which a restore binds, so it refuses any change.
    """
    proposed = parse_document(Registration, payload)
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        old = registration_for(connection, proposed.app_id)
        if proposed.route != old.route or proposed.installation != old.installation:
            raise ContractError(
                "Route and installation changes require host reprovisioning"
            )
        environment = environment_of(connection)
        if environment and retirement(connection, old.app_id, environment):
            raise ContractError("Restore the app before changing its registration")
        values = {item.name: item for item in old.bindings.settings}
        values.update({item.name: item for item in proposed.bindings.settings})
        merged = proposed.model_copy(
            update={
                "bindings": proposed.bindings.model_copy(
                    update={"settings": tuple(values[key] for key in sorted(values))}
                )
            }
        )
        current = (
            installed(connection, old.app_id, environment) if environment else None
        )
        if environment is None or current is None:
            replace_registration(connection, merged)
            return "updated"
        if installed_identity(merged) != installed_identity(current.registration):
            raise ContractError(
                "An installed application can change only its operator bindings "
                "and presentation"
            )
        require_idle(connection, old.app_id)
        eligible(connection, merged, current.selection.manifest_sha256)
        change = pending_change(connection, old.app_id, environment)
        if merged == (change.registration if change else current.registration):
            return "unchanged"
        connection.execute(
            "INSERT INTO app_registration_proposals(app_id, environment, "
            "base_generation, definition_json) VALUES (?,?,?,?)",
            (old.app_id, environment, current.generation, definition_of(merged)),
        )
        return "proposed" if merged != current.registration else "withdrawn"


def definition_of(registration: Registration) -> str:
    return json.dumps(
        registration.model_dump(mode="json", exclude={"app_id", "route"}),
        sort_keys=True,
        separators=(",", ":"),
    )


def replace_registration(
    connection: sqlite3.Connection, registration: Registration
) -> None:
    definition = definition_of(registration)
    connection.execute(
        "UPDATE app_registrations SET definition_json = ? WHERE app_id = ?",
        (definition, registration.app_id),
    )
    connection.execute(
        "INSERT INTO app_registration_changes(app_id, definition_json) VALUES (?,?)",
        (registration.app_id, definition),
    )


Outcome = Literal["updated", "unchanged"]


def require_unaccepted(connection: sqlite3.Connection, app_id: str) -> str:
    environment = environment_of(connection)
    if environment is None:
        raise ContractError("Application instance is not configured")
    require_idle(connection, app_id)
    if accepted(connection, app_id, environment) is not None:
        raise ContractError("Accepted installation history refuses a legacy cutover")
    return environment


def require_cutover_pair(legacy_side: Registration, managed_side: Registration) -> None:
    if not legacy(legacy_side):
        raise ContractError("A legacy-scripts definition is required")
    if not managed(managed_side):
        raise ContractError("A published managed definition is required")
    if legacy_side.route != managed_side.route:
        raise ContractError("Legacy cutover cannot change the route")
    if legacy_side.access != managed_side.access:
        raise ContractError("Legacy cutover cannot change access")
    if legacy_side.bindings.state_directory not in (
        None,
        managed_side.bindings.state_directory,
    ):
        raise ContractError("Legacy cutover cannot change the state directory")


def provision_legacy(
    database: Database, proposed: Registration, *, expected_legacy_sha256: Sha256
) -> Outcome:
    if not managed(proposed):
        raise ContractError("A published managed definition is required")
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        require_unaccepted(connection, proposed.app_id)
        current = registered(connection, proposed.app_id)
        if current == proposed:
            return "unchanged"
        require_cutover_pair(current, proposed)
        if fingerprint(current) != expected_legacy_sha256:
            raise ContractError("Legacy registration fingerprint does not match")
        replace_registration(connection, proposed)
    return "updated"


def restore_unaccepted_legacy(
    database: Database,
    legacy_registration: Registration,
    *,
    expected_legacy_sha256: Sha256,
    expected_managed_sha256: Sha256,
) -> Outcome:
    if not legacy(legacy_registration):
        raise ContractError("A legacy-scripts definition is required")
    if fingerprint(legacy_registration) != expected_legacy_sha256:
        raise ContractError("Legacy registration fingerprint does not match")
    app_id = legacy_registration.app_id
    outcome: Outcome = "unchanged"
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        environment = require_unaccepted(connection, app_id)
        current = registered(connection, app_id)
        if current != legacy_registration:
            require_cutover_pair(legacy_registration, current)
            if fingerprint(current) != expected_managed_sha256:
                raise ContractError("Managed registration fingerprint does not match")
            replace_registration(connection, legacy_registration)
            outcome = "updated"
        connection.execute(
            "UPDATE app_credentials SET revoked_at = CURRENT_TIMESTAMP "
            "WHERE app_id = ? AND environment = ? AND revoked_at IS NULL",
            (app_id, environment),
        )
    return outcome

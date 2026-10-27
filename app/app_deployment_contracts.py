import hashlib
import json
from collections.abc import Mapping
from typing import Annotated, Literal, get_args
from uuid import UUID

from pydantic import Field, StringConstraints, TypeAdapter, model_validator

from app.app_contracts import (
    Contract,
    Identifier,
    ImageId,
    ImageReference,
    Registration,
    Sha256,
    parse_document,
)

CredentialId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
REHEARSAL_CHECKS = frozenset(
    {"startup", "previous_reads_current", "semantic_records", "restore"}
)
Kind = Literal["rehearse", "deploy", "rollback", "recover", "retire"]
KINDS: frozenset[str] = frozenset(get_args(Kind))
SWITCHING_KINDS: frozenset[str] = KINDS - {"rehearse"}


def fingerprint(value: Contract | Mapping[str, object]) -> str:
    payload = json.dumps(
        value.model_dump(mode="json") if isinstance(value, Contract) else value,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class Selection(Contract):
    app_id: Identifier
    environment: Identifier
    manifest_sha256: Sha256
    archive_sha256: Sha256
    registration_sha256: Sha256
    policy_sha256: Sha256
    credential_id: CredentialId
    generation: Annotated[int, Field(ge=0)]
    preview_id: UUID


class Approval(Contract):
    selection: Selection
    administrator_id: UUID
    session_version: Annotated[int, Field(ge=1)]
    rehearsal_id: UUID | None


class Operation(Contract):
    operation_id: UUID
    request_id: UUID
    kind: Kind
    approval: Approval
    recovers: UUID | None = None

    @model_validator(mode="after")
    def recovery_names_blocked_operation(self) -> "Operation":
        if (self.recovers is None) == (self.kind == "recover"):
            raise ValueError("Only a recovery names the blocked operation")
        return self


def rehearsal_checks(previous_image_id: str | None) -> dict[str, bool | None]:
    """Every check passed; reading with the previous image needs a previous image."""
    return {
        name: None
        if name == "previous_reads_current" and previous_image_id is None
        else True
        for name in REHEARSAL_CHECKS
    }


class Rehearsal(Contract):
    selection: Selection
    image_id: ImageId
    previous_image_id: ImageId | None
    backup_sha256: Sha256 | None
    checks: dict[str, bool | None]

    def passed(self) -> bool:
        return self.checks == rehearsal_checks(self.previous_image_id)


class InstalledSnapshot(Contract):
    kind: Literal["installed"] = "installed"
    operation_id: UUID
    generation: Annotated[int, Field(ge=1)]
    selection: Selection
    registration: Registration
    image: ImageReference
    image_id: ImageId


ArchiveFileName = Annotated[
    str, StringConstraints(pattern=r"^retired-g[1-9][0-9]*-[0-9a-f-]{36}\.sqlite$")
]


class ArchiveRecord(Contract):
    """The final verified SQLite copy root published beside the daily backups."""

    file_name: ArchiveFileName
    sha256: Sha256
    content_sha256: Sha256
    tables: tuple[str, ...]
    size_bytes: Annotated[int, Field(ge=1)]


class RetiredSnapshot(Contract):
    kind: Literal["retired"] = "retired"
    operation_id: UUID
    generation: Annotated[int, Field(ge=2)]
    selection: Selection
    registration: Registration
    last_installed: InstalledSnapshot
    archive: ArchiveRecord | None

    @model_validator(mode="after")
    def retires_the_previous_generation(self) -> "RetiredSnapshot":
        if self.last_installed.generation != self.generation - 1:
            raise ValueError("A retirement follows the generation it retires")
        if self.last_installed.selection.app_id != self.selection.app_id:
            raise ValueError("A retirement names its own application")
        return self


# Rows and journal files written before retirement existed carry no kind, so
# the union is tried in order: only a retired payload fails InstalledSnapshot.
AcceptedSnapshot = InstalledSnapshot | RetiredSnapshot
ACCEPTED_SNAPSHOT: TypeAdapter[AcceptedSnapshot] = TypeAdapter(AcceptedSnapshot)


def decode_accepted(payload: bytes) -> AcceptedSnapshot:
    return parse_document(ACCEPTED_SNAPSHOT, payload)


def running_of(accepted: AcceptedSnapshot | None) -> InstalledSnapshot | None:
    return accepted if isinstance(accepted, InstalledSnapshot) else None


ReceiptError = Literal[
    "authority_changed",
    "policy_refused",
    "compatibility_failed",
    "records_changed",
    "installation_failed",
    "recovery_required",
    "backup_failed",
    "state_missing",
    "state_changed",
]


class Receipt(Contract):
    operation_id: UUID
    selection: Selection
    state: Literal["succeeded", "failed", "recovered", "blocked"]
    rehearsal: Rehearsal | None = None
    installed: InstalledSnapshot | None = None
    retired: RetiredSnapshot | None = None
    authorization_committed: bool = False
    error: ReceiptError | None = None

    @model_validator(mode="after")
    def one_outcome(self) -> "Receipt":
        outcomes = (self.rehearsal, self.installed, self.retired)
        if sum(outcome is not None for outcome in outcomes) > 1:
            raise ValueError("A receipt carries one outcome")
        return self


class OperationHint(Contract):
    schema_version: Literal[1] = 1
    operation_id: UUID


class InstallerCapabilities(Contract):
    """What the host installer runtime advertises after each pass.

    Kinds are plain strings so a core older than the runtime still parses the
    list; core refuses a kind the file does not name.
    """

    schema_version: Literal[1] = 1
    kinds: tuple[str, ...]

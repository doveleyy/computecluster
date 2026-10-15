import hashlib
import json
from collections.abc import Mapping
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, model_validator

from app.app_contracts import (
    Contract,
    Identifier,
    ImageId,
    ImageReference,
    Registration,
    Sha256,
)

CredentialId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
REHEARSAL_CHECKS = frozenset(
    {"startup", "previous_reads_current", "semantic_records", "restore"}
)


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
    kind: Literal["rehearse", "deploy", "rollback", "recover"]
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
    operation_id: UUID
    generation: Annotated[int, Field(ge=1)]
    selection: Selection
    registration: Registration
    image: ImageReference
    image_id: ImageId


class Receipt(Contract):
    operation_id: UUID
    selection: Selection
    state: Literal["succeeded", "failed", "recovered", "blocked"]
    rehearsal: Rehearsal | None = None
    installed: InstalledSnapshot | None = None
    authorization_committed: bool = False
    error: (
        Literal[
            "authority_changed",
            "policy_refused",
            "compatibility_failed",
            "records_changed",
            "installation_failed",
            "recovery_required",
        ]
        | None
    ) = None


class OperationHint(Contract):
    schema_version: Literal[1] = 1
    operation_id: UUID

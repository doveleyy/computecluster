import json
import os
import re
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import test_app_discovery
from pydantic import ValidationError
from test_app_discovery import package
from test_app_platform import managed
from test_app_registry import document

from app.accounts import AccountStore
from app.app_contracts import ContractError
from app.app_deployment_contracts import (
    Receipt,
    Rehearsal,
    Selection,
    fingerprint,
    rehearsal_checks,
)
from app.app_deployments import AppsService, pending_change, read_authority
from app.app_previews import CHECKS, PreviewResult
from app.app_publication import sha256
from app.app_registry import AppRegistry
from app.app_spool import publish
from app.config import InstanceFacts
from app.database import Database
from app.identity import ADMIN_USER_ID


@pytest.fixture
def ledger(tmp_path):
    database = Database(tmp_path / "core.sqlite")
    database.initialize()
    registry = AppRegistry(database)
    registry.publish_instance(
        InstanceFacts("test", "http://127.0.0.1:1/api/apps/v1", "UTC")
    )
    registry.register(document(managed(state=str(tmp_path / "state"))))
    registry.issue_credential("notes", tmp_path / "credentials" / "notes")
    _, archive, proof = package()
    digest = proof["manifest_sha256"]
    preview = PreviewResult(
        attempt_id=uuid4(),
        app_id="notes",
        manifest_sha256=digest,
        bundle_sha256=sha256(archive),
        state="ready",
        image_id="sha256:" + "b" * 64,
        preview_url="https://preview.example/notes/",
        checks=dict.fromkeys(CHECKS, True),
    )
    with database.connect() as connection:
        connection.execute(
            (
                "INSERT INTO "
                "app_eligible_releases(app_id,manifest_sha256,release_id,artifact_id,artifact_sha256,archive)"
                " VALUES (?,?,?,?,?,?)"
            ),
            ("notes", digest, 7, 42, sha256(archive), archive),
        )
        connection.execute(
            "INSERT INTO app_preview_attempts VALUES (?,?,?,?,?,?)",
            (
                str(preview.attempt_id),
                "notes",
                digest,
                sha256(archive),
                "ready",
                preview.model_dump_json(),
            ),
        )
    spool = tmp_path / "spool"
    (spool / "requests").mkdir(parents=True)
    (spool / "receipts").mkdir()
    service = AppsService(database, spool, lambda _: "a" * 64, receipt_uid=os.geteuid())
    yield SimpleNamespace(
        database=database,
        registry=registry,
        service=service,
        digest=digest,
        preview=preview,
        spool=spool,
    )
    database.close()


def request(ledger, kind="rehearse", **changes):
    """Request ``kind`` as the card would, naming the change it shows."""
    with ledger.database.connect() as connection:
        change = pending_change(connection, "notes", "test")
    values = dict(
        kind=kind,
        app_id="notes",
        digest=ledger.digest,
        preview_id=ledger.preview.attempt_id,
        administrator_id=UUID(ADMIN_USER_ID),
        session_version=1,
        request_id=uuid4(),
        registration_sha256=fingerprint(change.registration)
        if change and kind in {"rehearse", "deploy"}
        else None,
    )
    values.update(changes)
    return ledger.service.request(**values)


def push(ledger, monkeypatch, facts=(None, None), **changes):
    original = test_app_discovery.manifest

    def newer():
        value = original()
        value.update(changes)
        return value

    monkeypatch.setattr(test_app_discovery, "manifest", newer)
    _, archive, proof = package()
    monkeypatch.setattr(test_app_discovery, "manifest", original)
    with ledger.database.connect() as connection:
        connection.execute(
            (
                "INSERT INTO "
                "app_eligible_releases(app_id,manifest_sha256,release_id,artifact_id,"
                "artifact_sha256,archive,commit_title,committed_at)"
                " VALUES (?,?,?,?,?,?,?,?)"
            ),
            (
                "notes",
                proof["manifest_sha256"],
                8,
                43,
                sha256(archive),
                archive,
                *facts,
            ),
        )
    return proof["manifest_sha256"]


def complete_rehearsal(ledger, operation, checks=None):
    selection = operation.approval.selection
    receipt = Receipt(
        operation_id=operation.operation_id,
        selection=selection,
        state="succeeded",
        rehearsal=Rehearsal(
            selection=selection,
            image_id="sha256:" + "b" * 64,
            previous_image_id=None,
            backup_sha256=None,
            checks=checks or rehearsal_checks(None),
        ),
    )
    publish(
        ledger.spool / "receipts" / f"{operation.operation_id}.json",
        receipt.model_dump_json().encode(),
    )
    ledger.service.reconcile()


def test_durable_request_retries_and_changed_payload_refusal(ledger):
    operation = request(ledger)
    assert request(ledger, request_id=operation.request_id) == operation
    for changes in (
        {"preview_id": uuid4()},
        {"session_version": 2},
        {"rehearsal_id": uuid4()},
        {"digest": "0" * 64},
    ):
        with pytest.raises(ContractError):
            request(ledger, request_id=operation.request_id, **changes)
    with pytest.raises(ContractError, match="pending"):
        request(ledger)
    hint = ledger.spool / "requests" / f"{operation.operation_id}.json"
    hint.unlink()
    ledger.service.reconcile()
    assert hint.exists()


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE users SET disabled = 1 WHERE role = 'ADMIN'",
        "UPDATE users SET session_version = session_version + 1 WHERE role = 'ADMIN'",
        "UPDATE users SET role = 'MEMBER' WHERE role = 'ADMIN'",
        "UPDATE app_credentials SET revoked_at = CURRENT_TIMESTAMP",
        "UPDATE app_preview_attempts SET state = 'retired'",
        (
            "UPDATE app_registrations SET definition_json = "
            "replace(definition_json, 'dark', 'light') WHERE app_id = "
            "'notes'"
        ),
    ],
)
def test_queued_operation_revalidates_current_authority(ledger, change):
    operation = request(ledger)
    with ledger.database.connect() as connection:
        connection.execute(change)
        with pytest.raises(ContractError):
            read_authority(connection, operation.operation_id, "a" * 64)


def test_policy_and_selected_digest_cannot_follow_new_publication(ledger):
    operation = request(ledger)
    with ledger.database.connect() as connection:
        with pytest.raises(ContractError):
            read_authority(connection, operation.operation_id, "b" * 64)
        connection.execute(
            "INSERT INTO app_discovery(app_id,latest_sha256) VALUES ('notes',?)",
            ("f" * 64,),
        )
        assert (
            read_authority(connection, operation.operation_id, "a" * 64).operation
            == operation
        )


def test_deploy_requires_exact_successful_rehearsal(ledger):
    with pytest.raises(ContractError, match="rehearsal"):
        request(ledger, "deploy")
    operation = request(ledger)
    complete_rehearsal(ledger, operation)
    deploy = request(ledger, "deploy", rehearsal_id=operation.operation_id)
    assert deploy.approval.selection == operation.approval.selection


def test_partial_rehearsal_does_not_authorize_deployment(ledger):
    operation = request(ledger)
    complete_rehearsal(ledger, operation, {"startup": True})
    with ledger.database.connect() as connection:
        assert (
            connection.execute("SELECT state FROM app_operations").fetchone()[0]
            == "queued"
        )
    with pytest.raises(ContractError, match="pending"):
        request(ledger, "deploy", rehearsal_id=operation.operation_id)


def test_rejected_candidate_and_non_admin_rejection_fail_closed(ledger):
    with pytest.raises(ContractError, match="authority"):
        ledger.service.reject("notes", ledger.digest, uuid4(), 1)
    ledger.service.reject("notes", ledger.digest, UUID(ADMIN_USER_ID), 1)
    with pytest.raises(ContractError, match="rejected"):
        request(ledger)


def test_selection_credential_id_is_typed_at_the_contract_boundary(ledger):
    selection = request(ledger).approval.selection
    assert re.fullmatch(r"[0-9a-f]{32}", selection.credential_id)
    for forged in ("../../etc/passwd", "notes-" + "a" * 26, "A" * 32, ""):
        with pytest.raises(ValidationError):
            Selection.model_validate_json(
                json.dumps(
                    {**selection.model_dump(mode="json"), "credential_id": forged}
                )
            )


@pytest.mark.parametrize("change", ["logout", "policy"])
def test_stale_authority_rehearsal_resolves_to_failed_without_wedging(ledger, change):
    operation = request(ledger)
    if change == "logout":
        AccountStore(ledger.database).revoke_sessions(UUID(ADMIN_USER_ID), 1)
    else:
        ledger.service.policy_fingerprint = lambda _: "c" * 64
    complete_rehearsal(ledger, operation)
    with ledger.database.connect() as connection:
        state, receipt_json = connection.execute(
            "SELECT state, receipt_json FROM app_operations"
        ).fetchone()
    assert state == "failed"
    assert Receipt.model_validate_json(receipt_json).error == "authority_changed"
    assert not (ledger.spool / "requests" / f"{operation.operation_id}.json").exists()
    retried = request(ledger, session_version=2 if change == "logout" else 1)
    assert retried.operation_id != operation.operation_id


def test_contradictory_receipt_leaves_the_operation_pending_without_raising(ledger):
    operation = request(ledger)
    forged = Receipt(
        operation_id=operation.operation_id,
        selection=operation.approval.selection.model_copy(update={"generation": 7}),
        state="succeeded",
    )
    publish(
        ledger.spool / "receipts" / f"{operation.operation_id}.json",
        forged.model_dump_json().encode(),
    )
    ledger.service.reconcile()
    with ledger.database.connect() as connection:
        assert (
            connection.execute("SELECT state FROM app_operations").fetchone()[0]
            == "queued"
        )


def test_one_corrupt_operation_does_not_stall_other_apps(ledger, tmp_path):
    ledger.registry.register(
        document(managed("pad", "/apps/pad", state=str(tmp_path / "pad-state")))
    )
    corrupt = uuid4()
    with ledger.database.connect() as connection:
        connection.execute(
            "INSERT INTO app_operations(operation_id,request_id,app_id,environment,"
            "kind,state,operation_json) VALUES (?,?,'pad','test','rehearse',"
            "'queued','{\"operation_id\": 1}')",
            (str(corrupt), str(uuid4())),
        )
    operation = request(ledger)
    complete_rehearsal(ledger, operation)
    with ledger.database.connect() as connection:
        rows = {
            row[0]: row[1:]
            for row in connection.execute(
                "SELECT operation_id, state, reconcile_error FROM app_operations"
            ).fetchall()
        }
    assert rows[str(operation.operation_id)] == ("succeeded", None)
    state, error = rows[str(corrupt)]
    assert state == "queued"
    assert error is not None and error.startswith("Invalid JSON contract document")

"""Corrupt records and release history must never stall other apps.

Each case damages one record of the ``notes`` app and asserts that the ``pad``
app still reconciles and still renders, and that the damaged card or operation
carries a bounded error instead of an exception.
"""

import asyncio
import json
import sqlite3
from contextlib import suppress
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from test_app_deployments import complete_rehearsal, request
from test_app_deployments import ledger as ledger_fixture
from test_app_platform import managed
from test_app_registry import document
from test_dashboard_apps import browser as browser_fixture
from test_dashboard_apps import login, push
from test_installer_recovery import install, switch_request

from app import dashboard_apps
from app import main as core_main
from app.app_deployment_contracts import (
    InstalledSnapshot,
    Operation,
    Receipt,
    Rehearsal,
    rehearsal_checks,
)
from app.app_deployments import AppsService, eligible, installed, registration_for
from app.app_previews import CHECKS, PreviewResult
from app.app_spool import publish
from app.identity import ADMIN_USER_ID

ledger = ledger_fixture
browser = browser_fixture


def sql(ledger, statement, *args):
    with ledger.database.connect() as connection:
        connection.execute(statement, args)


def add_pad(ledger, tmp_path):
    ledger.registry.register(
        document(managed("pad", "/apps/pad", state=str(tmp_path / "pad-state")))
    )


def succeeded_rehearsal_receipt(ledger, operation):
    selection = operation.approval.selection
    publish(
        ledger.spool / "receipts" / f"{operation.operation_id}.json",
        Receipt(
            operation_id=operation.operation_id,
            selection=selection,
            state="succeeded",
            rehearsal=Rehearsal(
                selection=selection,
                image_id="sha256:" + "b" * 64,
                previous_image_id=None,
                backup_sha256=None,
                checks=rehearsal_checks(None),
            ),
        )
        .model_dump_json()
        .encode(),
    )


def history_row(ledger, payload):
    operation = uuid4()
    sql(
        ledger,
        "INSERT INTO app_operations(operation_id,request_id,app_id,environment,kind,"
        "state,operation_json) VALUES (?,?,'notes','test','deploy','succeeded','{}')",
        str(operation),
        str(uuid4()),
    )
    sql(
        ledger,
        "INSERT INTO app_installed_history VALUES (?,'notes','test',1,?)",
        str(operation),
        payload,
    )


def rehearse_row(ledger, receipt):
    sql(
        ledger,
        "INSERT INTO app_operations(operation_id,request_id,app_id,environment,kind,"
        "state,operation_json,receipt_json) VALUES (?,?,'notes','test','rehearse',"
        "'succeeded','{}',?)",
        str(uuid4()),
        str(uuid4()),
        receipt,
    )


PAGE = {
    "registration_open_brace": lambda ledger: sql(
        ledger, "UPDATE app_registrations SET definition_json='{' WHERE app_id='notes'"
    ),
    "registration_blob_ff": lambda ledger: sql(
        ledger,
        "UPDATE app_registrations SET definition_json=? WHERE app_id='notes'",
        b"\xff",
    ),
    "preview_ready_result_null": lambda ledger: sql(
        ledger, "UPDATE app_preview_attempts SET result_json=NULL WHERE app_id='notes'"
    ),
    "preview_result_garbage": lambda ledger: sql(
        ledger, "UPDATE app_preview_attempts SET result_json='{' WHERE app_id='notes'"
    ),
    "rehearsal_receipt_null": lambda ledger: rehearse_row(ledger, None),
    "rehearsal_receipt_garbage": lambda ledger: rehearse_row(ledger, "{"),
    "installed_snapshot_garbage": lambda ledger: history_row(ledger, "{"),
    "operation_json_garbage": lambda ledger: (
        request(ledger),
        sql(ledger, "UPDATE app_operations SET operation_json='{'"),
    ),
    "operation_receipt_garbage": lambda ledger: (
        request(ledger),
        sql(ledger, "UPDATE app_operations SET receipt_json='{'"),
    ),
    "reconcile_error_blob": lambda ledger: (
        request(ledger),
        sql(ledger, "UPDATE app_operations SET reconcile_error=?", b"\xff\xfe"),
    ),
}


@pytest.mark.parametrize("name", sorted(PAGE))
def test_overview_renders_other_apps_around_any_corrupt_record(
    name, browser, ledger, tmp_path
):
    add_pad(ledger, tmp_path)
    PAGE[name](ledger)
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    response = client.get("/dashboard/api/apps")
    assert response.status_code == 200, response.text[:200]
    assert str(tmp_path) not in response.text
    cards = {card["app_id"]: card for card in response.json()["apps"]}
    assert cards["pad"]["error"] is None
    assert cards["notes"]["error"] is None or isinstance(cards["notes"]["error"], str)


def pad_operation(ledger, notes_operation):
    data = notes_operation.model_dump(mode="json")
    data["operation_id"] = str(uuid4())
    data["request_id"] = str(uuid4())
    data["kind"] = "rehearse"
    data["recovers"] = None
    data["approval"]["selection"]["app_id"] = "pad"
    operation = Operation.model_validate_json(json.dumps(data))
    sql(
        ledger,
        "INSERT INTO app_operations(operation_id,request_id,app_id,environment,kind,"
        "state,operation_json) VALUES (?,?,'pad','test','rehearse','queued',?)",
        str(operation.operation_id),
        str(operation.request_id),
        operation.model_dump_json(),
    )
    publish(
        ledger.spool / "receipts" / f"{operation.operation_id}.json",
        Receipt(
            operation_id=operation.operation_id,
            selection=operation.approval.selection,
            state="failed",
            error="compatibility_failed",
        )
        .model_dump_json()
        .encode(),
    )
    return operation


def states(ledger, *operations):
    with ledger.database.connect() as connection:
        rows = connection.execute(
            "SELECT operation_id, state FROM app_operations WHERE operation_id IN ("
            + ",".join("?" * len(operations))
            + ")",
            tuple(str(item.operation_id) for item in operations),
        ).fetchall()
    return [dict(rows)[str(item.operation_id)] for item in operations]


LOOP = {
    "operation_json_garbage": lambda ledger, op: sql(
        ledger, "UPDATE app_operations SET operation_json='{' WHERE app_id='notes'"
    ),
    "receipt_is_directory": lambda ledger, op: (
        ledger.spool / "receipts" / f"{op.operation_id}.json"
    ).mkdir(),
    "receipt_garbage": lambda ledger, op: publish(
        ledger.spool / "receipts" / f"{op.operation_id}.json", b"{"
    ),
    "preview_ready_result_null_then_success": lambda ledger, op: (
        succeeded_rehearsal_receipt(ledger, op),
        sql(
            ledger,
            "UPDATE app_preview_attempts SET result_json=NULL WHERE app_id='notes'",
        ),
    ),
    "sqlite_abort_on_notes_update": lambda ledger, op: (
        succeeded_rehearsal_receipt(ledger, op),
        sql(
            ledger,
            "CREATE TRIGGER probe BEFORE UPDATE OF state ON app_operations "
            "WHEN OLD.app_id='notes' BEGIN SELECT RAISE(ABORT,'probe'); END",
        ),
    ),
    "admin_row_session_version_text": lambda ledger, op: (
        succeeded_rehearsal_receipt(ledger, op),
        sql(ledger, "UPDATE users SET session_version='x' WHERE id=?", ADMIN_USER_ID),
    ),
}


@pytest.mark.parametrize("name", sorted(LOOP))
def test_reconcile_isolates_any_corrupt_operation(name, ledger, tmp_path):
    add_pad(ledger, tmp_path)
    notes = request(ledger)
    pad = pad_operation(ledger, notes)
    LOOP[name](ledger, notes)
    ledger.service.reconcile()
    notes_state, pad_state = states(ledger, notes, pad)
    assert pad_state == "failed", "pad was not reconciled"
    with ledger.database.connect() as connection:
        error = connection.execute(
            "SELECT reconcile_error FROM app_operations WHERE operation_id=?",
            (str(notes.operation_id),),
        ).fetchone()[0]
    assert notes_state in {"queued", "failed"}
    assert error is None or (isinstance(error, str) and str(tmp_path) not in error)


def finish(ledger, operation, generation, digest):
    switch_request(ledger, operation)
    with ledger.database.connect() as connection:
        registration = registration_for(connection, "notes")
        release = eligible(connection, registration, digest)
    snapshot = InstalledSnapshot(
        operation_id=operation.recovers or operation.operation_id,
        generation=generation,
        selection=operation.approval.selection,
        registration=registration,
        image=release.manifest.image,
        image_id="sha256:" + "b" * 64,
    )
    publish(
        ledger.spool / "receipts" / f"{operation.operation_id}.json",
        Receipt(
            operation_id=operation.operation_id,
            selection=operation.approval.selection,
            state="succeeded",
            installed=snapshot,
            authorization_committed=True,
        )
        .model_dump_json()
        .encode(),
    )


def deploy(ledger, digest, preview_id):
    rehearsal = request(ledger, digest=digest, preview_id=preview_id)
    complete_rehearsal(ledger, rehearsal)
    return request(
        ledger,
        "deploy",
        digest=digest,
        preview_id=preview_id,
        rehearsal_id=rehearsal.operation_id,
    )


def installed_generation(ledger):
    with ledger.database.connect() as connection:
        snapshot = installed(connection, "notes", "test")
    assert snapshot is not None
    return snapshot.generation, snapshot.selection.manifest_sha256


def test_release_cycle_reaches_terminal_states_without_stalling_other_apps(
    ledger, tmp_path, monkeypatch
):
    add_pad(ledger, tmp_path)
    first = install(ledger)
    release_a = ledger.digest
    release_b = push(ledger, monkeypatch, source_commit="c" * 40)
    with ledger.database.connect() as connection:
        bundle = connection.execute(
            "SELECT artifact_sha256 FROM app_eligible_releases WHERE manifest_sha256=?",
            (release_b,),
        ).fetchone()[0]
        connection.execute("UPDATE app_preview_attempts SET state='retired'")
        preview_b = uuid4()
        connection.execute(
            "INSERT INTO app_preview_attempts VALUES (?,?,?,?,?,?)",
            (
                str(preview_b),
                "notes",
                release_b,
                bundle,
                "ready",
                PreviewResult(
                    attempt_id=preview_b,
                    app_id="notes",
                    manifest_sha256=release_b,
                    bundle_sha256=bundle,
                    state="ready",
                    image_id="sha256:" + "b" * 64,
                    preview_url="https://preview.example/notes/",
                    checks=dict.fromkeys(CHECKS, True),
                ).model_dump_json(),
            ),
        )

    deploy_b = deploy(ledger, release_b, preview_b)
    finish(ledger, deploy_b, 2, release_b)
    ledger.service.reconcile()
    assert states(ledger, deploy_b) == ["succeeded"]
    assert installed_generation(ledger) == (2, release_b)

    rollback_a = request(
        ledger, "rollback", digest=release_a, preview_id=first.selection.preview_id
    )
    finish(ledger, rollback_a, 3, release_a)
    pad = pad_operation(ledger, rollback_a)
    ledger.service.reconcile()
    assert states(ledger, rollback_a, pad) == ["succeeded", "failed"]
    assert installed_generation(ledger) == (3, release_a)

    redeploy_b = deploy(ledger, release_b, preview_b)
    finish(ledger, redeploy_b, 4, release_b)
    ledger.service.reconcile()
    assert states(ledger, redeploy_b) == ["succeeded"]
    assert installed_generation(ledger) == (4, release_b)

    blocked = request(
        ledger, "rollback", digest=release_a, preview_id=first.selection.preview_id
    )
    switch_request(ledger, blocked)
    publish(
        ledger.spool / "receipts" / f"{blocked.operation_id}.json",
        Receipt(
            operation_id=blocked.operation_id,
            selection=blocked.approval.selection,
            state="blocked",
            error="recovery_required",
        )
        .model_dump_json()
        .encode(),
    )
    ledger.service.reconcile()
    assert states(ledger, blocked) == ["blocked"]
    recover = request(ledger, "recover", digest=release_a, preview_id=None)
    finish(ledger, recover, 5, release_a)
    pad_again = pad_operation(ledger, rollback_a)
    ledger.service.reconcile()
    assert states(ledger, recover, blocked, pad_again) == [
        "succeeded",
        "succeeded",
        "failed",
    ]
    assert installed_generation(ledger) == (5, release_a)
    with ledger.database.connect() as connection:
        recorded = connection.execute(
            "SELECT manifest_sha256 FROM app_releases WHERE app_id='notes' "
            "ORDER BY manifest_sha256"
        ).fetchall()
        pending = connection.execute(
            "SELECT count(*) FROM app_operations WHERE state IN ('queued','switching')"
        ).fetchone()[0]
    assert [row[0] for row in recorded] == sorted({release_a, release_b})
    assert pending == 0


def test_action_maps_unexpected_errors_to_a_bounded_503(browser, ledger, monkeypatch):
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    token = client.get("/dashboard/api/apps").json()["csrf"]

    async def broken(*_):
        raise sqlite3.OperationalError("disk I/O error at /private/core.sqlite")

    monkeypatch.setattr(dashboard_apps, "perform", broken)
    response = client.post(
        "/dashboard/api/apps/check",
        json=dict(app_id="notes", digest=ledger.digest, request_id=str(uuid4())),
        headers={"Origin": "https://platform.test", "X-CSRF-Token": token},
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "Application records are unavailable"}
    assert "/private" not in response.text
    assert (
        client.post(
            "/dashboard/api/apps/bogus",
            json=dict(app_id="notes", digest=ledger.digest, request_id=str(uuid4())),
            headers={"Origin": "https://platform.test", "X-CSRF-Token": token},
        ).status_code
        == 503
    )


def test_background_reconcile_survives_an_unexpected_error(ledger, monkeypatch):
    calls = []

    def reconcile(self):
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("unexpected record shape")

    monkeypatch.setattr(AppsService, "reconcile", reconcile)
    monkeypatch.setenv("HOME_PLATFORM_INSTALLER_POLICY", "/etc/home-platform/x.json")
    monkeypatch.setattr(
        "ops.installer_policy.load_policy",
        lambda _: SimpleNamespace(spool=str(ledger.spool)),
    )
    real_sleep = asyncio.sleep
    monkeypatch.setattr(core_main.asyncio, "sleep", lambda _: real_sleep(0))

    async def drive():
        task = asyncio.create_task(core_main.reconcile_deployments(ledger.database))
        for _ in range(200):
            if len(calls) >= 2 or task.done():
                break
            await real_sleep(0.01)
        done = task.done()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return done

    assert asyncio.run(drive()) is False
    assert len(calls) >= 2

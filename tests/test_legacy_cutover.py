import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from test_app_deployments import complete_rehearsal, request
from test_installer_recovery import install, switch_request
from test_managed_installer import ledger as ledger_fixture

from app import migrations
from app.app_contracts import ContractError, Registration
from app.app_deployment_contracts import InstalledSnapshot, Receipt, fingerprint
from app.app_deployments import (
    eligible,
    provision_legacy,
    registered,
    restore_unaccepted_legacy,
)
from app.app_registry import AppRegistry, decode_registration
from app.app_spool import publish
from app.config import InstanceFacts
from app.database import Database

ledger = ledger_fixture


def legacy_of(registration, state=None):
    value = registration.model_dump(mode="json")
    value["source"] = {"kind": "legacy"}
    value["installation"]["owner"] = "legacy-scripts"
    value["installation"]["probe"]["readiness"] = "legacy-json"
    value["bindings"]["state_directory"] = state
    return Registration.model_validate_json(json.dumps(value))


def managed_of(registration, state="/srv/wishlist", **changes):
    value = registration.model_dump(mode="json")
    value["source"] = {
        "kind": "published",
        "repository": "example/wishlist",
        "branch": "main",
        "workflow": ".github/workflows/publish.yml",
        "image_repository": "ghcr.io/example/wishlist",
    }
    value["installation"]["owner"] = "platform"
    value["installation"]["probe"]["readiness"] = "v1-status"
    value["bindings"]["state_directory"] = state
    value.update(changes)
    return Registration.model_validate_json(json.dumps(value))


def snapshot(database, app_id):
    with database.connect() as connection:
        return (
            connection.execute(
                "SELECT route, definition_json FROM app_registrations WHERE app_id = ?",
                (app_id,),
            ).fetchone()[:],
            connection.execute(
                "SELECT definition_json FROM app_registration_changes WHERE app_id = ?",
                (app_id,),
            ).fetchall(),
        )


def overwrite(database, registration):
    with database.connect() as connection:
        connection.execute(
            "UPDATE app_registrations SET definition_json = ? WHERE app_id = ?",
            (
                registration.model_dump_json(exclude={"app_id", "route"}),
                registration.app_id,
            ),
        )


@pytest.fixture
def wishlist(tmp_path, monkeypatch):
    database = Database(tmp_path / "core.sqlite")
    with monkeypatch.context() as before:
        before.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:16])
        database.initialize()
    database.initialize()
    registry = AppRegistry(database)
    registry.publish_instance(
        InstanceFacts("test", "http://127.0.0.1:1/api/apps/v1", "UTC")
    )
    with database.connect() as connection:
        legacy = registered(connection, "wishlist")
    yield SimpleNamespace(
        database=database,
        registry=registry,
        legacy=legacy,
        proposal=managed_of(legacy),
    )
    database.close()


def test_seeded_wishlist_provisions_once_and_retries_unchanged(wishlist):
    assert wishlist.registry.managed_app("wishlist") is None
    assert wishlist.legacy.bindings.state_directory is None

    outcome = provision_legacy(
        wishlist.database,
        wishlist.proposal,
        expected_legacy_sha256=fingerprint(wishlist.legacy),
    )

    assert outcome == "updated"
    assert wishlist.registry.managed_app("wishlist").registration == wishlist.proposal
    keys = [app.key for app in wishlist.registry.navigation()]
    assert keys.count("wishlist") == 1
    row, audit = snapshot(wishlist.database, "wishlist")
    assert len(audit) == 1
    assert (
        decode_registration("wishlist", "/wishlist", audit[0][0]) == wishlist.proposal
    )
    assert (
        provision_legacy(
            wishlist.database,
            wishlist.proposal,
            expected_legacy_sha256=fingerprint(wishlist.legacy),
        )
        == "unchanged"
    )
    assert snapshot(wishlist.database, "wishlist") == (row, audit)


@pytest.mark.parametrize(
    ("legacy_state", "proposal_changes", "expected", "match"),
    [
        (None, {}, "0" * 64, "fingerprint"),
        (None, {"route": "/wishlist-next"}, None, "route"),
        (None, {"access": "administrator"}, None, "access"),
        ("/srv/old-wishlist", {}, None, "state directory"),
        (None, {"source": {"kind": "legacy"}}, None, "published managed"),
    ],
)
def test_provisioning_refusals_leave_the_row_unchanged(
    wishlist, legacy_state, proposal_changes, expected, match
):
    legacy = wishlist.legacy
    if legacy_state is not None:
        legacy = legacy.model_copy(
            update={
                "bindings": legacy.bindings.model_copy(
                    update={"state_directory": legacy_state}
                )
            }
        )
        overwrite(wishlist.database, legacy)
    if proposal_changes.get("source"):
        proposal = legacy
    else:
        proposal = managed_of(legacy, **proposal_changes)
    before = snapshot(wishlist.database, "wishlist")

    with pytest.raises(ContractError, match=match):
        provision_legacy(
            wishlist.database,
            proposal,
            expected_legacy_sha256=expected or fingerprint(legacy),
        )

    assert snapshot(wishlist.database, "wishlist") == before
    assert wishlist.registry.managed_app("wishlist") is None


def test_provisioning_requires_a_configured_instance(tmp_path, monkeypatch):
    database = Database(tmp_path / "core.sqlite")
    with monkeypatch.context() as before:
        before.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:16])
        database.initialize()
    database.initialize()
    with database.connect() as connection:
        legacy = registered(connection, "wishlist")
    before = snapshot(database, "wishlist")
    with pytest.raises(ContractError, match="not configured"):
        provision_legacy(
            database, managed_of(legacy), expected_legacy_sha256=fingerprint(legacy)
        )
    assert snapshot(database, "wishlist") == before
    database.close()


def test_pending_operation_refuses_provisioning(ledger):
    proposal = ledger.registry.managed_app("notes").registration
    request(ledger)
    legacy = legacy_of(proposal)
    overwrite(ledger.database, legacy)
    before = snapshot(ledger.database, "notes")

    with pytest.raises(ContractError, match="pending"):
        provision_legacy(
            ledger.database, proposal, expected_legacy_sha256=fingerprint(legacy)
        )

    assert snapshot(ledger.database, "notes") == before


def test_accepted_history_permanently_refuses_both_transitions(ledger):
    accepted = install(ledger)
    managed = accepted.registration
    legacy = legacy_of(managed)
    before = snapshot(ledger.database, "notes")

    with pytest.raises(ContractError, match="Accepted"):
        restore_unaccepted_legacy(
            ledger.database,
            legacy,
            expected_legacy_sha256=fingerprint(legacy),
            expected_managed_sha256=fingerprint(managed),
        )
    assert snapshot(ledger.database, "notes") == before
    assert ledger.registry.active_credential("notes") is not None

    overwrite(ledger.database, legacy)
    before = snapshot(ledger.database, "notes")
    with pytest.raises(ContractError, match="Accepted"):
        provision_legacy(
            ledger.database, managed, expected_legacy_sha256=fingerprint(legacy)
        )
    assert snapshot(ledger.database, "notes") == before


def test_reinitialize_preserves_accepted_installation_and_all_registry_history(ledger):
    install(ledger)
    with ledger.database.connect() as connection:
        before = tuple(connection.iterdump())
    ledger.database.initialize()
    with ledger.database.connect() as connection:
        assert tuple(connection.iterdump()) == before
    assert ledger.registry.managed_app("notes") is not None
    assert ledger.registry.active_credential("notes") is not None


def test_restore_returns_legacy_and_revokes_credentials(wishlist, tmp_path):
    original = snapshot(wishlist.database, "wishlist")[0]
    provision_legacy(
        wishlist.database,
        wishlist.proposal,
        expected_legacy_sha256=fingerprint(wishlist.legacy),
    )
    issued = wishlist.registry.issue_credential(
        "wishlist", tmp_path / "credentials" / "wishlist"
    )
    token = (tmp_path / "credentials" / "wishlist").read_text().strip()
    assert wishlist.registry.verify_credential(token).credential_id == (
        issued.credential_id
    )

    outcome = restore_unaccepted_legacy(
        wishlist.database,
        wishlist.legacy,
        expected_legacy_sha256=fingerprint(wishlist.legacy),
        expected_managed_sha256=fingerprint(wishlist.proposal),
    )

    assert outcome == "updated"
    row, audit = snapshot(wishlist.database, "wishlist")
    assert row == original
    assert len(audit) == 2
    assert wishlist.registry.managed_app("wishlist") is None
    assert wishlist.registry.verify_credential(token) is None
    assert wishlist.registry.active_credential("wishlist") is None

    with wishlist.database.connect() as connection:
        connection.execute(
            "INSERT INTO app_credentials "
            "(credential_id, app_id, environment, verifier, file_path) "
            "VALUES (?, 'wishlist', 'test', ?, '/srv/stray')",
            ("f" * 32, "e" * 64),
        )
    assert (
        restore_unaccepted_legacy(
            wishlist.database,
            wishlist.legacy,
            expected_legacy_sha256=fingerprint(wishlist.legacy),
            expected_managed_sha256=fingerprint(wishlist.proposal),
        )
        == "unchanged"
    )
    assert snapshot(wishlist.database, "wishlist") == (row, audit)
    assert wishlist.registry.active_credential("wishlist") is None


@pytest.mark.parametrize(
    ("restore_changes", "expected", "match"),
    [
        ({}, "0" * 64, "fingerprint"),
        ({"access": "administrator"}, None, "access"),
        ({"route": "/wishlist-next"}, None, "route"),
        (None, None, "legacy-scripts"),
    ],
)
def test_restore_refusals_keep_the_managed_row_and_credential(
    wishlist, tmp_path, restore_changes, expected, match
):
    provision_legacy(
        wishlist.database,
        wishlist.proposal,
        expected_legacy_sha256=fingerprint(wishlist.legacy),
    )
    wishlist.registry.issue_credential("wishlist", tmp_path / "credential")
    legacy = (
        wishlist.proposal
        if restore_changes is None
        else wishlist.legacy.model_copy(update=restore_changes)
    )
    before = snapshot(wishlist.database, "wishlist")

    with pytest.raises(ContractError, match=match):
        restore_unaccepted_legacy(
            wishlist.database,
            legacy,
            expected_legacy_sha256=fingerprint(legacy),
            expected_managed_sha256=expected or fingerprint(wishlist.proposal),
        )

    assert snapshot(wishlist.database, "wishlist") == before
    assert wishlist.registry.active_credential("wishlist") is not None


@pytest.mark.parametrize("branch", ["updated", "unchanged"])
def test_restore_refuses_a_legacy_definition_other_than_the_replaced_one(
    wishlist, tmp_path, branch
):
    replaced = fingerprint(wishlist.legacy)
    installation = wishlist.legacy.installation
    other = wishlist.legacy.model_copy(
        update={
            "installation": installation.model_copy(
                update={"host_port": installation.host_port + 1}
            )
        }
    )
    provision_legacy(
        wishlist.database, wishlist.proposal, expected_legacy_sha256=replaced
    )
    wishlist.registry.issue_credential("wishlist", tmp_path / "credential")
    if branch == "unchanged":
        overwrite(wishlist.database, other)
    before = snapshot(wishlist.database, "wishlist")

    with pytest.raises(ContractError, match="Legacy registration fingerprint"):
        restore_unaccepted_legacy(
            wishlist.database,
            other,
            expected_legacy_sha256=replaced,
            expected_managed_sha256=fingerprint(wishlist.proposal),
        )

    assert snapshot(wishlist.database, "wishlist") == before
    assert wishlist.registry.active_credential("wishlist") is not None


def test_pending_operation_refuses_restore(ledger):
    managed = ledger.registry.managed_app("notes").registration
    legacy = legacy_of(managed)
    request(ledger)
    before = snapshot(ledger.database, "notes")
    with pytest.raises(ContractError, match="pending"):
        restore_unaccepted_legacy(
            ledger.database,
            legacy,
            expected_legacy_sha256=fingerprint(legacy),
            expected_managed_sha256=fingerprint(managed),
        )
    assert snapshot(ledger.database, "notes") == before
    assert ledger.registry.active_credential("notes") is not None


def receipt(ledger, operation, **values):
    publish(
        ledger.spool / "receipts" / f"{operation.operation_id}.json",
        Receipt(
            operation_id=operation.operation_id,
            selection=operation.approval.selection,
            **values,
        )
        .model_dump_json()
        .encode(),
    )
    ledger.service.reconcile()


def blocked_first_deploy(ledger):
    rehearsal = request(ledger)
    complete_rehearsal(ledger, rehearsal)
    deploy = request(ledger, "deploy", rehearsal_id=rehearsal.operation_id)
    switch_request(ledger, deploy)
    receipt(ledger, deploy, state="blocked", error="recovery_required")
    return rehearsal, deploy


def recover(ledger):
    operation = request(ledger, "recover", preview_id=None)
    switch_request(ledger, operation)
    receipt(ledger, operation, state="succeeded", authorization_committed=True)


def operation_states(ledger):
    with ledger.database.connect() as connection:
        return dict(
            connection.execute("SELECT kind, state FROM app_operations").fetchall()
        )


def test_failed_first_install_restores_and_stale_approval_stays_dead(ledger, tmp_path):
    managed = ledger.registry.managed_app("notes").registration
    legacy = legacy_of(managed)
    rehearsal, _ = blocked_first_deploy(ledger)

    def restore():
        return restore_unaccepted_legacy(
            ledger.database,
            legacy,
            expected_legacy_sha256=fingerprint(legacy),
            expected_managed_sha256=fingerprint(managed),
        )

    with pytest.raises(ContractError, match="pending"):
        restore()
    recover(ledger)
    assert operation_states(ledger) == {
        "rehearse": "succeeded",
        "deploy": "failed",
        "recover": "succeeded",
    }

    assert restore() == "updated"
    assert ledger.registry.active_credential("notes") is None
    assert ledger.registry.managed_route("/apps/notes/") is None
    with pytest.raises(ContractError, match="not managed"):
        request(ledger)

    assert (
        provision_legacy(
            ledger.database, managed, expected_legacy_sha256=fingerprint(legacy)
        )
        == "updated"
    )
    ledger.registry.issue_credential("notes", tmp_path / "credentials" / "fresh")
    with pytest.raises(ContractError, match="rehearsal changed"):
        request(ledger, "deploy", rehearsal_id=rehearsal.operation_id)


def test_forged_success_for_a_failed_first_deploy_is_not_accepted(ledger):
    managed = ledger.registry.managed_app("notes").registration
    legacy = legacy_of(managed)
    _, deploy = blocked_first_deploy(ledger)
    recover(ledger)
    restore_unaccepted_legacy(
        ledger.database,
        legacy,
        expected_legacy_sha256=fingerprint(legacy),
        expected_managed_sha256=fingerprint(managed),
    )
    with ledger.database.connect() as connection:
        release = eligible(connection, managed, ledger.digest)
    states = operation_states(ledger)
    selection = deploy.approval.selection
    forged = Receipt(
        operation_id=deploy.operation_id,
        selection=selection,
        state="succeeded",
        installed=InstalledSnapshot(
            operation_id=deploy.operation_id,
            generation=1,
            selection=selection,
            registration=managed,
            image=release.manifest.image,
            image_id="sha256:" + "b" * 64,
        ),
        authorization_committed=True,
    )
    (ledger.spool / "receipts" / f"{deploy.operation_id}.json").write_bytes(
        forged.model_dump_json().encode()
    )

    ledger.service.reconcile()

    with ledger.database.connect() as connection:
        history = connection.execute("SELECT count(*) FROM app_installed_history")
        assert history.fetchone()[0] == 0
    assert operation_states(ledger) == states
    assert (
        provision_legacy(
            ledger.database, managed, expected_legacy_sha256=fingerprint(legacy)
        )
        == "updated"
    )


def test_operator_cli_round_trip(wishlist, tmp_path):
    argv = [
        sys.executable,
        "-m",
        "ops.apps",
        "--database",
        str(wishlist.database.path),
    ]

    def run(*command):
        return subprocess.run([*argv, *command], capture_output=True, text=True)

    def listed():
        result = run("list")
        assert result.returncode == 0, result.stderr
        return next(
            item for item in json.loads(result.stdout) if item["app_id"] == "wishlist"
        )

    def written(name, registration):
        path = tmp_path / name
        path.write_text(registration.model_dump_json())
        return str(path)

    legacy_sha = listed()["registration_sha256"]
    assert legacy_sha == fingerprint(wishlist.legacy)
    proposal = written("managed.json", wishlist.proposal)
    legacy = written("legacy.json", wishlist.legacy)
    stateless = json.loads(wishlist.proposal.model_dump_json())
    stateless["bindings"]["state_directory"] = None
    (tmp_path / "stateless.json").write_text(json.dumps(stateless))
    before = snapshot(wishlist.database, "wishlist")

    refused = run(
        "provision-legacy",
        str(tmp_path / "stateless.json"),
        "--expected-legacy-sha256",
        legacy_sha,
    )
    assert refused.returncode == 2
    assert "state directory" in refused.stderr
    assert str(tmp_path) not in refused.stderr
    wrong = run("provision-legacy", proposal, "--expected-legacy-sha256", "0" * 64)
    assert wrong.returncode == 2
    assert snapshot(wishlist.database, "wishlist") == before

    for expected in ("updated", "unchanged"):
        result = run(
            "provision-legacy", proposal, "--expected-legacy-sha256", legacy_sha
        )
        assert json.loads(result.stdout) == {"outcome": expected}, result.stderr
    managed_sha = listed()["registration_sha256"]
    assert managed_sha == fingerprint(wishlist.proposal)
    assert listed()["installation"] == "platform"

    restore = ("restore-legacy", legacy, "--expected-managed-sha256", managed_sha)
    wrong = run(*restore, "--expected-legacy-sha256", "0" * 64)
    assert wrong.returncode == 2
    assert "Legacy registration fingerprint" in wrong.stderr
    assert listed()["registration_sha256"] == managed_sha
    for expected in ("updated", "unchanged"):
        result = run(*restore, "--expected-legacy-sha256", legacy_sha)
        assert json.loads(result.stdout) == {"outcome": expected}, result.stderr
    assert listed()["registration_sha256"] == legacy_sha
    assert snapshot(wishlist.database, "wishlist")[0] == before[0]

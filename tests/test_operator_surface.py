"""What the operator sees: honest first-install rehearsals, a clear gateway
answer for an app that is not installed yet, the Apps link, and what to do
after a policy refusal."""

from pathlib import Path
from uuid import UUID

import pytest
from test_app_deployments import complete_rehearsal, request
from test_app_deployments import ledger as ledger_fixture
from test_dashboard_apps import browser as browser_fixture
from test_dashboard_apps import login
from test_installer_recovery import gateway, install
from test_managed_installer import helper as helper_fixture
from test_managed_installer import installed_by_root, retain

from app import dashboard_pages
from app.app_deployment_contracts import (
    REHEARSAL_CHECKS,
    InstalledSnapshot,
    Rehearsal,
    rehearsal_checks,
)
from app.identity import ADMIN_USER_ID
from ops import managed_installer
from ops.managed_installer import Installer

ledger = ledger_fixture
helper = helper_fixture
browser = browser_fixture
NOT_INSTALLED = "Application is not installed; approve a release from the Apps page"


def rehearsal_row(ledger):
    with ledger.database.connect() as connection:
        return connection.execute(
            "SELECT state, reconcile_error FROM app_operations WHERE kind='rehearse'"
        ).fetchone()


def test_first_install_rehearsal_must_not_claim_a_previous_image_read(ledger):
    operation = request(ledger)
    complete_rehearsal(ledger, operation, dict.fromkeys(REHEARSAL_CHECKS, True))
    assert tuple(rehearsal_row(ledger)) == (
        "queued",
        "Installer rehearsal is incomplete",
    )
    (ledger.spool / "receipts" / f"{operation.operation_id}.json").unlink()
    complete_rehearsal(ledger, operation)
    assert tuple(rehearsal_row(ledger)) == ("succeeded", None)


@pytest.mark.parametrize(
    "previous, checks, expected",
    [
        (None, rehearsal_checks(None), True),
        (None, dict.fromkeys(REHEARSAL_CHECKS, True), False),
        ("sha256:" + "a" * 64, rehearsal_checks("sha256:" + "a" * 64), True),
        ("sha256:" + "a" * 64, rehearsal_checks(None), False),
        (None, {**rehearsal_checks(None), "restore": False}, False),
        (
            None,
            {k: v for k, v in rehearsal_checks(None).items() if k != "startup"},
            False,
        ),
    ],
)
def test_rehearsal_passes_only_with_every_applicable_check(
    ledger, previous, checks, expected
):
    selection = request(ledger).approval.selection
    rehearsal = Rehearsal(
        selection=selection,
        image_id="sha256:" + "b" * 64,
        previous_image_id=previous,
        backup_sha256=None,
        checks=checks,
    )
    assert rehearsal.passed() is expected


@pytest.mark.parametrize("upgrade", [False, True])
def test_root_rehearsal_reports_the_previous_read_only_when_there_was_one(
    helper, tmp_path, monkeypatch, upgrade
):
    installer, operation, authority, app, release, _ = helper
    monkeypatch.setattr(managed_installer.os, "chown", lambda *_: None)
    monkeypatch.setattr(managed_installer.os, "fchown", lambda *_: None)
    monkeypatch.setattr(installer, "remove", lambda *_: None)
    monkeypatch.setattr(installer, "start", lambda *args, **kwargs: "fixture")
    monkeypatch.setattr(installer, "rehearsal_ready", lambda *_: None)
    monkeypatch.setattr(installer, "backup", lambda *args, **kwargs: None)
    monkeypatch.setattr(installer, "semantic_state", lambda *_: "[]")
    backup = None
    if upgrade:
        previous = InstalledSnapshot(
            operation_id=operation.operation_id,
            generation=1,
            selection=operation.approval.selection,
            registration=app.registration,
            image=release.image,
            image_id="sha256:" + "b" * 64,
        )
        installed_by_root(installer, previous)
        retain(installer, release)
        backup = tmp_path / "copy.sqlite"
        backup.write_bytes(b"disposable fixture")
    result = Installer.rehearse(
        installer, authority, app, release, "sha256:" + "b" * 64, backup
    )
    assert result.passed()
    assert (result.previous_image_id is not None) is upgrade
    assert result.checks["previous_reads_current"] is (True if upgrade else None)


def test_gateway_answers_an_uninstalled_managed_route_with_503(ledger, monkeypatch):
    with gateway(ledger) as client:
        assert client.get("/apps/notes/").status_code == 403
    monkeypatch.setenv("HOME_PLATFORM_INSTALLER_POLICY", "/etc/home-platform/x.json")
    with gateway(ledger) as client:
        response = client.get("/apps/notes/")
        assert response.status_code == 503
        assert response.json() == {"detail": NOT_INSTALLED}
    install(ledger)
    with gateway(ledger) as client:
        assert client.get("/apps/notes/").status_code == 403


def test_operator_navigation_links_to_apps():
    assert 'href="/dashboard/apps"' in dashboard_pages.DASHBOARD_HTML
    jobs = Path(dashboard_pages.__file__).with_name("jobs.html").read_text()
    assert '<a class="operator-nav hidden" href="/dashboard/apps">' in jobs


def test_policy_refused_card_tells_the_operator_to_update_host_policy(browser):
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    page = client.get("/dashboard/apps").text
    assert "policy_refused" in page
    assert "host policy entry" in page and "approve it again" in page

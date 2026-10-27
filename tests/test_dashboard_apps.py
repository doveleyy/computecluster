import asyncio
import json
import sqlite3
import threading
import time
from types import SimpleNamespace
from typing import get_args
from uuid import UUID, uuid4

import pytest
import test_app_discovery
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_app_deployments import complete_rehearsal, push, request
from test_app_deployments import ledger as ledger_fixture
from test_app_platform import managed
from test_app_registry import document
from test_installer_recovery import (
    accept,
    advertise,
    deploy_request,
    install,
    propose_release_with_secret,
    retire,
    retire_request,
    retired_receipt,
    switch_request,
)

from app import app_deployments, dashboard_apps
from app.accounts import AccountStore, UserCreate, encode_session
from app.app_contracts import ContractError
from app.app_deployment_contracts import ArchiveRecord, Receipt, fingerprint
from app.app_deployments import (
    Kind,
    proposed_registration,
    read_authority,
    registration_for,
    update_registration,
)
from app.app_spool import publish
from app.dashboard_auth import SESSION_COOKIE
from app.identity import ADMIN_USER_ID

ledger = ledger_fixture


@pytest.fixture
def browser(ledger, monkeypatch):
    application = FastAPI()
    application.include_router(dashboard_apps.router)
    accounts = AccountStore(ledger.database)
    application.state.account_store = accounts
    application.state.settings = SimpleNamespace(api_token="test-only-session-key")
    monkeypatch.setattr(dashboard_apps, "service", lambda _: ledger.service)
    monkeypatch.setenv("HOME_PLATFORM_APPS_ORIGIN", "https://platform.test")
    with TestClient(application, base_url="https://platform.test") as client:
        yield client, accounts


def login(client, identity, session_version=1):
    client.cookies.set(
        SESSION_COOKIE,
        encode_session(
            identity, "test-only-session-key", int(time.time()) + 60, session_version
        ),
    )


def test_apps_page_and_data_deny_members_and_missing_sessions(browser):
    client, accounts = browser
    for route in ("/dashboard/apps", "/dashboard/api/apps"):
        assert client.get(route).status_code == 401
    member = accounts.create(
        UserCreate(username="reader", password="long fixture password")
    )
    login(client, member)
    for route in ("/dashboard/apps", "/dashboard/api/apps"):
        assert client.get(route).status_code == 403
    assert client.post("/dashboard/api/apps/reject", json={}).status_code == 403


def test_apps_mutations_require_origin_and_session_bound_csrf(browser, ledger):
    client, accounts = browser
    identity = accounts.get_identity(UUID(ADMIN_USER_ID), 1)
    login(client, identity)
    overview = client.get("/dashboard/api/apps")
    assert overview.status_code == 200
    token = overview.json()["csrf"]
    payload = dict(app_id="notes", digest=ledger.digest, request_id=str(uuid4()))
    for headers in (
        {},
        {"Origin": "https://platform.test"},
        {"Origin": "https://evil.test", "X-CSRF-Token": token},
        {"Origin": "https://platform.test", "X-CSRF-Token": "forged"},
    ):
        assert (
            client.post(
                "/dashboard/api/apps/reject", json=payload, headers=headers
            ).status_code
            == 403
        )
    result = client.post(
        "/dashboard/api/apps/reject",
        json=payload,
        headers={"Origin": "https://platform.test", "X-CSRF-Token": token},
    )
    assert result.status_code == 200
    assert result.json() == {"state": "rejected"}


ARCHIVE = ArchiveRecord(
    file_name=f"retired-g2-{uuid4()}.sqlite",
    sha256="c" * 64,
    content_sha256="d" * 64,
    tables=("notes", "tags"),
    size_bytes=24576,
)


def retire_with_archive(ledger, current):
    operation = retire_request(ledger, current)
    switch_request(ledger, operation)
    receipt = retired_receipt(operation, current, archive=ARCHIVE)
    publish(
        ledger.spool / "receipts" / f"{operation.operation_id}.json",
        receipt.model_dump_json().encode(),
    )
    ledger.service.reconcile()
    return operation


def test_apps_overview_shows_host_paths_only_in_a_pending_change_to_a_live_session(
    browser, ledger
):
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    result = client.get("/dashboard/api/apps")
    assert result.status_code == 200
    assert str(ledger.database.path.parent) not in result.text
    assert "file_path" not in result.text
    first = install(ledger)
    value = json.loads(first.registration.model_dump_json())
    value["bindings"]["secret_files"][0]["file_path"] = "/private/rotated-key"
    update_registration(ledger.database, json.dumps(value).encode())
    card = client.get("/dashboard/api/apps").json()["apps"][0]
    assert "/private/rotated-key" in json.dumps(card.pop("pending"))
    assert "/private/" not in json.dumps(card)
    retire_with_archive(ledger, first)
    card = client.get("/dashboard/api/apps").json()["apps"][0]
    assert card["retired"]["archive"]["file_name"] == ARCHIVE.file_name
    assert "/private/" not in json.dumps(card)
    assert str(ledger.database.path.parent) not in json.dumps(card)
    with ledger.database.connect() as connection:
        connection.execute("UPDATE users SET session_version = 2 WHERE role = 'ADMIN'")
    assert client.get("/dashboard/api/apps").status_code == 401


def test_apps_recover_action_needs_a_blocked_operation(browser, ledger):
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    token = client.get("/dashboard/api/apps").json()["csrf"]
    headers = {"Origin": "https://platform.test", "X-CSRF-Token": token}
    payload = dict(app_id="notes", digest=ledger.digest, request_id=str(uuid4()))
    refused = client.post("/dashboard/api/apps/recover", json=payload, headers=headers)
    assert (refused.status_code, refused.json()) == (
        409,
        {"detail": "No blocked operation matches the selection"},
    )
    deploy = deploy_request(ledger)
    switch_request(ledger, deploy)
    receipt = Receipt(
        operation_id=deploy.operation_id,
        selection=deploy.approval.selection,
        state="blocked",
        error="recovery_required",
    )
    publish(
        ledger.spool / "receipts" / f"{deploy.operation_id}.json",
        receipt.model_dump_json().encode(),
    )
    ledger.service.reconcile()
    overview = client.get("/dashboard/api/apps").json()
    blocked = [
        item for item in overview["apps"][0]["operations"] if item["state"] == "blocked"
    ]
    assert blocked == [
        {
            "id": str(deploy.operation_id),
            "kind": "deploy",
            "state": "blocked",
            "error": "recovery_required",
            "digest": ledger.digest,
            "reconcile_error": None,
            "reconcile_failed_at": None,
        }
    ]
    result = client.post("/dashboard/api/apps/recover", json=payload, headers=headers)
    assert result.status_code == 200
    assert result.json()["state"] == "queued"


def test_apps_overview_survives_a_rehearsal_whose_authority_went_stale(browser, ledger):
    operation = request(ledger)
    client, accounts = browser
    accounts.revoke_sessions(UUID(ADMIN_USER_ID), 1)
    complete_rehearsal(ledger, operation)
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 2), 2)
    response = client.get("/dashboard/api/apps")
    assert response.status_code == 200
    card = response.json()["apps"][0]
    assert card["operations"] == [
        {
            "id": str(operation.operation_id),
            "kind": "rehearse",
            "state": "failed",
            "error": "authority_changed",
            "digest": ledger.digest,
            "reconcile_error": None,
            "reconcile_failed_at": None,
        }
    ]
    assert card["pending"]["digest"] == ledger.digest
    assert card["pending"]["step"] == "compatibility_failed"
    assert card["pending"]["status"] == (
        "Preview ready · all checks passed · compatibility check failed: "
        "authority changed"
    )


def test_preview_checks_eligibility_and_idleness_before_evicting(
    browser, ledger, monkeypatch, tmp_path
):
    evicted = []

    class FakePreviews:
        def __init__(self, database, *_):
            self.database = database

        async def evict(self, attempt):
            evicted.append(attempt)

        async def preview(self, app_id, digest):
            return SimpleNamespace(state="installing")

    token_file = tmp_path / "token"
    token_file.write_text("x" * 40)
    monkeypatch.setenv("HOME_PLATFORM_PREVIEW_ORIGIN", "https://staging.test")
    monkeypatch.setenv("HOME_PLATFORM_PREVIEW_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(dashboard_apps, "PreviewService", FakePreviews)
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    token = client.get("/dashboard/api/apps").json()["csrf"]
    headers = {"Origin": "https://platform.test", "X-CSRF-Token": token}

    def preview(digest):
        return client.post(
            "/dashboard/api/apps/preview",
            json=dict(app_id="notes", digest=digest, request_id=str(uuid4())),
            headers=headers,
        ).status_code

    assert preview("e" * 64) == 409
    newer = push(ledger, monkeypatch, source_commit="c" * 40)
    deploy = deploy_request(ledger)
    assert preview(newer) == 409
    assert evicted == []
    with ledger.database.connect() as connection:
        assert read_authority(connection, deploy.operation_id, "a" * 64)


def test_pending_candidate_stays_on_the_previewed_release_after_a_newer_push(
    browser, ledger, monkeypatch
):
    operation = request(ledger)
    complete_rehearsal(ledger, operation)
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    newer = push(ledger, monkeypatch, source_commit="c" * 40)
    pending = client.get("/dashboard/api/apps").json()["apps"][0]["pending"]
    assert pending["digest"] == ledger.digest
    assert pending["preview_state"] == "ready"
    assert pending["rehearsal_id"] == str(operation.operation_id)
    assert pending["step"] == "approve"
    assert pending["status"] == (
        "Preview ready · all checks passed · compatibility passed"
    )
    ledger.service.reject("notes", ledger.digest, UUID(ADMIN_USER_ID), 1)
    pending = client.get("/dashboard/api/apps").json()["apps"][0]["pending"]
    assert pending["digest"] == newer
    assert pending["rehearsal_id"] is None
    assert (pending["step"], pending["status"]) == ("preview", "Not previewed yet")


@pytest.fixture
def narrow(monkeypatch):
    original = test_app_discovery.manifest

    def narrowed():
        value = original()
        value["capabilities"] = ["identity"]
        value["secrets"] = []
        return value

    monkeypatch.setattr(test_app_discovery, "manifest", narrowed)
    return original


def test_installed_card_shows_release_capabilities_and_pending_additions(
    narrow, ledger, browser, monkeypatch
):
    install(ledger)
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    card = client.get("/dashboard/api/apps").json()["apps"][0]
    assert card["installed"] == ledger.digest
    assert card["installed_capabilities"] == ["identity"]
    assert card["installed_secrets"] == []
    monkeypatch.setattr(test_app_discovery, "manifest", narrow)
    newer = push(ledger, monkeypatch)
    pending = client.get("/dashboard/api/apps").json()["apps"][0]["pending"]
    assert pending["digest"] == newer
    assert pending["capabilities"] == ["identity", "jobs"]
    assert pending["added_capabilities"] == ["jobs"]
    assert pending["added_secrets"] == ["provider-key"]


def test_cards_name_releases_by_version_commit_title_and_time(
    ledger, browser, monkeypatch
):
    with ledger.database.connect() as connection:
        connection.execute(
            "UPDATE app_preview_attempts SET result_json = ?",
            (ledger.preview.model_copy(update={"version": "1.0.0"}).model_dump_json(),),
        )
    install(ledger)
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    card = client.get("/dashboard/api/apps").json()["apps"][0]
    assert card["running"] == {
        "digest": ledger.digest,
        "commit": "a" * 40,
        "commit_url": "https://github.com/example/notes/commit/" + "a" * 40,
        "title": None,
        "committed_at": None,
        "version": "1.0.0",
    }
    assert card["pending"] is None
    title = "Add <img src=x onerror=alert(1)> export"
    newer = push(
        ledger,
        monkeypatch,
        facts=(title, "2026-10-14T01:12:30Z"),
        source_commit="c" * 40,
    )
    card = client.get("/dashboard/api/apps").json()["apps"][0]
    assert card["running"]["version"] == "1.0.0"
    assert card["pending"]["release"] == {
        "digest": newer,
        "commit": "c" * 40,
        "commit_url": "https://github.com/example/notes/commit/" + "c" * 40,
        "title": title,
        "committed_at": "2026-10-14T01:12:30Z",
        "version": None,
    }
    assert (card["pending"]["step"], card["pending"]["status"]) == (
        "preview",
        "Not previewed yet",
    )


@pytest.mark.parametrize(
    ("preview_state", "result", "rehearsed", "rehearsal", "expected"),
    [
        (None, None, False, None, ("preview", "Not previewed yet")),
        ("installing", None, False, None, ("previewing", "Preview running…")),
        (
            "failed",
            {"checks": {"readiness": False, "direct_denial": False, "image": True}},
            False,
            None,
            (
                "preview_failed",
                "Preview failed · failed checks: readiness, direct denial",
            ),
        ),
        (
            "failed",
            {"error": "Preview did not become ready"},
            False,
            None,
            ("preview_failed", "Preview failed: Preview did not become ready"),
        ),
        ("failed", {}, False, None, ("preview_failed", "Preview failed")),
        (
            "ready",
            {},
            False,
            None,
            (
                "check_compatibility",
                "Preview ready · all checks passed · compatibility not checked yet",
            ),
        ),
        (
            "ready",
            {},
            False,
            ("queued", None),
            (
                "checking_compatibility",
                "Preview ready · all checks passed · checking compatibility…",
            ),
        ),
        (
            "ready",
            {},
            False,
            ("failed", "compatibility_failed"),
            (
                "compatibility_failed",
                "Preview ready · all checks passed · compatibility check failed: "
                "compatibility failed",
            ),
        ),
        (
            "ready",
            {},
            False,
            ("failed", "records_changed"),
            (
                "compatibility_failed",
                "Preview ready · all checks passed · compatibility check failed: "
                "this release would change stored records",
            ),
        ),
        (
            "ready",
            {},
            False,
            ("failed", "installation_failed"),
            (
                "compatibility_failed",
                "Preview ready · all checks passed · compatibility check failed: "
                "installation failed",
            ),
        ),
        (
            "ready",
            {},
            False,
            ("succeeded", None),
            (
                "check_compatibility",
                "Preview ready · all checks passed · compatibility not checked yet",
            ),
        ),
        (
            "ready",
            {},
            True,
            ("succeeded", None),
            ("approve", "Preview ready · all checks passed · compatibility passed"),
        ),
    ],
)
def test_next_step_names_the_state_and_failures_in_plain_words(
    ledger, preview_state, result, rehearsed, rehearsal, expected
):
    preview = ledger.preview.model_copy(update=result) if result is not None else None
    assert (
        dashboard_apps.next_step(preview_state, preview, rehearsed, rehearsal)
        == expected
    )


def test_preview_url_is_shown_only_for_ready_previews(browser, ledger):
    failed = ledger.preview.model_copy(
        update={"state": "failed", "preview_url": "javascript:alert(1)"}
    )
    with ledger.database.connect() as connection:
        connection.execute(
            "UPDATE app_preview_attempts SET state = 'failed', result_json = ?",
            (failed.model_dump_json(),),
        )
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    pending = client.get("/dashboard/api/apps").json()["apps"][0]["pending"]
    assert pending["preview_state"] == "failed"
    assert pending["preview_url"] is None


def test_card_skips_a_release_its_registration_narrowed_below(browser, ledger):
    with ledger.database.connect() as connection:
        current = registration_for(connection, "notes")
    changed = json.loads(current.model_dump_json())
    changed["capabilities"] = ["identity"]
    update_registration(ledger.database, json.dumps(changed).encode())
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    response = client.get("/dashboard/api/apps")
    assert response.status_code == 200
    (card,) = response.json()["apps"]
    assert card["app_id"] == "notes"
    assert card["error"] is None
    assert card["pending"] is None and card["operations"] == []


def test_overview_renders_other_apps_when_one_apps_records_fail(
    browser, ledger, tmp_path, monkeypatch
):
    ledger.registry.register(
        document(managed("pad", "/apps/pad", state=str(tmp_path / "pad-state")))
    )
    original = dashboard_apps.installed

    def failing(connection, app_id, *args):
        if app_id == "notes":
            raise ContractError("Installed history is unreadable")
        return original(connection, app_id, *args)

    monkeypatch.setattr(dashboard_apps, "installed", failing)
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    response = client.get("/dashboard/api/apps")
    assert response.status_code == 200
    cards = {card["app_id"]: card for card in response.json()["apps"]}
    assert set(cards) == {"notes", "pad"}
    assert cards["notes"]["error"] == "Installed history is unreadable"
    assert cards["pad"]["error"] is None
    assert cards["pad"]["label"] == "Notes <test>" and cards["pad"]["settings"]


def test_preview_requests_for_one_app_never_overlap(
    browser, ledger, monkeypatch, tmp_path
):
    inflight = {"now": 0, "peak": 0}

    class FakePreviews:
        def __init__(self, database, *_):
            pass

        async def evict(self, attempt):
            pass

        async def preview(self, app_id, digest):
            inflight["now"] += 1
            inflight["peak"] = max(inflight["peak"], inflight["now"])
            await asyncio.sleep(0.2)
            inflight["now"] -= 1
            return SimpleNamespace(state="installing")

    token_file = tmp_path / "token"
    token_file.write_text("x" * 40)
    monkeypatch.setenv("HOME_PLATFORM_PREVIEW_ORIGIN", "https://staging.test")
    monkeypatch.setenv("HOME_PLATFORM_PREVIEW_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(dashboard_apps, "PreviewService", FakePreviews)
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    token = client.get("/dashboard/api/apps").json()["csrf"]
    results = []

    def click():
        results.append(
            client.post(
                "/dashboard/api/apps/preview",
                json=dict(
                    app_id="notes", digest=ledger.digest, request_id=str(uuid4())
                ),
                headers={"Origin": "https://platform.test", "X-CSRF-Token": token},
            ).json()
        )

    clicks = [threading.Thread(target=click) for _ in range(2)]
    for thread in clicks:
        thread.start()
    for thread in clicks:
        thread.join()
    assert results == [{"state": "installing"}, {"state": "installing"}]
    assert inflight["peak"] == 1


def test_persistent_reconcile_failure_is_shown_on_the_card_until_it_clears(
    browser, ledger
):
    operation = request(ledger)
    receipt = ledger.spool / "receipts" / f"{operation.operation_id}.json"
    publish(
        receipt,
        Receipt(
            operation_id=operation.operation_id,
            selection=operation.approval.selection.model_copy(update={"generation": 7}),
            state="succeeded",
        )
        .model_dump_json()
        .encode(),
    )
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    ledger.service.reconcile()
    (shown,) = client.get("/dashboard/api/apps").json()["apps"][0]["operations"]
    assert shown["state"] == "queued"
    assert shown["reconcile_error"] == "Installer receipt does not match operation"
    assert shown["reconcile_failed_at"]
    receipt.unlink()
    ledger.service.reconcile()
    (shown,) = client.get("/dashboard/api/apps").json()["apps"][0]["operations"]
    assert shown["state"] == "queued"
    assert shown["reconcile_error"] is None and shown["reconcile_failed_at"] is None


def test_overview_never_reconciles_and_renders_an_unreadable_registration(
    browser, ledger, tmp_path
):
    ledger.registry.register(
        document(managed("pad", "/apps/pad", state=str(tmp_path / "pad-state")))
    )
    operation = request(ledger)
    publish(
        ledger.spool / "receipts" / f"{operation.operation_id}.json",
        Receipt(
            operation_id=operation.operation_id,
            selection=operation.approval.selection,
            state="failed",
            error="installation_failed",
        )
        .model_dump_json()
        .encode(),
    )
    with ledger.database.connect() as connection:
        connection.execute(
            "UPDATE app_registrations SET definition_json = '{' WHERE app_id = 'pad'"
        )
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    response = client.get("/dashboard/api/apps")
    assert response.status_code == 200
    cards = {card["app_id"]: card for card in response.json()["apps"]}
    assert set(cards) == {"notes", "pad"}
    assert cards["pad"]["label"] == "pad"
    assert cards["pad"]["error"] == "Invalid JSON contract document"
    (shown,) = cards["notes"]["operations"]
    assert shown["state"] == "queued", "the overview consumed a receipt"
    ledger.service.reconcile()
    (shown,) = client.get("/dashboard/api/apps").json()["apps"][0]["operations"]
    assert (shown["state"], shown["error"]) == ("failed", "installation_failed")


def test_apps_page_renders_card_errors_and_reconcile_failures(browser):
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    page = client.get("/dashboard/apps").text
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in page
    for field in ("app.error", "operation.reconcile_error", "reconcile_failed_at"):
        assert field in page


def raised_memory(registration):
    value = json.loads(registration.model_dump_json())
    value["bindings"]["resources"]["memory_bytes"] = 512 * 1024 * 1024
    value["bindings"]["settings"].append({"name": "NOTES_PAGE_SIZE", "value": "50"})
    return json.dumps(value).encode()


def test_card_offers_an_installed_settings_change_as_its_one_decision(browser, ledger):
    first = install(ledger)
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    assert client.get("/dashboard/api/apps").json()["apps"][0]["pending"] is None
    update_registration(ledger.database, raised_memory(first.registration))
    with ledger.database.connect() as connection:
        proposal = proposed_registration(connection, "notes")
    card = client.get("/dashboard/api/apps").json()["apps"][0]
    assert card["installed"] == ledger.digest
    pending = card["pending"]
    assert pending["digest"] == ledger.digest
    assert pending["settings_change"] == [
        "memory 256 → 512 MiB",
        "adds setting NOTES_PAGE_SIZE = 50",
    ]
    assert pending["registration_sha256"] == fingerprint(proposal)
    assert pending["step"] == "check_compatibility"
    assert pending["policy_mismatch"] is False
    ledger.service.policy_registration = lambda _: first.registration
    assert client.get("/dashboard/api/apps").json()["apps"][0]["pending"][
        "policy_mismatch"
    ]
    ledger.service.policy_registration = lambda _: proposal
    rehearsal = request(ledger)
    complete_rehearsal(ledger, rehearsal)
    pending = client.get("/dashboard/api/apps").json()["apps"][0]["pending"]
    assert pending["policy_mismatch"] is False
    assert (pending["step"], pending["rehearsal_id"]) == (
        "approve",
        str(rehearsal.operation_id),
    )


def test_card_rejects_the_settings_change_it_showed(browser, ledger):
    first = install(ledger)
    update_registration(ledger.database, raised_memory(first.registration))
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    data = client.get("/dashboard/api/apps").json()
    pending = data["apps"][0]["pending"]
    headers = {"Origin": "https://platform.test", "X-CSRF-Token": data["csrf"]}
    payload = dict(app_id="notes", digest=pending["digest"], request_id=str(uuid4()))
    stale = client.post("/dashboard/api/apps/reject", json=payload, headers=headers)
    assert stale.status_code == 409
    payload["registration_sha256"] = pending["registration_sha256"]
    result = client.post("/dashboard/api/apps/reject", json=payload, headers=headers)
    assert result.json() == {"state": "rejected"}
    card = client.get("/dashboard/api/apps").json()["apps"][0]
    assert card["pending"] is None and card["installed"] == ledger.digest


def test_settings_change_rides_with_a_pending_release(browser, ledger, monkeypatch):
    first = install(ledger)
    update_registration(ledger.database, raised_memory(first.registration))
    newer = push(ledger, monkeypatch, source_commit="c" * 40)
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    pending = client.get("/dashboard/api/apps").json()["apps"][0]["pending"]
    assert pending["digest"] == newer
    assert pending["settings_change"][0] == "memory 256 → 512 MiB"
    page = client.get("/dashboard/apps").text
    assert "Settings change, applied with this update:" in page


def test_check_compatibility_refuses_a_settings_change_the_card_did_not_show(
    browser, ledger, monkeypatch, tmp_path
):
    class ReadyPreviews:
        def __init__(self, *_):
            pass

        async def preview(self, app_id, digest):
            return SimpleNamespace(state="ready", attempt_id=ledger.preview.attempt_id)

    token_file = tmp_path / "token"
    token_file.write_text("x" * 40)
    monkeypatch.setenv("HOME_PLATFORM_PREVIEW_ORIGIN", "https://staging.test")
    monkeypatch.setenv("HOME_PLATFORM_PREVIEW_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(dashboard_apps, "PreviewService", ReadyPreviews)
    first = install(ledger)
    update_registration(ledger.database, raised_memory(first.registration))
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    data = client.get("/dashboard/api/apps").json()
    shown = data["apps"][0]["pending"]
    replaced = json.loads(raised_memory(first.registration))
    replaced["bindings"]["resources"]["memory_bytes"] = 4096 * 1024 * 1024
    update_registration(ledger.database, json.dumps(replaced).encode())
    headers = {"Origin": "https://platform.test", "X-CSRF-Token": data["csrf"]}

    def check(registration_sha256):
        return client.post(
            "/dashboard/api/apps/preview",
            json=dict(
                app_id="notes",
                digest=shown["digest"],
                preview_id=shown["preview_id"],
                registration_sha256=registration_sha256,
                request_id=str(uuid4()),
            ),
            headers=headers,
        )

    assert check(shown["registration_sha256"]).status_code == 409
    assert check(None).status_code == 409
    with ledger.database.connect() as connection:
        kinds = [
            row[0] for row in connection.execute("SELECT kind FROM app_operations")
        ]
    assert kinds == ["rehearse", "deploy"], "a rehearsal of an unseen change queued"
    current = client.get("/dashboard/api/apps").json()["apps"][0]["pending"]
    assert current["registration_sha256"] != shown["registration_sha256"]
    assert check(current["registration_sha256"]).json()["state"] == "queued"


def test_card_shows_old_and_new_binding_values_as_text(browser, ledger):
    first = install(ledger)
    value = json.loads(raised_memory(first.registration))
    bindings = value["bindings"]
    bindings["settings"] = [
        {"name": "NOTES_THEME", "value": "<b>light</b>"},
        {"name": "NOTES_BANNER", "value": "x" * 100},
    ]
    bindings["secret_files"] = [
        {"name": "provider-key", "file_path": "/private/rotated-key"}
    ]
    bindings["backup_directory"] = "/private/backups"
    bindings["integration_bindings"] = ["library"]
    update_registration(ledger.database, json.dumps(value).encode())
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    pending = client.get("/dashboard/api/apps").json()["apps"][0]["pending"]
    banner = "x" * 39 + "…"
    assert pending["settings_change"] == [
        "memory 256 → 512 MiB",
        f"adds setting NOTES_BANNER = {banner}",
        "changes setting NOTES_THEME dark → <b>light</b>",
        "changes secret file provider-key /private/test-key → /private/rotated-key",
        "adds integration library",
        "moves backups none → /private/backups",
    ]
    assert pending["proposed_bindings"] == [
        [
            "Proposed resources",
            "512 MiB memory · 0.5 CPU · 128 processes · 300 s maintenance",
        ],
        ["Proposed settings", f"NOTES_BANNER={banner}, NOTES_THEME=<b>light</b>"],
        ["Proposed secret files", "provider-key at /private/rotated-key"],
        ["Proposed backups", "/private/backups"],
        ["Proposed integrations", "library"],
    ]
    page = client.get("/dashboard/apps").text
    assert "pending.proposed_bindings" in page
    assert "for(const item of pending.settings_change)" in page


def test_card_predicts_policy_refusals_before_the_administrator_clicks(browser, ledger):
    first = install(ledger)
    update_registration(ledger.database, raised_memory(first.registration))
    with ledger.database.connect() as connection:
        proposal = proposed_registration(connection, "notes")
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))

    def shown(policy):
        ledger.service.policy_registration = lambda _: policy
        return client.get("/dashboard/api/apps").json()["apps"][0]

    card = shown(first.registration)
    assert card["pending"]["policy_mismatch"] is True
    assert card["pending"]["status"] == (
        "Update this app's host policy entry to match this settings change, "
        "then check compatibility."
    )
    assert card["restart_notice"] is None
    card = shown(proposal)
    assert card["pending"]["status"] == (
        "Preview ready · all checks passed · compatibility not checked yet"
    )
    assert card["restart_notice"] == (
        "Its host policy entry already has the settings change, so the running "
        "app cannot restart until that change is deployed."
    )
    rehearsal = request(ledger)
    complete_rehearsal(ledger, rehearsal)
    deploy = request(ledger, "deploy", rehearsal_id=rehearsal.operation_id)
    accept(ledger, deploy, 2)
    card = shown(proposal)
    assert card["restart_notice"] is None
    assert card["rollback"]["policy_mismatch"] is True
    assert card["rollback"]["notice"] == (
        "Rollback needs this app's host policy entry returned to the previous "
        "registration first."
    )
    card = shown(first.registration)
    assert card["rollback"] == {
        "digest": first.selection.manifest_sha256,
        "preview_id": str(first.selection.preview_id),
        "policy_mismatch": False,
        "notice": None,
    }
    assert card["restart_notice"] == (
        "Its host policy entry no longer matches the installed registration, so "
        "the running app cannot restart until they match again."
    )
    page = client.get("/dashboard/apps").text
    assert "pending.policy_mismatch" in page and "app.rollback.policy_mismatch" in page


def test_recover_waits_for_the_policy_registration_to_be_reverted(browser, ledger):
    first = install(ledger)
    update_registration(ledger.database, raised_memory(first.registration))
    with ledger.database.connect() as connection:
        proposal = proposed_registration(connection, "notes")
    rehearsal = request(ledger)
    complete_rehearsal(ledger, rehearsal)
    deploy = request(ledger, "deploy", rehearsal_id=rehearsal.operation_id)
    switch_request(ledger, deploy)
    publish(
        ledger.spool / "receipts" / f"{deploy.operation_id}.json",
        Receipt(
            operation_id=deploy.operation_id,
            selection=deploy.approval.selection,
            state="blocked",
            error="recovery_required",
        )
        .model_dump_json()
        .encode(),
    )
    ledger.service.reconcile()
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))

    def shown(policy):
        ledger.service.policy_registration = lambda _: policy
        return client.get("/dashboard/api/apps").json()["apps"][0]

    card = shown(proposal)
    assert card["recover_notice"] == (
        "Recover needs this app's host policy registration reverted to the "
        "accepted registration first."
    )
    assert card["restart_notice"] == (
        "Its host policy registration is not the installed one, so the app "
        "cannot restart until that registration is reverted."
    )
    card = shown(first.registration)
    assert (card["recover_notice"], card["restart_notice"]) == (None, None)
    page = client.get("/dashboard/apps").text
    assert (
        "button(secondary,'Recover','recover',blockedOperation,"
        "Boolean(app.recover_notice)||sending!==undefined"
    ) in page


def test_a_policy_entry_listing_bindings_in_another_order_is_told_to_reorder(
    browser, ledger
):
    first = install(ledger)
    value = json.loads(raised_memory(first.registration))
    value["bindings"]["settings"].append({"name": "NOTES_A", "value": "1"})
    value["bindings"]["secret_files"].insert(
        0, {"name": "zz-key", "file_path": "/private/zz-key"}
    )
    value["bindings"]["integration_bindings"] = ["library", "archive"]
    update_registration(ledger.database, json.dumps(value).encode())
    with ledger.database.connect() as connection:
        proposal = json.loads(
            proposed_registration(connection, "notes").model_dump_json()
        )
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))

    def shown(entry):
        policy = type(first.registration).model_validate_json(json.dumps(entry))
        ledger.service.policy_registration = lambda _: policy
        return policy, client.get("/dashboard/api/apps").json()["apps"][0]

    for key in ("settings", "secret_files", "integration_bindings"):
        entry = json.loads(json.dumps(proposal))
        entry["bindings"][key].reverse()
        policy, card = shown(entry)
        assert fingerprint(policy) != card["pending"]["registration_sha256"]
        assert card["pending"]["policy_mismatch"] is True
        assert card["pending"]["status"] == (
            "Update this app's host policy entry to match this settings change, "
            "then check compatibility. Only the order differs: list its "
            "settings, secret files and integration bindings in the same order "
            "as the registration."
        )
    entry["bindings"]["settings"][0]["value"] = "2"
    _, card = shown(entry)
    assert card["pending"]["status"] == (
        "Update this app's host policy entry to match this settings change, "
        "then check compatibility."
    )


def test_a_failed_rehearsal_of_a_replaced_change_is_not_the_cards_status(
    browser, ledger
):
    first = install(ledger)
    update_registration(ledger.database, raised_memory(first.registration))
    rehearsal = request(ledger)
    publish(
        ledger.spool / "receipts" / f"{rehearsal.operation_id}.json",
        Receipt(
            operation_id=rehearsal.operation_id,
            selection=rehearsal.approval.selection,
            state="failed",
            error="compatibility_failed",
        )
        .model_dump_json()
        .encode(),
    )
    ledger.service.reconcile()
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    pending = client.get("/dashboard/api/apps").json()["apps"][0]["pending"]
    assert pending["step"] == "compatibility_failed"
    value = json.loads(raised_memory(first.registration))
    value["bindings"]["resources"]["memory_bytes"] = 384 * 1024 * 1024
    update_registration(ledger.database, json.dumps(value).encode())
    pending = client.get("/dashboard/api/apps").json()["apps"][0]["pending"]
    assert (pending["step"], pending["status"]) == (
        "check_compatibility",
        "Preview ready · all checks passed · compatibility not checked yet",
    )


def with_extra_key(registration, path):
    value = json.loads(registration.model_dump_json())
    value["bindings"]["secret_files"].append(
        {"name": "extra-key", "file_path": str(path)}
    )
    return json.dumps(value).encode()


def test_a_rollback_across_an_added_secret_leaves_the_card_usable(
    browser, ledger, monkeypatch, tmp_path
):
    first = install(ledger)
    extra = with_extra_key(first.registration, tmp_path / "extra/key")
    assert update_registration(ledger.database, extra) == "proposed"
    rehearsal = request(ledger)
    complete_rehearsal(ledger, rehearsal)
    accept(ledger, request(ledger, "deploy", rehearsal_id=rehearsal.operation_id), 2)
    newer = push(
        ledger,
        monkeypatch,
        secrets=["provider-key", "extra-key"],
        source_commit="c" * 40,
    )
    rollback = accept(ledger, request(ledger, "rollback"), 3)
    assert rollback.registration == first.registration
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    data = client.get("/dashboard/api/apps").json()
    card = data["apps"][0]
    assert card["error"] is None
    assert (card["installed"], card["pending"]) == (ledger.digest, None)

    assert update_registration(ledger.database, extra) == "proposed"
    pending = client.get("/dashboard/api/apps").json()["apps"][0]["pending"]
    assert pending["digest"] == newer
    assert pending["added_secrets"] == ["extra-key"]
    result = client.post(
        "/dashboard/api/apps/reject",
        json=dict(
            app_id="notes",
            digest=newer,
            request_id=str(uuid4()),
            registration_sha256=pending["registration_sha256"],
        ),
        headers={"Origin": "https://platform.test", "X-CSRF-Token": data["csrf"]},
    )
    assert result.json() == {"state": "rejected"}


def test_card_never_offers_a_release_older_than_the_installed_one(
    browser, ledger, monkeypatch, tmp_path
):
    first = install(ledger)
    deploy = propose_release_with_secret(
        ledger, monkeypatch, first.registration, tmp_path / "extra/key"
    )
    accept(ledger, deploy, 2)
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    card = client.get("/dashboard/api/apps").json()["apps"][0]
    assert (card["error"], card["pending"]) == (None, None)


def test_a_stale_preview_the_registration_no_longer_admits_is_skipped(
    browser, ledger, monkeypatch, tmp_path
):
    class Previews:
        def __init__(self, *_):
            pass

        async def evict(self, attempt):
            raise AssertionError("the stale preview is the one requested")

        async def preview(self, app_id, digest):
            return SimpleNamespace(state="installing")

    token_file = tmp_path / "token"
    token_file.write_text("x" * 40)
    monkeypatch.setenv("HOME_PLATFORM_PREVIEW_ORIGIN", "https://staging.test")
    monkeypatch.setenv("HOME_PLATFORM_PREVIEW_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(dashboard_apps, "PreviewService", Previews)
    first = install(ledger)
    second = accept(
        ledger,
        propose_release_with_secret(
            ledger, monkeypatch, first.registration, tmp_path / "extra/key"
        ),
        2,
    )
    newer = second.selection.manifest_sha256
    accept(ledger, request(ledger, "rollback"), 3)
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    data = client.get("/dashboard/api/apps").json()
    card = data["apps"][0]
    assert card["error"] is None
    assert card["pending"] is None
    assert card["rollback"]["digest"] == newer
    headers = {"Origin": "https://platform.test", "X-CSRF-Token": data["csrf"]}

    def preview():
        return client.post(
            "/dashboard/api/apps/preview",
            json=dict(app_id="notes", digest=newer, request_id=str(uuid4())),
            headers=headers,
        )

    assert preview().status_code == 409
    proposal = second.registration.model_dump_json().encode()
    assert update_registration(ledger.database, proposal) == "proposed"
    pending = client.get("/dashboard/api/apps").json()["apps"][0]["pending"]
    assert (pending["digest"], pending["preview_state"]) == (newer, "ready")
    assert preview().json() == {"state": "installing"}


@pytest.fixture
def staging(browser, monkeypatch, tmp_path):
    """Preview through a staging stand-in that records what core evicts."""
    evicted = []

    class Previews:
        def __init__(self, *_):
            pass

        async def evict(self, attempt):
            evicted.append(attempt)

        async def preview(self, app_id, digest):
            return SimpleNamespace(state="installing")

    token_file = tmp_path / "token"
    token_file.write_text("x" * 40)
    monkeypatch.setenv("HOME_PLATFORM_PREVIEW_ORIGIN", "https://staging.test")
    monkeypatch.setenv("HOME_PLATFORM_PREVIEW_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(dashboard_apps, "PreviewService", Previews)
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    headers = {
        "Origin": "https://platform.test",
        "X-CSRF-Token": client.get("/dashboard/api/apps").json()["csrf"],
    }

    def preview(app_id, digest):
        return client.post(
            "/dashboard/api/apps/preview",
            json=dict(app_id=app_id, digest=digest, request_id=str(uuid4())),
            headers=headers,
        ).json()

    return SimpleNamespace(preview=preview, evicted=evicted)


def other_app(ledger, monkeypatch, tmp_path):
    ledger.registry.register(
        document(managed("other", "/apps/other", state=str(tmp_path / "other")))
    )
    monkeypatch.setattr(dashboard_apps, "eligible", lambda *_: None)
    return "d" * 64


def test_preview_frees_the_staging_slot_of_an_installed_release(
    staging, ledger, monkeypatch, tmp_path
):
    digest = other_app(ledger, monkeypatch, tmp_path)
    assert staging.preview("other", digest) == {"state": "installing"}
    assert staging.evicted == []
    install(ledger)
    assert staging.preview("other", digest) == {"state": "installing"}
    assert staging.evicted == [ledger.preview.attempt_id]


def test_preview_frees_the_staging_slot_of_a_rejected_release(
    staging, ledger, monkeypatch, tmp_path
):
    digest = other_app(ledger, monkeypatch, tmp_path)
    ledger.service.reject("notes", ledger.digest, UUID(ADMIN_USER_ID), 1)
    assert staging.preview("other", digest) == {"state": "installing"}
    assert staging.evicted == [ledger.preview.attempt_id]


def test_preview_again_replaces_a_failed_attempt(staging, ledger):
    with ledger.database.connect() as connection:
        connection.execute("UPDATE app_preview_attempts SET state = 'failed'")
    assert staging.preview("notes", ledger.digest) == {"state": "installing"}
    assert staging.evicted == [ledger.preview.attempt_id]


def test_preview_evicts_the_oldest_idle_preview_when_staging_is_full(
    staging, ledger, monkeypatch, tmp_path
):
    digest = other_app(ledger, monkeypatch, tmp_path)
    ledger.registry.register(
        document(managed("third", "/apps/third", state=str(tmp_path / "third")))
    )
    third = uuid4()
    with ledger.database.connect() as connection:
        connection.execute(
            "INSERT INTO app_eligible_releases(app_id,manifest_sha256,release_id,"
            "artifact_id,artifact_sha256,archive) VALUES (?,?,?,?,?,?)",
            ("third", "f" * 64, 9, 44, "0" * 64, b"archive"),
        )
        connection.execute(
            "INSERT INTO app_preview_attempts VALUES (?,?,?,?,?,?)",
            (str(third), "third", "f" * 64, "0" * 64, "ready", None),
        )
    request(ledger)
    assert staging.preview("other", digest) == {"state": "installing"}
    assert staging.evicted == [third]
    staging.evicted.clear()
    with ledger.database.connect() as connection:
        connection.execute("UPDATE app_operations SET state = 'failed'")
    assert staging.preview("other", digest) == {"state": "installing"}
    assert staging.evicted == [ledger.preview.attempt_id]


def facts(kind, state, error=None, reconcile_error=None):
    failed_at = "2026-10-27 12:00:00" if reconcile_error else None
    return dashboard_apps.OperationFacts(kind, state, error, reconcile_error, failed_at)


@pytest.mark.parametrize(
    ("operations", "previewing", "expected"),
    [
        ([], False, None),
        ([facts("deploy", "succeeded")], False, None),
        ([facts("rehearse", "failed", "records_changed")], False, None),
        ([facts("deploy", "queued")], False, ("progress", "Deploying…", True, False)),
        (
            [facts("rollback", "switching")],
            False,
            ("progress", "Rolling back · switching containers…", True, False),
        ),
        ([facts("rehearse", "queued")], False, ("progress", "", True, False)),
        (
            [facts("deploy", "queued", reconcile_error="Receipt is unreadable")],
            False,
            (
                "failed",
                "Deploying · not reconciled since 2026-10-27 12:00:00: "
                "Receipt is unreadable",
                True,
                False,
            ),
        ),
        ([], True, ("progress", "", False, False)),
        (
            [
                facts("recover", "queued"),
                facts("deploy", "blocked", "recovery_required"),
            ],
            False,
            ("progress", "Recovering…", True, False),
        ),
        (
            [
                facts("recover", "failed"),
                facts("deploy", "blocked", "recovery_required"),
            ],
            False,
            ("failed", "Needs recovery: recovery required", True, True),
        ),
        (
            [facts("deploy", "failed", "policy_refused"), facts("deploy", "succeeded")],
            False,
            ("failed", "Last deploy failed: policy refused", False, True),
        ),
        (
            [facts("deploy", "recovered", "installation_failed")],
            False,
            (
                "failed",
                "Last deploy failed: installation failed · previous release restored",
                False,
                True,
            ),
        ),
        ([facts("deploy", "succeeded"), facts("deploy", "failed")], False, None),
    ],
)
def test_activity_names_each_operation_state_and_when_polling_stops(
    operations, previewing, expected
):
    """Catches a status rule that drifts from the old page, or a settled app the
    page would keep polling (``final`` false with nothing running)."""
    found = dashboard_apps.activity(operations, previewing)
    assert (
        None if found is None else (found.tone, found.text, found.active, found.final)
    ) == expected


def test_a_preview_still_installing_does_not_hide_a_blocked_operation():
    """Catches a preview attempt stuck installing answering for the card ahead
    of a blocked operation, which reads as nothing to do and drops Recover: the
    page offers it only for an active, final activity."""
    found = dashboard_apps.activity(
        [facts("deploy", "blocked", "recovery_required")], True
    )
    assert found == dashboard_apps.Activity(
        "failed", "Needs recovery: recovery required", True, True
    )


@pytest.mark.parametrize(
    ("operations", "retired", "expected"),
    [
        ([facts("retire", "queued")], False, ("progress", "Retiring…", True, False)),
        (
            [facts("retire", "switching")],
            False,
            ("progress", "Retiring · switching containers…", True, False),
        ),
        (
            [facts("retire", "recovered", "backup_failed")],
            False,
            (
                "failed",
                "Last retirement failed: the final archive could not be taken "
                "or verified · app restarted",
                False,
                True,
            ),
        ),
        (
            [facts("retire", "blocked", "installation_failed")],
            False,
            ("failed", "Needs recovery: installation failed", True, True),
        ),
        (
            [facts("deploy", "switching")],
            True,
            ("progress", "Restoring · switching containers…", True, False),
        ),
        (
            [facts("deploy", "failed", "state_missing")],
            True,
            (
                "failed",
                "Last restore failed: the kept database is missing",
                False,
                True,
            ),
        ),
        (
            [facts("deploy", "recovered", "state_changed")],
            True,
            (
                "failed",
                "Last restore failed: the kept database differs from its final "
                "archive · still retired",
                False,
                True,
            ),
        ),
    ],
)
def test_activity_names_retirement_and_restore_in_plain_words(
    operations, retired, expected
):
    """Catches a failed restore told as "previous release restored" while the
    app stays retired, a retirement that restarted the app told as nothing
    changing, and receipt error codes shown to the operator raw."""
    found = dashboard_apps.activity(operations, False, retired)
    assert (
        None if found is None else (found.tone, found.text, found.active, found.final)
    ) == expected


def test_activity_feed_calls_a_deploy_onto_a_retired_app_a_restore(browser, ledger):
    """Catches the feed missing that the app is retired, which would call a
    failed restore a deploy that restored a previous release."""
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    retire(ledger)
    rehearsal = request(ledger)
    complete_rehearsal(ledger, rehearsal)
    deploy = request(ledger, "deploy", rehearsal_id=rehearsal.operation_id)
    switch_request(ledger, deploy)
    feed = client.get("/dashboard/api/apps/activity").json()
    assert feed["notes"]["text"] == "Restoring · switching containers…"
    publish(
        ledger.spool / "receipts" / f"{deploy.operation_id}.json",
        Receipt(
            operation_id=deploy.operation_id,
            selection=deploy.approval.selection,
            state="recovered",
            error="state_changed",
        )
        .model_dump_json()
        .encode(),
    )
    ledger.service.reconcile()
    assert client.get("/dashboard/api/apps/activity").json()["notes"]["text"] == (
        "Last restore failed: the kept database differs from its final archive "
        "· still retired"
    )


def test_activity_words_cover_every_operation_kind():
    """Catches a new operation kind whose card would raise KeyError mid-poll."""
    kinds = set(get_args(Kind))
    assert set(dashboard_apps.PROGRESSIVE) == kinds == set(dashboard_apps.NOUN)


def test_activity_feed_follows_an_operation_until_it_settles(browser, ledger):
    """Catches polling that never starts or never stops: the feed is non-final
    while the installer works and final or empty once its receipt lands."""
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))

    def feed():
        response = client.get("/dashboard/api/apps/activity")
        assert response.status_code == 200
        return response.json()

    assert feed() == {"notes": None}
    rehearsal = request(ledger)
    assert feed()["notes"] == {
        "tone": "progress",
        "text": "",
        "active": True,
        "final": False,
    }
    complete_rehearsal(ledger, rehearsal)
    assert feed() == {"notes": None}
    deploy = request(ledger, "deploy", rehearsal_id=rehearsal.operation_id)
    assert feed()["notes"]["text"] == "Deploying…"
    switch_request(ledger, deploy)
    assert feed()["notes"] == {
        "tone": "progress",
        "text": "Deploying · switching containers…",
        "active": True,
        "final": False,
    }
    publish(
        ledger.spool / "receipts" / f"{deploy.operation_id}.json",
        Receipt(
            operation_id=deploy.operation_id,
            selection=deploy.approval.selection,
            state="failed",
            error="installation_failed",
        )
        .model_dump_json()
        .encode(),
    )
    ledger.service.reconcile()
    settled = {
        "tone": "failed",
        "text": "Last deploy failed: installation failed",
        "active": False,
        "final": True,
    }
    assert feed() == {"notes": settled}
    assert client.get("/dashboard/api/apps").json()["apps"][0]["activity"] == settled


def test_activity_feed_reports_a_running_preview_for_polling(browser, ledger):
    """Catches a reload during a preview that would never refresh the card."""
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    with ledger.database.connect() as connection:
        connection.execute("UPDATE app_preview_attempts SET state = 'installing'")
    assert client.get("/dashboard/api/apps/activity").json()["notes"] == {
        "tone": "progress",
        "text": "",
        "active": False,
        "final": False,
    }


def test_activity_feed_never_verifies_release_packages(browser, ledger, monkeypatch):
    """Catches a poll that re-verifies every release package each few seconds,
    the cost the full overview pays per card."""
    request(ledger)
    calls = []
    original = app_deployments.verify_package

    def counted(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(app_deployments, "verify_package", counted)
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    response = client.get("/dashboard/api/apps/activity")
    assert response.status_code == 200
    assert response.json()["notes"]["active"] is True
    assert calls == []
    client.get("/dashboard/api/apps")
    assert calls, "the overview verifies packages, so the counter must see it"


def test_activity_feed_needs_an_administrator_session(browser):
    client, accounts = browser
    assert client.get("/dashboard/api/apps/activity").status_code == 401
    member = accounts.create(
        UserCreate(username="reader", password="long fixture password")
    )
    login(client, member)
    assert client.get("/dashboard/api/apps/activity").status_code == 403


def test_one_apps_operation_leaves_other_cards_unchanged(browser, ledger, tmp_path):
    """Catches an action on one card that changes another card's document, which
    the page would then re-render, closing its details and dropping focus."""
    ledger.registry.register(
        document(managed("pad", "/apps/pad", state=str(tmp_path / "pad-state")))
    )
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))

    def cards():
        apps = client.get("/dashboard/api/apps").json()["apps"]
        return {item["app_id"]: item for item in apps}

    before = cards()
    request(ledger)
    after = cards()
    assert after["pad"] == before["pad"]
    assert after["notes"]["activity"]["active"] is True
    assert client.get("/dashboard/api/apps/activity").json()["pad"] is None


def test_apps_page_reports_actions_on_the_card_not_the_page_status(browser):
    """Catches action results written to the page-level status line, which
    sends the administrator scrolling to the top to see them."""
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    page = client.get("/dashboard/apps").text
    assert "Refresh to see the result" not in page
    assert page.count("status.textContent") == 1
    assert "function live(" in page.split("status.textContent")[0].rsplit("\n", 1)[1]
    assert "line.setAttribute('role','status');secondary.append(line)" in page
    assert "/dashboard/api/apps/activity" in page


def overview(client):
    data = client.get("/dashboard/api/apps").json()
    return data["csrf"], data["apps"][0]


def test_retire_is_offered_only_for_an_idle_running_app(browser, ledger):
    """Catches Retire on an app with nothing to retire, or beside an operation
    already holding the app, where core would refuse the click."""
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    advertise(ledger)
    card = overview(client)[1]
    assert (card["lifecycle"], card["retire"]) == ("not_installed", None)
    current = install(ledger)
    card = overview(client)[1]
    assert card["lifecycle"] == "running"
    assert card["retire"] == {
        "digest": current.selection.manifest_sha256,
        "preview_id": str(current.selection.preview_id),
        "notice": None,
    }
    rehearsal = request(ledger)
    assert overview(client)[1]["retire"] is None
    complete_rehearsal(ledger, rehearsal)
    assert overview(client)[1]["retire"] is not None
    retire(ledger, current)
    card = overview(client)[1]
    assert (card["lifecycle"], card["retire"]) == ("retired", None)


def queue_compute_run(ledger):
    raw = sqlite3.connect(ledger.database.path)
    with raw:
        raw.execute(
            "INSERT INTO jobs(id,type,parameters_json,status,created_at,"
            "updated_at,group_id,owner_user_id) VALUES "
            "('job-1','app','{}','QUEUED','t','t','group-1',?)",
            (ADMIN_USER_ID,),
        )
        raw.execute(
            "INSERT INTO app_runs(group_id,app_id,owner_user_id,installation_id,"
            "submission_key,payload_sha256,inputs_json) VALUES "
            "('group-1','notes','owner','workload','key-1','p','[]')"
        )
    raw.close()


def test_retire_names_why_it_cannot_run_yet(browser, ledger):
    """Catches an enabled Retire that core or root refuses: a host runtime
    that cannot retire, compute runs still using the app, or a host policy
    entry that no longer matches the installed registration."""
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    current = install(ledger)
    unable = "The host installer runtime must be rebuilt before it can retire."
    assert overview(client)[1]["retire"]["notice"] == unable
    advertise(ledger, kinds=("rehearse", "deploy", "rollback", "recover"))
    assert overview(client)[1]["retire"]["notice"] == unable
    (ledger.spool / "receipts" / "installer.json").unlink()
    advertise(ledger)
    assert overview(client)[1]["retire"]["notice"] is None
    queue_compute_run(ledger)
    assert overview(client)[1]["retire"]["notice"] == (
        "Retire waits for 1 compute run still queued or running."
    )
    raw = sqlite3.connect(ledger.database.path)
    with raw:
        raw.execute("UPDATE jobs SET status = 'COMPLETED'")
    raw.close()
    moved = current.registration.model_copy(update={"route": "/apps/elsewhere"})
    ledger.service.policy_registration = lambda _: moved
    assert overview(client)[1]["retire"]["notice"] == (
        "Retire needs this app's host policy entry to match the installed "
        "registration first."
    )


def test_retire_posts_the_installed_digest_and_queues_a_retirement(
    browser, ledger, monkeypatch
):
    """Catches a Retire button that sends a release other than the running one,
    which core refuses, or an action route that never queues the retirement."""
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    current = install(ledger)
    newer = push(ledger, monkeypatch, source_commit="c" * 40)
    advertise(ledger)
    csrf, card = overview(client)
    headers = {"Origin": "https://platform.test", "X-CSRF-Token": csrf}
    offer = card["retire"]

    def post(digest):
        return client.post(
            "/dashboard/api/apps/retire",
            json=dict(
                app_id="notes",
                digest=digest,
                preview_id=offer["preview_id"],
                request_id=str(uuid4()),
            ),
            headers=headers,
        )

    refused = post(newer)
    assert (refused.status_code, refused.json()["detail"]) == (
        409,
        "Retire target changed",
    )
    result = post(offer["digest"])
    assert result.status_code == 200
    assert result.json()["state"] == "queued"
    with ledger.database.connect() as connection:
        kind, operation_json = connection.execute(
            "SELECT kind, operation_json FROM app_operations WHERE operation_id = ?",
            (result.json()["operation_id"],),
        ).fetchone()
    selected = json.loads(operation_json)["approval"]["selection"]
    assert kind == "retire"
    assert selected["manifest_sha256"] == current.selection.manifest_sha256
    assert overview(client)[1]["activity"]["text"] == "Retiring…"


def test_retired_card_shows_what_last_ran_and_its_final_archive(browser, ledger):
    """Catches a retired card that reads as never installed, or loses the
    archive digests an off-host copy is checked against."""
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    current = install(ledger)
    operation = retire_with_archive(ledger, current)
    card = overview(client)[1]
    assert (card["lifecycle"], card["installed"], card["running"]) == (
        "retired",
        None,
        None,
    )
    retired = card["retired"]
    assert retired["release"]["digest"] == current.selection.manifest_sha256
    assert retired["archive"] == json.loads(ARCHIVE.model_dump_json())
    with ledger.database.connect() as connection:
        (created,) = connection.execute(
            "SELECT created_at FROM app_operations WHERE operation_id = ?",
            (str(operation.operation_id),),
        ).fetchone()
    assert retired["retired_at"] == created.replace(" ", "T") + "Z"


def test_retired_card_offers_restore_through_preview_and_rehearsal(
    browser, ledger, monkeypatch
):
    """Catches a retired app with no way back: its own release is offered for
    restore and a newer one ahead of it, each through compatibility first."""
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    current = install(ledger)
    retire(ledger, current)
    pending = overview(client)[1]["pending"]
    assert pending["digest"] == current.selection.manifest_sha256
    assert pending["step"] == "check_compatibility"
    assert pending["added_capabilities"] == []
    rehearsal = request(ledger)
    complete_rehearsal(ledger, rehearsal)
    pending = overview(client)[1]["pending"]
    assert (pending["step"], pending["rehearsal_id"]) == (
        "approve",
        str(rehearsal.operation_id),
    )
    push(ledger, monkeypatch, source_commit="c" * 40)
    pending = overview(client)[1]["pending"]
    assert (pending["digest"], pending["step"]) == (
        current.selection.manifest_sha256,
        "approve",
    ), "a newer push took the card from a restore already checked"


def test_retired_card_offers_a_newer_release_ahead_of_the_retired_one(
    browser, ledger, monkeypatch
):
    """Catches the preview a retired release was installed from holding the
    card, so a newer release could never be restored without rejecting it."""
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    retire(ledger)
    newer = push(ledger, monkeypatch, source_commit="c" * 40)
    pending = overview(client)[1]["pending"]
    assert (pending["digest"], pending["step"]) == (newer, "preview")


def test_a_failed_restore_check_leaves_a_newer_release_ahead_on_a_retired_card(
    browser, ledger, monkeypatch
):
    """Catches a failed compatibility check of the retired release, such as
    one refused because the kept state changed, holding the card over a newer
    release as if the restore had been checked."""
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    retire(ledger)
    rehearsal = request(ledger)
    publish(
        ledger.spool / "receipts" / f"{rehearsal.operation_id}.json",
        Receipt(
            operation_id=rehearsal.operation_id,
            selection=rehearsal.approval.selection,
            state="failed",
            error="state_changed",
        )
        .model_dump_json()
        .encode(),
    )
    ledger.service.reconcile()
    newer = push(ledger, monkeypatch, source_commit="c" * 40)
    pending = overview(client)[1]["pending"]
    assert (pending["digest"], pending["step"]) == (newer, "preview")


def test_no_rollback_is_offered_while_retired(browser, ledger, monkeypatch, tmp_path):
    """Catches Rollback on a retired card, which would start the release before
    the retired one without the preview and rehearsal a restore needs."""
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    first = install(ledger)
    second = accept(
        ledger,
        propose_release_with_secret(
            ledger, monkeypatch, first.registration, tmp_path / "extra/key"
        ),
        2,
    )
    assert overview(client)[1]["rollback"] is not None
    retire(ledger, second)
    card = overview(client)[1]
    assert (card["lifecycle"], card["rollback"]) == ("retired", None)


def test_apps_page_confirms_retire_inside_the_card(browser):
    """Catches a Retire button that posts on its first click, a confirmation
    on a timer, or a retired card worded as a fresh deploy."""
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    page = client.get("/dashboard/apps").text
    retire = page.split("if(app.retire){", 1)[1].split("}", 1)[0]
    assert "retire.onclick=()=>confirmRetire(app,!confirm)" in retire
    assert "button(row,'Retire '+app.label,'retire',app.retire" in page
    assert "button(row,'Cancel'" in page
    assert (
        "setTimeout" not in page.split("function confirmRetire", 1)[1].split("\n", 1)[0]
    )
    assert "'Approve and restore'" in page
    assert "['RESTORE READY','yellow'" in page
    assert "['RETIRED','muted'" in page

import asyncio
import json
import threading
import time
from types import SimpleNamespace
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
    deploy_request,
    install,
    propose_release_with_secret,
    switch_request,
)

from app import dashboard_apps
from app.accounts import AccountStore, UserCreate, encode_session
from app.app_contracts import ContractError
from app.app_deployment_contracts import Receipt, fingerprint
from app.app_deployments import (
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


def test_preview_checks_eligibility_and_idleness_before_retiring(
    browser, ledger, monkeypatch, tmp_path
):
    retired = []

    class FakePreviews:
        def __init__(self, database, *_):
            self.database = database

        async def retire(self, attempt):
            retired.append(attempt)

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
    assert retired == []
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

        async def retire(self, attempt):
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
        "button(secondary,'Recover','recover',blocked,Boolean(app.recover_notice))"
        in (page)
    )


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

        async def retire(self, attempt):
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
    """Preview through a staging stand-in that records what core retires."""
    retired = []

    class Previews:
        def __init__(self, *_):
            pass

        async def retire(self, attempt):
            retired.append(attempt)

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

    return SimpleNamespace(preview=preview, retired=retired)


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
    assert staging.retired == []
    install(ledger)
    assert staging.preview("other", digest) == {"state": "installing"}
    assert staging.retired == [ledger.preview.attempt_id]


def test_preview_frees_the_staging_slot_of_a_rejected_release(
    staging, ledger, monkeypatch, tmp_path
):
    digest = other_app(ledger, monkeypatch, tmp_path)
    ledger.service.reject("notes", ledger.digest, UUID(ADMIN_USER_ID), 1)
    assert staging.preview("other", digest) == {"state": "installing"}
    assert staging.retired == [ledger.preview.attempt_id]


def test_preview_again_replaces_a_failed_attempt(staging, ledger):
    with ledger.database.connect() as connection:
        connection.execute("UPDATE app_preview_attempts SET state = 'failed'")
    assert staging.preview("notes", ledger.digest) == {"state": "installing"}
    assert staging.retired == [ledger.preview.attempt_id]


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
    assert staging.retired == [third]
    staging.retired.clear()
    with ledger.database.connect() as connection:
        connection.execute("UPDATE app_operations SET state = 'failed'")
    assert staging.preview("other", digest) == {"state": "installing"}
    assert staging.retired == [ledger.preview.attempt_id]

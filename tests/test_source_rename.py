import asyncio
import os
from uuid import UUID, uuid4

import httpx
import pytest
from test_app_deployments import complete_rehearsal, request
from test_app_deployments import ledger as ledger_fixture
from test_app_discovery import GitHubFixture, package
from test_dashboard_apps import browser as browser_fixture
from test_dashboard_apps import login, overview
from test_installer_recovery import (
    accept,
    block,
    boot_view,
    container,
    install,
    settings_change,
)
from test_managed_installer import forged_authority, installed_by_root, retain
from test_managed_installer import helper as helper_fixture

from app.app_contracts import ContractError
from app.app_deployment_contracts import InstalledSnapshot, fingerprint
from app.app_deployments import (
    eligible,
    pending_change,
    proposed_registration,
    read_authority,
    update_registration,
)
from app.app_discovery import ReleaseDiscovery
from app.app_github import GitHubPublications
from app.app_previews import CHECKS, PreviewResult
from app.app_publication import sha256
from app.identity import ADMIN_USER_ID
from ops.apps import rename_source
from ops.managed_installer import Installer

ledger = ledger_fixture
helper = helper_fixture
browser = browser_fixture
RENAMED = "example/renamed-notes"


def serve(fixture, **refusals):
    """The fixture's GitHub, answering 404 or a redirect where it names."""

    def respond(request):
        status = refusals.get(request.url.path.rsplit("/", 1)[1])
        if status == 301:
            fixture.calls.append(request.url.path)
            return httpx.Response(301, headers={"location": "https://api.github.com"})
        if status is not None:
            fixture.calls.append(request.url.path)
            return httpx.Response(status)
        if not request.url.path.startswith(f"/repos/{fixture.repository}"):
            fixture.calls.append(request.url.path)
            return httpx.Response(301, headers={"location": "https://api.github.com"})
        return fixture.respond(request)

    return httpx.MockTransport(respond)


def prove(ledger, fixture, repository=RENAMED, image_repository=None, **refusals):
    async def run():
        async with httpx.AsyncClient(transport=serve(fixture, **refusals)) as client:
            return await rename_source(
                ledger.database,
                GitHubPublications(client),
                "notes",
                repository,
                image_repository,
            )

    return asyncio.run(run())


def proposals(ledger):
    with ledger.database.connect() as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM app_registration_proposals"
        ).fetchone()[0]


def renamed(registration, repository=RENAMED):
    source = registration.source.model_copy(
        update={"repository": repository, "image_repository": "ghcr.io/" + repository}
    )
    return registration.model_copy(update={"source": source})


def renamed_release(ledger):
    """Retain a release published under the new name, with a ready preview."""
    _, archive, proof = package(RENAMED)
    digest = proof["manifest_sha256"]
    preview = PreviewResult(
        attempt_id=uuid4(),
        app_id="notes",
        manifest_sha256=digest,
        bundle_sha256=sha256(archive),
        state="ready",
        image_id="sha256:" + "c" * 64,
        preview_url="https://preview.example/notes/",
        checks=dict.fromkeys(CHECKS, True),
    )
    with ledger.database.connect() as connection:
        connection.execute("UPDATE app_preview_attempts SET state = 'retired'")
        connection.execute(
            "INSERT INTO app_eligible_releases(app_id,manifest_sha256,release_id,"
            "artifact_id,artifact_sha256,archive) VALUES (?,?,?,?,?,?)",
            ("notes", digest, 8, 43, sha256(archive), archive),
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
    return digest, preview.attempt_id


def rename_deploy(ledger):
    digest, preview_id = renamed_release(ledger)
    rehearsal = request(ledger, digest=digest, preview_id=preview_id)
    complete_rehearsal(ledger, rehearsal)
    deploy = request(
        ledger,
        "deploy",
        digest=digest,
        preview_id=preview_id,
        rehearsal_id=rehearsal.operation_id,
    )
    return digest, deploy


def test_proven_rename_proposes_only_the_new_source_and_keeps_boot_authority(ledger):
    first = install(ledger)
    before = boot_view(ledger)
    assert prove(ledger, GitHubFixture(RENAMED)) == {
        "outcome": "proposed",
        "repository": RENAMED,
        "repository_id": 1001,
    }
    with ledger.database.connect() as connection:
        change = pending_change(connection, "notes", "test")
    assert change.registration == renamed(first.registration)
    assert boot_view(ledger) == before
    assert prove(ledger, GitHubFixture(RENAMED))["outcome"] == "unchanged"
    assert proposals(ledger) == 1


def test_rename_keeps_a_pending_settings_change_and_an_explicit_image(ledger):
    first = install(ledger)
    update_registration(ledger.database, settings_change(first.registration))
    with ledger.database.connect() as connection:
        settings = pending_change(connection, "notes", "test").registration
    prove(ledger, GitHubFixture(RENAMED), image_repository="ghcr.io/example/other")
    with ledger.database.connect() as connection:
        change = pending_change(connection, "notes", "test").registration
    assert change == settings.model_copy(
        update={
            "source": settings.source.model_copy(
                update={
                    "repository": RENAMED,
                    "image_repository": "ghcr.io/example/other",
                }
            )
        }
    )


def test_rename_stores_the_name_github_spells(ledger):
    install(ledger)
    fixture = GitHubFixture(RENAMED)
    fixture.full_name = "Example/Renamed-Notes"
    result = prove(ledger, fixture, repository="example/renamed-notes")
    assert result["repository"] == "Example/Renamed-Notes"
    with ledger.database.connect() as connection:
        source = pending_change(connection, "notes", "test").registration.source
    assert (source.repository, source.image_repository) == (
        "Example/Renamed-Notes",
        "ghcr.io/example/renamed-notes",
    )


@pytest.mark.parametrize(
    "forge,refusals,repository",
    [
        ({"repository": {"id": 5, "full_name": RENAMED}}, {}, RENAMED),
        ({"head_repository": {"id": 5, "full_name": RENAMED}}, {}, RENAMED),
        ({"repository": {"id": 1001, "full_name": "example/notes"}}, {}, RENAMED),
        ({"head_sha": "c" * 40}, {}, RENAMED),
        ({"run_attempt": 2}, {}, RENAMED),
        ({}, {"7": 404}, RENAMED),
        ({}, {"1": 404}, RENAMED),
        ({}, {}, "example/notes"),
        ({}, {}, "example/unrelated"),
    ],
    ids=[
        "run-repository-id",
        "run-head-repository-id",
        "run-repository-name",
        "run-head-sha",
        "run-attempt",
        "release-not-in-new-repository",
        "run-not-in-new-repository",
        "old-name-redirects",
        "unrelated-repository",
    ],
)
def test_unproven_rename_is_refused_without_a_proposal(
    ledger, forge, refusals, repository
):
    install(ledger)
    fixture = GitHubFixture(RENAMED)
    fixture.forge = forge
    with pytest.raises(ContractError):
        prove(ledger, fixture, repository=repository, **refusals)
    assert proposals(ledger) == 0


@pytest.mark.parametrize("repository", ["../notes", "example/notes/../x", "a"])
def test_malformed_repository_name_is_refused_before_any_request(ledger, repository):
    install(ledger)
    fixture = GitHubFixture(RENAMED)
    with pytest.raises(ContractError, match="Invalid GitHub repository"):
        prove(ledger, fixture, repository=repository)
    assert fixture.calls == []


def test_rename_of_an_uninstalled_app_points_to_update_registration(ledger):
    with pytest.raises(ContractError, match="update-registration"):
        prove(ledger, GitHubFixture(RENAMED))
    assert proposals(ledger) == 0


def test_discovery_after_a_rename_proposal_verifies_releases_under_the_new_name(
    ledger,
):
    install(ledger)
    prove(ledger, GitHubFixture(RENAMED))
    fixture = GitHubFixture(RENAMED, release_id=8)

    async def run():
        async with httpx.AsyncClient(transport=serve(fixture)) as client:
            return await ReleaseDiscovery(
                ledger.database, GitHubPublications(client)
            ).discover("notes")

    result = asyncio.run(run())
    digest = fixture.proof["manifest_sha256"]
    assert (result.latest_sha256, result.error) == (digest, None)
    assert all(path.startswith(f"/repos/{RENAMED}/") for path in fixture.calls)
    with ledger.database.connect() as connection:
        release = eligible(
            connection, proposed_registration(connection, "notes"), digest
        )
    assert release.manifest.image.startswith(f"ghcr.io/{RENAMED}@")


def test_card_shows_a_pending_rename_once_a_new_name_release_exists(browser, ledger):
    install(ledger)
    client, accounts = browser
    login(client, accounts.get_identity(UUID(ADMIN_USER_ID), 1))
    prove(ledger, GitHubFixture(RENAMED))
    _, card = overview(client)
    assert (card.get("error"), card["installed"], card["pending"]) == (
        None,
        ledger.digest,
        None,
    )
    digest, _ = renamed_release(ledger)
    _, card = overview(client)
    assert card["pending"]["digest"] == digest
    assert card["pending"]["settings_change"] == [
        f"moves its source example/notes → {RENAMED}"
    ]


def test_rename_deploys_a_new_name_release_and_rolls_back_across_it(ledger):
    first = install(ledger)
    prove(ledger, GitHubFixture(RENAMED))
    digest, deploy = rename_deploy(ledger)
    assert deploy.approval.selection.registration_sha256 == fingerprint(
        renamed(first.registration)
    )
    assert boot_view(ledger)[0] == first.registration
    second = accept(ledger, deploy, 2)
    row, authority, snapshot, *_ = boot_view(ledger)
    assert snapshot == second
    assert row == authority.registration == renamed(first.registration)
    with ledger.database.connect() as connection:
        assert pending_change(connection, "notes", "test") is None
        assert eligible(connection, row, digest).manifest.image.startswith(
            f"ghcr.io/{RENAMED}@"
        )

    rollback = request(ledger, "rollback", preview_id=first.selection.preview_id)
    assert rollback.approval.selection.registration_sha256 == fingerprint(
        first.registration
    )
    third = accept(ledger, rollback, 3)
    row, authority, snapshot, *_ = boot_view(ledger)
    assert snapshot == third
    assert row == authority.registration == first.registration


def test_rehearsal_checks_the_previous_release_under_its_installed_source(
    helper, tmp_path, monkeypatch
):
    installer, operation, authority, app, release, _ = helper
    previous = InstalledSnapshot(
        operation_id=uuid4(),
        generation=1,
        selection=operation.approval.selection,
        registration=app.registration,
        image=release.image,
        image_id="sha256:" + "b" * 64,
    )
    installed_by_root(installer, previous)
    retain(installer, release)
    policy = app.model_copy(update={"registration": renamed(app.registration)})
    backup = tmp_path / "copy.sqlite"
    backup.write_bytes(b"disposable fixture")
    monkeypatch.setattr(os, "chown", lambda *_: None)
    monkeypatch.setattr(os, "fchown", lambda *_: None)
    monkeypatch.setattr(installer, "remove", lambda *_: None)
    monkeypatch.setattr(installer, "start", lambda *args, **kwargs: "fixture")
    monkeypatch.setattr(installer, "rehearsal_ready", lambda *_: None)
    monkeypatch.setattr(installer, "backup", lambda *args, **kwargs: None)
    monkeypatch.setattr(installer, "semantic_state", lambda *_: "records")
    result = Installer.rehearse(
        installer,
        authority.model_copy(update={"previous": previous}),
        policy,
        release,
        "sha256:" + "b" * 64,
        backup,
    )
    assert result.checks["semantic_records"]
    assert result.previous_image_id == "sha256:" + "b" * 64


def test_blocked_rename_deploy_recovers_by_restoring_the_installed_release(
    helper, ledger, monkeypatch
):
    installer, rehearsal, _, app, release, _ = helper
    complete_rehearsal(ledger, rehearsal)
    first = accept(
        ledger, request(ledger, "deploy", rehearsal_id=rehearsal.operation_id), 1
    )
    installed_by_root(installer, first)
    retain(installer, release)
    prove(ledger, GitHubFixture(RENAMED))
    digest, deploy = rename_deploy(ledger)
    block(ledger, deploy, installer.journal)

    recover = request(ledger, "recover", digest=digest, preview_id=None)
    with ledger.database.connect() as connection:
        authority = read_authority(connection, recover.operation_id, "a" * 64)
    forged_authority(installer, authority, app, recover, monkeypatch)
    real_child = installer.child
    monkeypatch.setattr(
        installer,
        "child",
        lambda *args: b"switching" if args[2] == "state" else real_child(*args),
    )
    monkeypatch.setattr(
        installer,
        "owned",
        lambda *_: container(first, operation=str(deploy.operation_id)),
    )
    monkeypatch.setattr(installer, "local_image", lambda *_: first.image_id)
    monkeypatch.setattr(installer, "stop_timers", lambda *_: None)
    monkeypatch.setattr(installer, "remove", lambda *_: None)
    started = []
    monkeypatch.setattr(
        installer,
        "start",
        lambda _a, started_app, *_, **kw: started.append(
            (kw["installed_operation_id"], started_app.registration)
        ),
    )
    monkeypatch.setattr(installer, "probe", lambda *_: None)
    installer.execute(recover.operation_id)
    ledger.service.reconcile()

    assert started == [(first.operation_id, first.registration)]
    with ledger.database.connect() as connection:
        states = dict(
            connection.execute("SELECT kind, state FROM app_operations").fetchall()
        )
        change = pending_change(connection, "notes", "test")
    assert (states["recover"], states["deploy"]) == ("succeeded", "recovered")
    row, authority, snapshot, *_ = boot_view(ledger)
    assert snapshot == first == installer.current(app)
    assert row == authority.registration == first.registration
    assert change.registration == renamed(first.registration)

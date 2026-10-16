import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_app_registry import manifest as manifest_document

from app.app_contracts import (
    MAX_DOCUMENT_BYTES,
    ContractError,
    Maintenance,
    ReleaseManifest,
    parse_document,
)
from app.app_registry import AppRegistry
from app.config import InstanceFacts
from app.database import Database
from ops import onboard, onboard_child, onboarding
from ops.installer_policy import HostPolicy, app_fingerprint
from ops.onboarding import Account, CoreStatus, HostFacts, OnboardSpec, plan_onboarding


@pytest.fixture
def inputs(tmp_path):
    policy = HostPolicy(
        core_uid=1000,
        core_gid=1000,
        database=str(tmp_path / "core" / "core.sqlite"),
        spool=str(tmp_path / "spool"),
        journal=str(tmp_path / "journal"),
        scratch=str(tmp_path / "scratch"),
        units=str(tmp_path / "units"),
        python="/usr/bin/python3",
        environment="test",
        app_api_url="http://core.example/internal/apps",
        timezone="UTC",
        apps=(),
    )
    spec = OnboardSpec(
        repository="example/notes", compatibility_argv=("python", "-m", "notes.check")
    )
    document = manifest_document()
    document["secrets"] = []
    manifest = parse_document(ReleaseManifest, json.dumps(document).encode())
    core = CoreStatus(
        registrations=(),
        instance_configured=True,
        environment=policy.environment,
        app_api_url=policy.app_api_url,
        timezone=policy.timezone,
    )
    facts = HostFacts((), (), frozenset())
    return spec, policy, facts, core, manifest


def test_release_defines_registration_and_host_contract(inputs):
    spec, policy, facts, core, manifest = inputs
    plan = plan_onboarding(spec, policy, facts, core, manifest)
    assert plan.account == Account("hp-notes", 2000, 2000)
    assert plan.app.registration.capabilities == manifest.capabilities
    assert plan.app.state == manifest.state
    assert plan.app.maintenance == manifest.maintenance
    assert plan.app.installation.probe.version_path == manifest.runtime.version_path
    assert plan.app.registration.access == "administrator"
    assert plan.app.backup_schedule is not None
    assert parse_document(HostPolicy, plan.policy_bytes) == plan.candidate


def test_stateless_release_has_no_backup_timer(inputs):
    spec, policy, facts, core, manifest = inputs
    document = manifest.model_dump(mode="json", exclude={"state"})
    manifest = parse_document(ReleaseManifest, json.dumps(document).encode())
    plan = plan_onboarding(spec, policy, facts, core, manifest)
    assert plan.app.state is None
    assert plan.app.backup_schedule is None
    assert plan.app.registration.bindings.backup_directory is not None


def test_allocator_avoids_all_identity_spaces_ports_and_reservations(inputs):
    spec, policy, _, core, manifest = inputs
    facts = HostFacts(
        (Account("core", 1000, 1000), Account("other", 2004, 2005)),
        (("other", 2005), ("orphan-group", 2010), ("nobody", 65534)),
        frozenset({8100, 8102}),
        frozenset({2012}),
    )
    plan = plan_onboarding(spec, policy, facts, core, manifest)
    assert plan.account.uid == plan.account.gid == 2013
    assert plan.app.installation.host_port == 8101


def test_crash_reservation_keeps_allocation_and_verified_release(inputs):
    spec, policy, facts, core, manifest = inputs
    first = plan_onboarding(spec, policy, facts, core, manifest)
    occupied = replace(facts, accounts=(first.account,), groups=(("hp-notes", 2000),))
    newer = manifest.model_copy(update={"source_commit": "b" * 40, "maintenance": ()})
    again = plan_onboarding(spec, policy, occupied, core, newer, first.record)
    assert again.app == first.app
    assert again.record.manifest == manifest


def test_other_onboarding_cannot_take_a_crashed_reservation(inputs):
    spec, policy, facts, core, manifest = inputs
    first = plan_onboarding(spec, policy, facts, core, manifest)
    reserved = replace(
        facts, reserved_uids=frozenset({first.app.uid}), reserved_apps=(first.app,)
    )
    next_spec = spec.model_copy(update={"repository": "example/ledger"})
    next_manifest = manifest.model_copy(
        update={
            "app_id": "ledger",
            "image": "ghcr.io/example/ledger@sha256:" + "b" * 64,
        }
    )
    second = plan_onboarding(next_spec, policy, reserved, core, next_manifest)
    assert second.app.installation.host_port != first.app.installation.host_port
    assert second.app.uid != first.app.uid
    both_reserved = replace(reserved, reserved_apps=(first.app, second.app))
    after_second = core.model_copy(update={"registrations": (second.app.registration,)})
    retried = plan_onboarding(
        spec, second.candidate, both_reserved, after_second, manifest, first.record
    )
    assert retried.app == first.app
    assert retried.candidate.apps == (second.app, first.app)


@pytest.mark.parametrize("same_id", [True, False])
def test_another_repository_cannot_take_reserved_app_id_or_route(inputs, same_id):
    spec, policy, facts, core, manifest = inputs
    first = plan_onboarding(spec, policy, facts, core, manifest)
    reserved = replace(
        facts, reserved_uids=frozenset({first.app.uid}), reserved_apps=(first.app,)
    )
    other_spec = spec.model_copy(
        update={"repository": "example/another", "route": "/notes"}
    )
    other_manifest = manifest.model_copy(
        update={
            "app_id": "notes" if same_id else "another",
            "image": "ghcr.io/example/another@sha256:" + "b" * 64,
        }
    )
    with pytest.raises(ContractError, match="onboarding reservation"):
        plan_onboarding(other_spec, policy, reserved, core, other_manifest)


@pytest.mark.parametrize(
    "field,value",
    [
        ("access", "linked-user"),
        ("compatibility_argv", ("changed",)),
        ("label", "Different"),
        ("route", "/different"),
    ],
)
def test_reserved_spec_changes_are_refused(inputs, field, value):
    spec, policy, facts, core, manifest = inputs
    first = plan_onboarding(spec, policy, facts, core, manifest)
    changed = spec.model_copy(update={field: value})
    with pytest.raises(ContractError, match="spec changed"):
        plan_onboarding(changed, policy, facts, core, manifest, first.record)


def test_appending_preserves_existing_fingerprint(inputs):
    spec, policy, facts, core, manifest = inputs
    first = plan_onboarding(spec, policy, facts, core, manifest)
    next_spec = spec.model_copy(update={"repository": "example/ledger"})
    next_manifest = manifest.model_copy(
        update={
            "app_id": "ledger",
            "image": "ghcr.io/example/ledger@sha256:" + "b" * 64,
        }
    )
    next_core = core.model_copy(update={"registrations": (first.app.registration,)})
    second = plan_onboarding(
        next_spec, first.candidate, facts, next_core, next_manifest
    )
    assert second.candidate.apps[0] == first.app
    assert app_fingerprint(first.candidate, "notes") == app_fingerprint(
        second.candidate, "notes"
    )
    assert second.account.uid != first.account.uid


@pytest.mark.parametrize(
    "kind",
    ["route", "core-route", "account", "group", "credential", "instance", "secrets"],
)
def test_conflicts_are_refused_before_effects(inputs, kind):
    spec, policy, facts, core, manifest = inputs
    if kind == "route":
        first = plan_onboarding(spec, policy, facts, core, manifest)
        core = core.model_copy(
            update={
                "registrations": (
                    first.app.registration.model_copy(update={"app_id": "other"}),
                )
            }
        )
    elif kind == "core-route":
        spec = spec.model_copy(update={"route": "/dashboard/apps"})
    elif kind == "account":
        facts = replace(facts, accounts=(Account("hp-notes", 1000, 1000),))
    elif kind == "group":
        facts = replace(facts, groups=(("hp-notes", 999),))
    elif kind == "credential":
        core = core.model_copy(
            update={"credential_path": "/elsewhere", "credential_valid": True}
        )
    elif kind == "instance":
        core = core.model_copy(update={"environment": "other"})
    else:
        manifest = manifest.model_copy(update={"secrets": ("provider-key",)})
    with pytest.raises(ContractError):
        plan_onboarding(spec, policy, facts, core, manifest)


class FakeHost(onboard.OnboardingHost):
    def __init__(self, policy_path, policy, manifest):
        super().__init__(policy_path, policy)
        self.manifest = manifest
        self.events = []
        self.fail_after = None
        self.accounts = ()
        self.saved = None
        self.db = Database(Path(policy.database))
        self.db.initialize()
        self.registry = AppRegistry(self.db)
        self.registry.publish_instance(
            InstanceFacts(
                policy.environment,
                policy.app_api_url,
                policy.timezone,
            )
        )

    def did(self, event):
        self.events.append(event)
        if event == self.fail_after:
            self.fail_after = None
            raise RuntimeError("simulated crash")

    def record(self, spec):
        return self.saved

    def release(self, spec, token_file):
        self.events.append("release")
        return self.manifest

    def status(self, app_id):
        return onboard_child.status(self.registry, app_id)

    def facts(self):
        return HostFacts(self.accounts, (), frozenset())

    def prerequisites(self, plan):
        pass

    def reserve(self, spec, plan):
        if self.saved is not None:
            assert self.saved == plan.record
        self.saved = plan.record
        self.did("reserve")

    def ensure_account(self, account):
        self.accounts = (account,)
        self.did("account")

    def ensure_directories(self, plan):
        Path(plan.app.credential_file).parent.mkdir(parents=True, exist_ok=True)
        self.did("directories")

    def register(self, plan):
        self.registry.register(plan.app.registration.model_dump_json().encode())
        self.did("register")

    def credential(self, plan):
        onboard_child.ensure_credential(
            self.registry, plan.app.registration.app_id, Path(plan.app.credential_file)
        )
        self.did("credential")

    def install_policy(self, plan):
        self.policy_path.write_bytes(plan.policy_bytes)
        self.policy = parse_document(HostPolicy, self.policy_path.read_bytes())
        self.did("policy")


@pytest.fixture
def fake(inputs, tmp_path, monkeypatch):
    _, policy, _, _, manifest = inputs
    monkeypatch.setattr(onboarding, "STATE_ROOT", tmp_path / "app-state")
    monkeypatch.setattr(onboarding, "INSTALLER_ROOT", tmp_path / "installer")
    return FakeHost(tmp_path / "policy.json", policy, manifest)


@pytest.mark.parametrize(
    "crash", ["reserve", "account", "directories", "register", "credential", "policy"]
)
def test_every_crash_point_converges_without_rotation(inputs, fake, crash):
    spec = inputs[0]
    fake.fail_after = crash
    with pytest.raises(RuntimeError, match="simulated crash"):
        onboard.run(spec, fake, dry_run=False)
    first = fake.saved
    plan = onboard.run(spec, fake, dry_run=False)
    assert plan.app == first.app
    credential = fake.registry.active_credential("notes")
    token = credential.file_path.read_bytes()
    onboard.run(spec, fake, dry_run=False)
    assert fake.registry.active_credential("notes") == credential
    assert credential.file_path.read_bytes() == token
    assert fake.policy.apps == (first.app,)
    assert fake.events.count("release") == 1


def test_dry_run_has_no_host_or_database_writes(inputs, fake, capsys):
    before = list(fake.db._connection().iterdump())
    onboard.run(inputs[0], fake, dry_run=True)
    assert fake.events == ["release"]
    assert fake.saved is None
    assert fake.accounts == ()
    assert not fake.policy_path.exists()
    assert list(fake.db._connection().iterdump()) == before
    assert "Dry run" in capsys.readouterr().out


def test_oversized_reservation_is_rejected_before_any_effects(inputs, fake):
    spec, policy, _facts, _core, manifest = inputs
    maintenance = Maintenance(
        name="cleanup",
        argv=tuple("x" * 256 for _ in range(32)),
        interval_hours=24,
        timeout_seconds=300,
    )
    large = manifest.model_copy(
        update={
            "maintenance": tuple(
                maintenance.model_copy(update={"name": f"cleanup{i}"}) for i in range(4)
            )
        }
    )
    assert len(large.model_dump_json().encode()) < MAX_DOCUMENT_BYTES
    app = onboarding.build_app(spec, large, Account("hp-notes", 2000, 2000), 8100, 10)
    candidate = policy.model_copy(update={"apps": (app,)})
    assert len(candidate.model_dump_json().encode()) < MAX_DOCUMENT_BYTES
    fake.manifest = large
    with pytest.raises(ContractError, match="exceeds 65536 bytes"):
        onboard.run(spec, fake, dry_run=False)
    assert fake.events == ["release"]
    assert fake.saved is None
    assert fake.accounts == ()
    assert fake.registry.summaries() == ()


def test_credential_file_survives_crash_before_database_commit(
    inputs, fake, monkeypatch
):
    spec = inputs[0]
    plan = onboard.run(spec, fake, dry_run=True)
    fake.register(plan)
    path = Path(plan.app.credential_file)
    path.parent.mkdir(parents=True)
    token = "hpa1." + "a" * 32 + "." + "b" * 43
    path.write_text(token + "\n")
    path.chmod(0o600)
    onboard_child.ensure_credential(fake.registry, "notes", path)
    assert fake.registry.verify_credential(token).file_path == path
    assert path.read_text().strip() == token


def test_privileged_runtime_validation_precedes_external_effects(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(onboard, "load_policy", lambda _: object())

    def refuse(_):
        calls.append("validate")
        raise ContractError("unsafe runtime")

    monkeypatch.setattr(onboard, "validate_paths", refuse)
    monkeypatch.setattr(
        onboard.OnboardingHost, "release", lambda *_: calls.append("network")
    )
    assert onboard.main([str(tmp_path / "missing.json"), "--policy", "/policy"]) == 2
    assert calls == ["validate"]


def test_child_keeps_release_token_out_of_argv_and_output(inputs, monkeypatch, capsys):
    policy = inputs[1]
    host = onboard.OnboardingHost(Path("/policy"), policy)
    captured = {}

    def child(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        return subprocess_result(0, b"{}")

    monkeypatch.setattr(onboard.subprocess, "run", child)
    secret = "sensitive-release-token"
    host.child("release", json.dumps({"token": secret}).encode())
    assert secret not in repr(captured["argv"])
    assert secret.encode() in captured["input"]
    assert captured["user"] == policy.core_uid
    assert captured["group"] == policy.core_gid
    assert captured["extra_groups"] == ()
    assert captured["env"]["HOME"] == "/nonexistent"
    assert secret not in capsys.readouterr().out


def subprocess_result(returncode, stdout):
    from types import SimpleNamespace

    return SimpleNamespace(returncode=returncode, stdout=stdout)


def test_policy_install_checks_compare_and_swap(inputs, monkeypatch):
    spec, policy, facts, core, manifest = inputs
    plan = plan_onboarding(spec, policy, facts, core, manifest)
    host = onboard.OnboardingHost(Path("/policy"), policy)
    changed = policy.model_copy(update={"environment": "changed"})
    monkeypatch.setattr(onboard, "load_policy", lambda _: changed)
    monkeypatch.setattr(
        onboard.os, "replace", lambda *_: pytest.fail("policy overwritten")
    )
    with pytest.raises(ContractError, match="changed during"):
        host.install_policy(plan)


def test_empty_installer_has_no_app_units_or_operations(inputs, monkeypatch):
    from ops.managed_installer import Installer

    installer = Installer(inputs[1])
    units = Path(installer.policy.units)
    units.mkdir()
    commands = []
    monkeypatch.setattr(
        installer, "command", lambda *args, **_kwargs: commands.append(args)
    )
    installer.trigger()
    assert installer.policy.apps == ()
    assert {path.name for path in units.iterdir()} == {
        "hp-managed-installer.service",
        "hp-managed-installer.path",
        "hp-managed-installer.timer",
    }
    assert all(command[0] == "systemctl" for command in commands)

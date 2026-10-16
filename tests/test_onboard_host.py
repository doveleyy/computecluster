import asyncio
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_onboarding import inputs as inputs_fixture

from app.app_contracts import ContractError, SecretFile
from app.app_spool import read_owned
from ops import onboard, onboard_child, onboarding
from ops.onboarding import HostFacts, OnboardingRecord, plan_onboarding

inputs = inputs_fixture


@pytest.fixture
def host_plan(inputs, tmp_path, monkeypatch):
    spec, policy, facts, core, manifest = inputs
    monkeypatch.setattr(onboarding, "STATE_ROOT", tmp_path / "states")
    monkeypatch.setattr(onboarding, "INSTALLER_ROOT", tmp_path / "installer")
    plan = plan_onboarding(spec, policy, facts, core, manifest)
    host = onboard.OnboardingHost(tmp_path / "policy.json", policy)
    monkeypatch.setattr(onboard, "root_path", lambda *_args, **_kwargs: None)
    return host, plan


def test_real_policy_replace_validates_before_write_and_is_atomic(
    host_plan, monkeypatch
):
    host, plan = host_plan
    host.policy_path.write_bytes(host.policy.model_dump_json().encode())
    events = []
    monkeypatch.setattr(onboard, "load_policy", lambda path: host.policy)

    def validate(candidate):
        assert host.policy_path.read_bytes() == host.policy.model_dump_json().encode()
        assert candidate == plan.candidate
        events.append("validate")

    monkeypatch.setattr(onboard, "validate_paths", validate)
    host.install_policy(plan)
    assert events == ["validate"]
    assert host.policy_path.read_bytes() == plan.policy_bytes
    assert stat.S_IMODE(host.policy_path.stat().st_mode) == 0o644


def test_invalid_candidate_leaves_policy_untouched(host_plan, monkeypatch):
    host, plan = host_plan
    initial = host.policy.model_dump_json().encode()
    host.policy_path.write_bytes(initial)
    monkeypatch.setattr(onboard, "load_policy", lambda path: host.policy)

    def refuse(candidate):
        raise ContractError("candidate path refused")

    monkeypatch.setattr(onboard, "validate_paths", refuse)
    with pytest.raises(ContractError, match="candidate path refused"):
        host.install_policy(plan)
    assert host.policy_path.read_bytes() == initial


def test_os_account_commands_resume_from_group_creation(host_plan, monkeypatch):
    host, plan = host_plan
    accounts = []
    groups = []
    commands = []
    monkeypatch.setattr(
        host,
        "facts",
        lambda: HostFacts(
            tuple(accounts), tuple((g.gr_name, g.gr_gid) for g in groups), frozenset()
        ),
    )
    monkeypatch.setattr(onboard.grp, "getgrall", lambda: groups)
    monkeypatch.setattr(
        onboard.pwd,
        "getpwall",
        lambda: [SimpleNamespace(pw_name=a.name) for a in accounts],
    )

    def command(argv, **kwargs):
        commands.append(argv)
        if argv[0] == "groupadd":
            groups.append(
                SimpleNamespace(gr_name=plan.account.name, gr_gid=plan.account.gid)
            )
            raise RuntimeError("after group creation")
        accounts.append(plan.account)
        return b""

    monkeypatch.setattr(onboard, "capture", command)
    with pytest.raises(RuntimeError, match="after group creation"):
        host.ensure_account(plan.account)
    host.ensure_account(plan.account)
    host.ensure_account(plan.account)
    assert [command[0] for command in commands] == ["groupadd", "useradd"]
    assert "--no-create-home" in commands[1]
    assert "/usr/sbin/nologin" in commands[1]


def test_directory_creation_publishes_owner_and_mode_together(host_plan, monkeypatch):
    host, plan = host_plan
    ownership = {}
    monkeypatch.setattr(
        onboard.os,
        "chown",
        lambda path, uid, gid: ownership.update({Path(path): (uid, gid)}),
    )
    rename = os.rename

    def publish_directory(source, target):
        assert Path(source).stat().st_mode & 0o777 in {0o755, 0o750, 0o700}
        owner = ownership.pop(Path(source))
        rename(source, target)
        ownership[Path(target)] = owner

    monkeypatch.setattr(onboard.os, "rename", publish_directory)

    def check(path, uid, gid, mode):
        assert ownership[path] == (uid, gid)
        assert stat.S_IMODE(path.stat().st_mode) == mode

    monkeypatch.setattr(host, "check_directory", check)
    host.ensure_directories(plan)
    before = dict(ownership)
    host.ensure_directories(plan)
    assert ownership == before
    assert ownership[Path(str(plan.app.registration.bindings.state_directory))] == (
        plan.app.uid,
        plan.app.gid,
    )


def test_existing_directory_is_never_chowned_to_the_new_app(host_plan, monkeypatch):
    host, plan = host_plan
    path = Path(str(plan.app.registration.bindings.state_directory))
    path.mkdir(parents=True)
    (path / "owner-data").write_text("preserve")
    monkeypatch.setattr(
        onboard.os, "chown", lambda *_: pytest.fail("changed existing owner")
    )
    with pytest.raises(ContractError, match="ownership or mode"):
        host.ensure_directories(plan)
    assert (path / "owner-data").read_text() == "preserve"


def test_reservation_is_private_immutable_and_stateless_safe(
    inputs, host_plan, monkeypatch
):
    spec = inputs[0]
    host, plan = host_plan
    manifest = plan.record.manifest.model_copy(update={"state": None})
    plan = onboarding.OnboardingPlan(
        account=plan.account,
        record=plan.record.model_copy(update={"manifest": manifest}),
        candidate=plan.candidate,
        policy_sha256=plan.policy_sha256,
        core=plan.core,
    )
    Path(host.policy.journal).mkdir()
    host.reserve(spec, plan)
    path = host.record_path(spec)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert read_owned(path, os.geteuid()) == plan.record.model_dump_json().encode()
    assert (
        OnboardingRecord.model_validate_json(path.read_bytes()).manifest.state is None
    )
    before = path.read_bytes()
    host.reserve(spec, plan)
    assert path.read_bytes() == before
    changed = onboarding.OnboardingPlan(
        account=plan.account,
        record=plan.record.model_copy(update={"spec_sha256": "c" * 64}),
        candidate=plan.candidate,
        policy_sha256=plan.policy_sha256,
        core=plan.core,
    )
    with pytest.raises(ContractError, match="conflicts"):
        host.reserve(spec, changed)


def test_secret_prerequisite_never_prints_or_copies_secret(
    inputs, host_plan, tmp_path, monkeypatch, capsys
):
    host, plan = host_plan
    path = tmp_path / "provider" / "key"
    path.parent.mkdir()
    path.write_text("sensitive-provider-value")
    path.chmod(0o600)
    binding = SecretFile(name="provider-key", file_path=str(path))
    app = plan.app.model_copy(
        update={
            "registration": plan.app.registration.model_copy(
                update={
                    "bindings": plan.app.registration.bindings.model_copy(
                        update={"secret_files": (binding,)}
                    )
                }
            )
        }
    )
    plan = onboarding.OnboardingPlan(
        account=plan.account,
        record=plan.record.model_copy(update={"app": app}),
        candidate=plan.candidate,
        policy_sha256=plan.policy_sha256,
        core=plan.core,
    )
    monkeypatch.setattr(
        onboard, "read_owned", lambda file, uid: read_owned(file, os.geteuid())
    )
    before = path.read_bytes()
    host.prerequisites(plan)
    print(onboarding.describe(plan))
    assert "sensitive-provider-value" not in capsys.readouterr().out
    assert "sensitive-provider-value" not in plan.record.model_dump_json()
    assert path.read_bytes() == before
    path.chmod(0o644)
    with pytest.raises(ContractError, match="private to root"):
        host.prerequisites(plan)


def test_verified_release_uses_exact_source_and_latest_stable(inputs, monkeypatch):
    spec, _, _, _, manifest = inputs
    events = []

    class Publications:
        def __init__(self, client, token):
            assert token == "private-token"

        async def listing(self, source, etag):
            events.append(("listing", source, etag))
            return SimpleNamespace(releases=[{"id": 8}, {"id": 7}])

        async def verify(self, source, release_id):
            events.append(("verify", source, release_id))
            return SimpleNamespace(manifest=manifest)

    monkeypatch.setattr(onboard_child, "GitHubPublications", Publications)
    assert (
        asyncio.run(onboard_child.verified_release(spec.source(), "private-token"))
        == manifest
    )
    assert events == [("listing", spec.source(), None), ("verify", spec.source(), 8)]

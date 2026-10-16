"""Register one published app and append its host policy without deploying it."""

import argparse
import fcntl
import grp
import hashlib
import json
import os
import pwd
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import psutil

from app.app_contracts import (
    MAX_DOCUMENT_BYTES,
    ContractError,
    ReleaseManifest,
    parse_document,
)
from app.app_deployment_contracts import fingerprint
from app.app_spool import publish, read_owned
from ops.installer_policy import (
    HostPolicy,
    integration_sources,
    load_policy,
    root_path,
    validate_paths,
)
from ops.managed_installer import capture, sync_directory
from ops.onboarding import (
    Account,
    CoreStatus,
    HostFacts,
    OnboardingPlan,
    OnboardingRecord,
    OnboardSpec,
    check_account,
    describe,
    plan_onboarding,
)


class OnboardingHost:
    def __init__(self, policy_path: Path, policy: HostPolicy) -> None:
        self.policy_path = policy_path
        self.policy = policy
        self.records = Path(policy.journal) / "onboarding"

    def child(self, kind: str, payload: bytes = b"{}", app_id: str = "") -> bytes:
        result = subprocess.run(
            (
                self.policy.python,
                "-I",
                "-m",
                "ops.onboard_child",
                kind,
                self.policy.database,
                app_id,
            ),
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=300,
            check=False,
            user=self.policy.core_uid,
            group=self.policy.core_gid,
            extra_groups=(),
            cwd="/",
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/nonexistent"},
        )
        if result.returncode or len(result.stdout) > MAX_DOCUMENT_BYTES:
            raise ContractError(f"Unprivileged onboarding {kind} failed")
        return result.stdout

    def facts(self) -> HostFacts:
        reserved: set[int] = set()
        apps = []
        if self.records.exists():
            root_path(self.records)
            paths = tuple(self.records.glob("*.json"))
            if len(paths) > 128:
                raise ContractError(
                    "Onboarding record limit reached; operator review required"
                )
            for path in paths:
                record = parse_document(OnboardingRecord, read_owned(path, 0))
                reserved.update((record.app.uid, record.app.gid))
                apps.append(record.app)
        return HostFacts(
            accounts=tuple(
                Account(a.pw_name, a.pw_uid, a.pw_gid) for a in pwd.getpwall()
            ),
            groups=tuple((group.gr_name, group.gr_gid) for group in grp.getgrall()),
            listening_ports=frozenset(
                item.laddr.port
                for item in psutil.net_connections(kind="inet")
                if item.laddr and (item.status == psutil.CONN_LISTEN or item.type == 2)
            ),
            reserved_uids=frozenset(reserved),
            reserved_apps=tuple(apps),
        )

    def record_path(self, spec: OnboardSpec) -> Path:
        return self.records / (
            hashlib.sha256(spec.repository.encode()).hexdigest() + ".json"
        )

    def record(self, spec: OnboardSpec) -> OnboardingRecord | None:
        if not self.records.exists():
            return None
        root_path(self.records)
        try:
            payload = read_owned(self.record_path(spec), 0)
        except FileNotFoundError:
            return None
        return parse_document(OnboardingRecord, payload)

    def release(self, spec: OnboardSpec, token_file: Path | None) -> ReleaseManifest:
        token = None
        if token_file is not None:
            info = token_file.lstat()
            if info.st_uid not in {0, self.policy.core_uid} or info.st_mode & 0o077:
                raise ContractError("Release token must be private to root or core")
            owner = token_file.parent.lstat().st_uid
            if owner not in {0, self.policy.core_uid}:
                raise ContractError(
                    "Release token directory must belong to root or core"
                )
            root_path(token_file.parent, leaf_uid=owner)
            token = read_owned(token_file, info.st_uid, 16384).decode().strip()
            if not token:
                raise ContractError("Release token file is empty")
        payload = json.dumps(
            {"source": spec.source().model_dump(mode="json"), "token": token}
        )
        return parse_document(ReleaseManifest, self.child("release", payload.encode()))

    def status(self, app_id: str) -> CoreStatus:
        return parse_document(CoreStatus, self.child("status", app_id=app_id))

    def prerequisites(self, plan: OnboardingPlan) -> None:
        integration_sources(plan.app)
        for item in plan.app.registration.bindings.secret_files:
            path = Path(item.file_path)
            root_path(path, directory=False)
            read_owned(path, 0)
            if path.lstat().st_mode & 0o077:
                raise ContractError("Secret files must be private to root")
        for path, uid, gid, mode in self.directories(plan):
            if path.exists() or path.is_symlink():
                self.check_directory(path, uid, gid, mode)
            else:
                parent = path.parent
                while not parent.exists():
                    parent = parent.parent
                root_path(parent)

    def directories(
        self, plan: OnboardingPlan
    ) -> tuple[tuple[Path, int, int, int], ...]:
        bindings = plan.app.registration.bindings
        return (
            (Path(str(bindings.state_directory)), plan.app.uid, plan.app.gid, 0o750),
            (Path(str(bindings.backup_directory)), 0, 0, 0o700),
            (
                Path(plan.app.credential_file).parent,
                self.policy.core_uid,
                self.policy.core_gid,
                0o700,
            ),
        )

    @staticmethod
    def check_directory(path: Path, uid: int, gid: int, mode: int) -> None:
        root_path(path, leaf_uid=uid)
        info = path.lstat()
        if info.st_gid != gid or stat.S_IMODE(info.st_mode) != mode:
            raise ContractError(
                "Existing onboarding directory ownership or mode conflicts"
            )

    def reserve(self, spec: OnboardSpec, plan: OnboardingPlan) -> None:
        if not self.records.exists():
            self.records.mkdir(mode=0o700)
        root_path(self.records)
        if (
            not self.record_path(spec).exists()
            and len(tuple(self.records.glob("*.json"))) >= 128
        ):
            raise ContractError(
                "Onboarding record limit reached; operator review required"
            )
        publish(
            self.record_path(spec), plan.record.model_dump_json().encode(), mode=0o600
        )

    def ensure_account(self, account: Account) -> None:
        check_account(account, self.facts())
        groups = {item.gr_name: item.gr_gid for item in grp.getgrall()}
        if account.name not in groups:
            capture(
                ("groupadd", "--gid", str(account.gid), account.name),
                timeout=30,
                limit=4096,
            )
        if not any(item.pw_name == account.name for item in pwd.getpwall()):
            capture(
                (
                    "useradd",
                    "--uid",
                    str(account.uid),
                    "--gid",
                    str(account.gid),
                    "--no-create-home",
                    "--home-dir",
                    "/nonexistent",
                    "--shell",
                    "/usr/sbin/nologin",
                    "--no-user-group",
                    account.name,
                ),
                timeout=30,
                limit=4096,
            )

    def ensure_directories(self, plan: OnboardingPlan) -> None:
        def create(path: Path, uid: int, gid: int, mode: int) -> None:
            temporary = Path(tempfile.mkdtemp(prefix=".onboard-", dir=path.parent))
            os.chown(temporary, uid, gid)
            temporary.chmod(mode)
            os.rename(temporary, path)
            sync_directory(path.parent)

        for path, uid, gid, mode in self.directories(plan):
            missing = []
            parent = path.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            root_path(parent)
            for directory in reversed(missing):
                create(directory, 0, 0, 0o755)
            if not path.exists():
                create(path, uid, gid, mode)
            self.check_directory(path, uid, gid, mode)

    def register(self, plan: OnboardingPlan) -> None:
        self.child("register", plan.app.registration.model_dump_json().encode())

    def credential(self, plan: OnboardingPlan) -> None:
        payload = json.dumps({"path": plan.app.credential_file}).encode()
        self.child("credential", payload, plan.app.registration.app_id)

    def install_policy(self, plan: OnboardingPlan) -> None:
        if fingerprint(load_policy(self.policy_path)) != plan.policy_sha256:
            raise ContractError("Host policy changed during onboarding; retry required")
        validate_paths(plan.candidate)
        if fingerprint(plan.candidate) == plan.policy_sha256:
            return
        with tempfile.NamedTemporaryFile(
            prefix=".onboard-policy-", dir=self.policy_path.parent, delete=False
        ) as temporary:
            temporary.write(plan.policy_bytes)
            temporary.flush()
            os.fchmod(temporary.fileno(), 0o644)
            os.fsync(temporary.fileno())
        os.replace(temporary.name, self.policy_path)
        sync_directory(self.policy_path.parent)


def apply_onboarding(
    spec: OnboardSpec, plan: OnboardingPlan, host: OnboardingHost
) -> None:
    host.prerequisites(plan)
    host.reserve(spec, plan)
    host.ensure_account(plan.account)
    host.ensure_directories(plan)
    host.register(plan)
    host.credential(plan)
    host.install_policy(plan)


def run(
    spec: OnboardSpec,
    host: OnboardingHost,
    *,
    dry_run: bool,
    token_file: Path | None = None,
) -> OnboardingPlan:
    record = host.record(spec)
    manifest = record.manifest if record else host.release(spec, token_file)
    core = host.status(manifest.app_id)
    plan = plan_onboarding(spec, host.policy, host.facts(), core, manifest, record)
    print(describe(plan))
    host.prerequisites(plan)
    if dry_run:
        print("Dry run. No host or core records were written.")
    else:
        apply_onboarding(spec, plan, host)
        print(
            "Onboarding complete. Use the Apps page to check for updates and approve."
        )
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--release-token-file", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
        validate_paths(policy)
        with args.spec.open("rb") as source:
            spec = parse_document(OnboardSpec, source.read(MAX_DOCUMENT_BYTES + 1))
        host = OnboardingHost(args.policy, policy)
        if args.dry_run:
            run(spec, host, dry_run=True, token_file=args.release_token_file)
        else:
            lock_path = Path(policy.journal) / "installer.lock"
            descriptor = os.open(
                lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "a") as lock:
                root_path(lock_path, directory=False)
                fcntl.flock(lock, fcntl.LOCK_EX)
                host.policy = load_policy(args.policy)
                validate_paths(host.policy)
                run(spec, host, dry_run=False, token_file=args.release_token_file)
        return 0
    except ContractError as error:
        print(f"Onboarding refused. {error}", file=sys.stderr)
        return 2
    except (OSError, ValueError, subprocess.SubprocessError):
        print(
            "Onboarding refused. Check spec, host prerequisites and release access.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

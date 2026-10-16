"""Derive one append-only host policy from operator intent and verified facts."""

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_serializer

from app.app_contracts import (
    Contract,
    ContractError,
    HostMount,
    ImageRepository,
    ManagedInstalled,
    OperatorBindings,
    PathValue,
    Presentation,
    Probe,
    PublishedSource,
    Registration,
    RelativePath,
    ReleaseManifest,
    ResourcePolicy,
    SecretFile,
    Setting,
    Sha256,
    Text,
    parse_document,
)
from app.app_deployment_contracts import fingerprint
from app.app_registry import CORE_APP_IDS, CORE_ROUTE_TREES, routes_overlap
from ops.installer_policy import BackupSchedule, HostApp, HostPolicy, app_fingerprint

UID_FLOOR = 2000
UID_CEILING = 60000
PORTS = range(8100, 8200)
STATE_ROOT = Path("/var/lib/home-platform")
INSTALLER_ROOT = Path("/var/lib/home-platform-installer")


class OnboardSpec(Contract):
    repository: Annotated[
        str,
        StringConstraints(
            pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$",
            max_length=256,
        ),
    ]
    compatibility_argv: Annotated[tuple[Text, ...], Field(min_length=1, max_length=32)]
    branch: RelativePath = "main"
    workflow: RelativePath = ".github/workflows/publish.yml"
    image_repository: ImageRepository | None = None
    access: Literal["linked-user", "administrator"] = "administrator"
    route: PathValue | None = None
    label: Text | None = None
    accent: Annotated[str, StringConstraints(pattern=r"^#[0-9a-fA-F]{6}$")] = "#6b9bd2"
    kicker: Annotated[str, StringConstraints(max_length=256)] = ""
    description: Annotated[str, StringConstraints(max_length=1024)] = ""
    settings: Annotated[tuple[Setting, ...], Field(max_length=64)] = ()
    secrets: Annotated[tuple[SecretFile, ...], Field(max_length=32)] = ()
    mounts: Annotated[tuple[HostMount, ...], Field(max_length=32)] = ()
    resources: ResourcePolicy = ResourcePolicy()

    def source(self) -> PublishedSource:
        return PublishedSource(
            kind="published",
            repository=self.repository,
            branch=self.branch,
            workflow=self.workflow,
            image_repository=self.image_repository
            or "ghcr.io/" + self.repository.lower(),
        )


class CoreStatus(Contract):
    registrations: tuple[Registration, ...]
    credential_path: str | None = None
    credential_valid: bool = False
    instance_configured: bool
    environment: str | None = None
    app_api_url: str | None = None
    timezone: str | None = None


@dataclass(frozen=True)
class Account:
    name: str
    uid: int
    gid: int


@dataclass(frozen=True)
class HostFacts:
    accounts: tuple[Account, ...]
    groups: tuple[tuple[str, int], ...]
    listening_ports: frozenset[int]
    reserved_uids: frozenset[int] = frozenset()
    reserved_apps: tuple[HostApp, ...] = ()


class OnboardingRecord(Contract):
    spec_sha256: Sha256
    manifest: ReleaseManifest
    app: HostApp

    @field_serializer("manifest")
    def serialize_manifest(self, manifest: ReleaseManifest) -> dict[str, object]:
        return manifest.model_dump(mode="json", exclude_none=True)


@dataclass(frozen=True)
class OnboardingPlan:
    account: Account
    record: OnboardingRecord
    candidate: HostPolicy
    policy_sha256: str
    core: CoreStatus

    @property
    def app(self) -> HostApp:
        return self.record.app

    @property
    def policy_bytes(self) -> bytes:
        return self.candidate.model_dump_json().encode()


def account_name(app_id: str) -> str:
    name = "hp-" + app_id
    if len(name) > 32:
        raise ContractError("Application ID is too long for its host account")
    return name


def allocate(
    app_id: str, policy: HostPolicy, facts: HostFacts, core: CoreStatus
) -> tuple[Account, int]:
    name = account_name(app_id)
    occupied = {policy.core_uid, policy.core_gid, *facts.reserved_uids}
    occupied.update(item.uid for item in policy.apps)
    occupied.update(item.gid for item in policy.apps)
    existing = next((item for item in facts.accounts if item.name == name), None)
    if existing is not None:
        if existing.uid != existing.gid or existing.uid in occupied:
            raise ContractError("Application account conflicts with another identity")
        account = existing
    else:
        all_ids = occupied | {item.uid for item in facts.accounts}
        all_ids.update(item.gid for item in facts.accounts)
        all_ids.update(gid for _, gid in facts.groups)
        high = max((value for value in all_ids if value < UID_CEILING), default=0)
        uid = max(UID_FLOOR, high + 1)
        if uid >= UID_CEILING:
            raise ContractError("Application account range is exhausted")
        account = Account(name, uid, uid)
    check_account(account, facts)
    registered = next(
        (item for item in core.registrations if item.app_id == app_id), None
    )
    ports = {item.installation.host_port for item in policy.apps}
    ports.update(item.installation.host_port for item in facts.reserved_apps)
    for item in core.registrations:
        if item.app_id != app_id and item.installation is not None:
            ports.add(item.installation.host_port)
    if registered is not None and isinstance(registered.installation, ManagedInstalled):
        port = registered.installation.host_port
        if port in ports or port in facts.listening_ports:
            raise ContractError("Existing registration port is occupied")
    else:
        port = next((p for p in PORTS if p not in ports | facts.listening_ports), 0)
        if not port:
            raise ContractError("Application port range is exhausted")
    return account, port


def check_account(account: Account, facts: HostFacts) -> None:
    for item in facts.accounts:
        if item.name == account.name:
            if item != account:
                raise ContractError("Application account name has different IDs")
        elif {item.uid, item.gid} & {account.uid, account.gid}:
            raise ContractError("Application account IDs belong to another account")
    for name, gid in facts.groups:
        if (name == account.name) != (gid == account.gid) or (
            name != account.name and gid == account.uid
        ):
            raise ContractError("Application group name or ID conflicts")


def build_app(
    spec: OnboardSpec,
    manifest: ReleaseManifest,
    account: Account,
    port: int,
    position: int,
) -> HostApp:
    app_id = manifest.app_id
    resources = spec.resources
    if any(
        item.timeout_seconds > resources.maintenance_timeout_seconds
        for item in manifest.maintenance
    ):
        raise ContractError(
            "Resource maintenance timeout is below the release requirement"
        )
    if sorted(item.name for item in spec.secrets) != sorted(manifest.secrets):
        raise ContractError(
            "Secret file bindings must exactly match the verified release"
        )
    registration = Registration(
        app_id=app_id,
        route=spec.route or "/" + app_id,
        source=spec.source(),
        access=spec.access,
        capabilities=manifest.capabilities,
        presentation=Presentation(
            label=spec.label or app_id.replace("-", " ").title(),
            health_name=spec.label or app_id.replace("-", " ").title(),
            position=position,
            accent=spec.accent,
            kicker=spec.kicker,
            description=spec.description,
        ),
        bindings=OperatorBindings(
            state_directory=str(STATE_ROOT / app_id),
            backup_directory=str(INSTALLER_ROOT / ("backups-" + app_id)),
            secret_files=tuple(sorted(spec.secrets, key=lambda item: item.name)),
            settings=tuple(sorted(spec.settings, key=lambda item: item.name)),
            integration_bindings=tuple(sorted(item.name for item in spec.mounts)),
            resources=resources,
        ),
        installation=ManagedInstalled(
            owner="platform",
            host_port=port,
            probe=Probe(
                service=app_id,
                readiness="v1-status",
                ready_path=manifest.runtime.ready_path,
                version_path=manifest.runtime.version_path,
            ),
        ),
    )
    minute = int(fingerprint({"app_id": app_id})[:8], 16) % 300
    return HostApp(
        registration=registration,
        uid=account.uid,
        gid=account.gid,
        credential_file=str(
            INSTALLER_ROOT / ("credentials-" + app_id) / "app-credential"
        ),
        compatibility_argv=spec.compatibility_argv,
        state=manifest.state,
        maintenance=manifest.maintenance,
        backup_schedule=(
            BackupSchedule(
                hour=1 + minute // 60, minute=minute % 60, jitter_seconds=600
            )
            if manifest.state is not None
            else None
        ),
        mounts=tuple(sorted(spec.mounts, key=lambda item: item.name)),
    )


def plan_onboarding(
    spec: OnboardSpec,
    policy: HostPolicy,
    facts: HostFacts,
    core: CoreStatus,
    manifest: ReleaseManifest,
    record: OnboardingRecord | None = None,
) -> OnboardingPlan:
    if not core.instance_configured:
        raise ContractError("Core application instance must be configured first")
    if (core.environment, core.app_api_url, core.timezone) != (
        policy.environment,
        policy.app_api_url,
        policy.timezone,
    ):
        raise ContractError("Core instance configuration differs from host policy")
    if record is not None:
        if record.spec_sha256 != fingerprint(spec):
            raise ContractError(
                "Onboarding spec changed; existing record must be reviewed"
            )
        manifest = record.manifest
    if manifest.image.split("@", 1)[0] != spec.source().image_repository:
        raise ContractError("Verified release image differs from operator source")
    app_id = manifest.app_id
    existing = next(
        (item for item in policy.apps if item.registration.app_id == app_id), None
    )
    registered = next(
        (item for item in core.registrations if item.app_id == app_id), None
    )
    if record is not None or existing is not None:
        target = record.app if record is not None else existing
        assert target is not None
        account = Account(account_name(app_id), target.uid, target.gid)
        check_account(account, facts)
        expected = build_app(
            spec,
            manifest,
            account,
            target.installation.host_port,
            target.registration.presentation.position,
        )
        if expected != target or (existing is not None and existing != target):
            raise ContractError(
                "Existing application policy differs from the onboarding spec"
            )
    else:
        account, port = allocate(app_id, policy, facts, core)
        position = (
            registered.presentation.position
            if registered is not None
            else max((r.presentation.position for r in core.registrations), default=0)
            + 10
        )
        target = build_app(spec, manifest, account, port, position)
    registration = target.registration
    for reserved in facts.reserved_apps:
        if reserved.source.repository == spec.repository:
            if reserved != target:
                raise ContractError("Saved onboarding allocation differs from the plan")
            continue
        if (
            reserved.registration.app_id == app_id
            or routes_overlap(reserved.registration.route, registration.route)
            or reserved.installation.host_port == target.installation.host_port
            or {reserved.uid, reserved.gid} & {target.uid, target.gid}
        ):
            raise ContractError(
                "Application conflicts with another onboarding reservation"
            )
    if {target.uid, target.gid} & {policy.core_uid, policy.core_gid} or any(
        item.registration.app_id != app_id
        and {target.uid, target.gid} & {item.uid, item.gid}
        for item in policy.apps
    ):
        raise ContractError("Application IDs conflict with host policy identities")
    if existing is None and target.installation.host_port in facts.listening_ports:
        raise ContractError("Application port is already listening")
    if registration.app_id in CORE_APP_IDS or any(
        routes_overlap(registration.route, route) for route in CORE_ROUTE_TREES
    ):
        raise ContractError("Application ID or route is reserved")
    for item in (*core.registrations, *(a.registration for a in policy.apps)):
        if item.app_id == app_id:
            if item != registration:
                raise ContractError(
                    "Existing core registration differs from the onboarding spec"
                )
        elif routes_overlap(item.route, registration.route):
            raise ContractError("Application route overlaps an existing registration")
        elif (
            item.installation is not None
            and item.installation.host_port == target.installation.host_port
        ):
            raise ContractError("Application port overlaps an existing registration")
    if core.credential_path is not None and (
        core.credential_path != target.credential_file or not core.credential_valid
    ):
        raise ContractError("Existing active credential differs or fails verification")
    apps = policy.apps if existing is not None else (*policy.apps, target)
    candidate = parse_document(
        HostPolicy, policy.model_copy(update={"apps": apps}).model_dump_json().encode()
    )
    for old_app in policy.apps:
        other = old_app.registration.app_id
        if app_fingerprint(policy, other) != app_fingerprint(candidate, other):
            raise ContractError("Onboarding would change an existing app fingerprint")
    retained = OnboardingRecord(
        spec_sha256=fingerprint(spec), manifest=manifest, app=target
    )
    retained = parse_document(OnboardingRecord, retained.model_dump_json().encode())
    return OnboardingPlan(
        account=account,
        record=retained,
        candidate=candidate,
        policy_sha256=fingerprint(policy),
        core=core,
    )


def describe(plan: OnboardingPlan) -> str:
    app = plan.app
    reg = app.registration
    return "\n".join(
        (
            f"Application {reg.app_id} from {app.source.repository}",
            f"Route {reg.route}, access {reg.access}",
            f"Account {plan.account.name}, UID/GID {app.uid}/{app.gid}",
            f"Loopback port {app.installation.host_port}",
            f"State directory {reg.bindings.state_directory}",
            f"Backup directory {reg.bindings.backup_directory}",
            f"Credential file {app.credential_file}",
            "Existing app fingerprints remain unchanged.",
            "Registration and host policy only. Approve through the Apps page.",
        )
    )

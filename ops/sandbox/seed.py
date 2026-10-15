"""Synthetic state for the integration sandbox.

The sandbox owns every piece of core configuration. ``Plan`` is that
configuration as one value; ``seed`` converges a state directory and database
to it, so a restarted container reaches the same state as a fresh one.
"""

import hashlib
import json
import os
import secrets
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.accounts import AccountStore, UserCreate
from app.app_contracts import ContractError, ReleaseManifest, parse_document
from app.app_deployments import update_registration
from app.app_registry import AppRegistry, RegistrationConflict
from app.app_workloads import WorkloadStore
from app.config import InstanceFacts
from app.database import Database
from app.repository import JobRepository
from contracts.models import JobType

ENVIRONMENT = "sandbox"
BRIDGE_PORT = 19001
SAMPLE_CSV = b"name,score\nAda,1\nBen,3\n"
SYNTHETIC_COMMIT = "0" * 40
SYNTHETIC_IMAGE_DIGEST = "0" * 64
MARKERS = {"${SOURCE_COMMIT}", "${IMAGE}", "${SHA256}"}


class SandboxRefusal(Exception):
    pass


@dataclass(frozen=True)
class Persona:
    username: str
    login: str
    display_name: str
    enabled: bool


PERSONAS = (
    Persona("alex", "alex@sandbox.test", "Alex Sandbox", enabled=True),
    Persona("sam", "sam@sandbox.test", "Sam Sandbox", enabled=True),
    Persona("dana", "dana@sandbox.test", "Dana Sandbox", enabled=False),
)


@dataclass(frozen=True)
class AppDefinition:
    app_id: str
    route: str
    manifest_json: bytes
    manifest: ReleaseManifest
    archives: tuple[tuple[str, Path], ...]

    @classmethod
    def load(cls, source: Path, app_dir: Path, route: str | None) -> "AppDefinition":
        if not source.is_file():
            raise SandboxRefusal(f"{source} is required (a template is accepted)")
        try:
            resolved = resolve_manifest(_read_mounted(source), app_dir)
            manifest = parse_document(ReleaseManifest, resolved)
        except (OSError, ValueError, ContractError) as error:
            raise SandboxRefusal(f"Invalid app manifest: {error}") from None
        archives = tuple(
            (workload.id, app_dir / workload.asset) for workload in manifest.workloads
        )
        for _, archive in archives:
            _read_mounted(archive)
        return cls(
            manifest.app_id,
            route or "/apps/" + manifest.app_id,
            resolved,
            manifest,
            archives,
        )


@dataclass(frozen=True)
class Plan:
    root: Path
    ingress_token: str
    app: AppDefinition
    app_host: str
    app_port: int
    api_url: str
    worker_id: str

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def credentials_dir(self) -> Path:
        return self.root / "credentials"

    @property
    def database_path(self) -> Path:
        return self.state_dir / "core.db"

    @property
    def credential_file(self) -> Path:
        return self.credentials_dir / "app-credential"

    def core_environment(self) -> dict[str, str]:
        state = self.state_dir
        return {
            "HOME_PLATFORM_DB_PATH": str(self.database_path),
            "HOME_PLATFORM_API_TOKEN_FILE": str(state / "api-token"),
            "HOME_PLATFORM_APP_CONTEXT_KEY_FILE": str(state / "context-key"),
            "HOME_PLATFORM_ENVIRONMENT": ENVIRONMENT,
            "HOME_PLATFORM_APP_API_URL": self.api_url,
            "HOME_PLATFORM_UPLOAD_DIR": str(state / "uploads"),
            "HOME_PLATFORM_ARTIFACT_DIR": str(state / "artifacts"),
            "HOME_PLATFORM_STORAGE_DIR": str(state / "storage"),
            "HOME_PLATFORM_MEMBER_STORAGE_ENABLED": "true",
            "PREVIEW_APP_HOST": self.app_host,
            "PREVIEW_APP_PORT": str(self.app_port),
            "PREVIEW_BRIDGE_TOKEN_FILE": str(state / "ingress-token"),
        }

    def registration(self) -> bytes:
        runtime = self.app.manifest.runtime
        return json.dumps(
            {
                "app_id": self.app.app_id,
                "route": self.app.route,
                "source": {
                    "kind": "published",
                    "repository": "sandbox/" + self.app.app_id,
                    "branch": "main",
                    "workflow": ".github/workflows/publish.yml",
                    "image_repository": "ghcr.io/sandbox/" + self.app.app_id,
                },
                "access": "linked-user",
                "capabilities": list(self.app.manifest.capabilities),
                "presentation": {
                    "label": self.app.app_id,
                    "health_name": self.app.app_id,
                    "position": 50,
                    "accent": "#75d8a9",
                },
                "bindings": {"state_directory": str(self.root / "app-state")},
                "installation": {
                    "owner": "platform",
                    "host_port": BRIDGE_PORT,
                    "probe": {
                        "service": self.app.app_id,
                        "ready_path": runtime.ready_path,
                        "version_path": runtime.version_path,
                        "readiness": "v1-status",
                    },
                },
            }
        ).encode()


OWNED_PREFIXES = ("HOME_PLATFORM_", "HP_FIXTURE_", "PREVIEW_")
OWNED_NAMES = frozenset({"APP_ALLOW_DEV_IDENTITY", "HOME_PLATFORM_DEV_USER"})


def plan_from_environment(
    environ: Mapping[str, str], root: Path = Path("/sandbox")
) -> Plan:
    owned = sorted(
        name
        for name in environ
        if name.startswith(OWNED_PREFIXES) or name in OWNED_NAMES
    )
    if owned:
        raise SandboxRefusal(
            "The sandbox owns core and identity configuration; remove "
            + ", ".join(owned)
        )
    token = environ.get("HP_SANDBOX_TOKEN", "")
    if not 32 <= len(token) <= 512 or not token.isascii() or not token.isprintable():
        raise SandboxRefusal(
            "HP_SANDBOX_TOKEN must be 32 to 512 printable ASCII characters; export "
            "it before starting, for example HP_SANDBOX_TOKEN=$(openssl rand -hex 32)"
        )
    try:
        port = int(environ.get("HP_SANDBOX_APP_PORT", "8080"))
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise SandboxRefusal("HP_SANDBOX_APP_PORT must be a TCP port")
    return Plan(
        root=root,
        ingress_token=token,
        app=AppDefinition.load(
            root / "manifest.json", root / "app", environ.get("HP_SANDBOX_APP_ROUTE")
        ),
        app_host=environ.get("HP_SANDBOX_APP_HOST", "app"),
        app_port=port,
        api_url=environ.get("HP_SANDBOX_API_URL", "http://core:8000/api/apps/v1"),
        worker_id=environ.get("HP_SANDBOX_WORKER_ID", "sandbox-worker"),
    )


def resolve_manifest(template: bytes, app_dir: Path) -> bytes:
    """Fill a starter's unresolved markers with synthetic values.

    A manifest without markers passes through byte for byte, so a published
    manifest.json is ingested exactly as the platform would ingest it.
    """
    document = json.loads(template)
    if not isinstance(document, dict):
        raise ValueError("Manifest must be a JSON object")
    if not any(_contains_marker(value) for value in document.values()):
        return template
    app_id = document.get("app_id")
    if not isinstance(app_id, str):
        raise ValueError("Manifest app_id must be a string")
    if document.get("source_commit") == "${SOURCE_COMMIT}":
        document["source_commit"] = SYNTHETIC_COMMIT
    if document.get("image") == "${IMAGE}":
        document["image"] = f"ghcr.io/sandbox/{app_id}@sha256:{SYNTHETIC_IMAGE_DIGEST}"
    for workload in document.get("workloads", ()):
        if isinstance(workload, dict) and workload.get("sha256") == "${SHA256}":
            asset = workload.get("asset")
            if not isinstance(asset, str) or "/" in asset:
                raise ValueError("Workload asset must be a file name")
            workload["sha256"] = hashlib.sha256(
                _read_mounted(app_dir / asset)
            ).hexdigest()
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def _read_mounted(path: Path) -> bytes:
    """Read an author-mounted file, naming the fix when the mount is wrong."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise SandboxRefusal(
            f"{path} is missing; package the workload archives into the mounted "
            "directory before starting the sandbox"
        ) from None
    except PermissionError:
        raise SandboxRefusal(
            f"{path} is not readable by the sandbox user (uid {os.getuid()}); "
            "make the manifest and workload archives world-readable, for example "
            "by cloning with the default umask 022"
        ) from None


def _contains_marker(value: object) -> bool:
    if isinstance(value, str):
        return value in MARKERS
    if isinstance(value, dict):
        return any(_contains_marker(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_marker(item) for item in value)
    return False


def _refuse_foreign_database(path: Path, facts: InstanceFacts) -> None:
    """Refuse a database pinned to a different instance before anything touches it.

    ``Database.initialize()`` applies schema migrations unconditionally, so a
    wrongly mounted database used to get migrated before ``publish_instance``
    ever got a chance to refuse it. This opens a bare connection, with no
    migration and no write, and refuses on a mismatch. A database with no
    ``platform_instance`` row yet (new, or pre-dating that table) is not
    foreign; migration proceeds normally for it.
    """
    if not path.is_file():
        return
    connection = sqlite3.connect(path, timeout=5)
    try:
        row = connection.execute(
            "SELECT environment, app_api_url, timezone FROM platform_instance"
        ).fetchone()
    except sqlite3.DatabaseError:
        return
    finally:
        connection.close()
    if row is not None and tuple(row) != (
        facts.environment,
        facts.app_api_url,
        facts.timezone,
    ):
        raise SandboxRefusal(
            f"{path} already belongs to a different platform instance; refusing "
            "to migrate it"
        )


def seed(plan: Plan) -> dict[str, object]:
    """Converge files and database to the plan. Safe to repeat."""
    for directory in (plan.state_dir, plan.credentials_dir):
        directory.mkdir(parents=True, exist_ok=True)
    for name in ("api-token", "context-key"):
        _write_secret_once(plan.state_dir / name, secrets.token_urlsafe(48))
    _write_secret(plan.state_dir / "ingress-token", plan.ingress_token)
    facts = InstanceFacts(ENVIRONMENT, plan.api_url, "UTC")
    _refuse_foreign_database(plan.database_path, facts)
    database = Database(plan.database_path)
    try:
        database.initialize()
        registry = AppRegistry(database)
        registry.publish_instance(facts)
        users = _seed_personas(AccountStore(database), plan.state_dir / "storage")
        registration: str
        try:
            registration = registry.register(plan.registration())
        except RegistrationConflict:
            registration = update_registration(database, plan.registration())
        receipt = registry.ingest_release(plan.app.app_id, plan.app.manifest_json)
        workloads = WorkloadStore(database)
        installed = {
            workload_id: str(
                workloads.install(
                    plan.app.app_id, receipt.manifest_sha256, workload_id, archive
                ).id
            )
            for workload_id, archive in plan.app.archives
        }
        credential = _converge_credential(registry, plan)
        _register_worker(database, plan.worker_id)
    finally:
        database.close()
    return {
        "environment": ENVIRONMENT,
        "app_id": plan.app.app_id,
        "route": plan.app.route,
        "registration": registration,
        "release_sha256": receipt.manifest_sha256,
        "installed_workloads": installed,
        "credential": credential,
        "worker": plan.worker_id,
        "users": users,
    }


def _seed_personas(accounts: AccountStore, storage: Path) -> dict[str, str]:
    existing = {item.username: item for item in accounts.list()}
    users = {}
    for persona in PERSONAS:
        user = existing.get(persona.username) or accounts.create(
            UserCreate(username=persona.username, password=secrets.token_urlsafe(24))
        )
        accounts.link_external_identity(
            user.id, "tailscale", persona.login, persona.display_name
        )
        if user.disabled == persona.enabled:
            accounts.set_disabled(user.id, not persona.enabled)
        home = storage / "users" / str(user.id) / "Inputs"
        home.mkdir(parents=True, exist_ok=True)
        _write_once(home / "sample.csv", SAMPLE_CSV)
        users[persona.login] = str(user.id)
    shared = storage / "shared" / "Inputs"
    shared.mkdir(parents=True, exist_ok=True)
    _write_once(shared / "shared.csv", SAMPLE_CSV)
    return users


def _converge_credential(registry: AppRegistry, plan: Plan) -> str:
    active = registry.active_credential(plan.app.app_id)
    target = plan.credential_file
    if active is not None and active.file_path == target and target.is_file():
        return "retained"
    if target.exists() or target.is_symlink():
        target.unlink()
    return registry.issue_credential(
        plan.app.app_id, target, rotate=active is not None
    ).outcome


def _register_worker(database: Database, worker_id: str) -> None:
    """Admit submissions before the disposable worker's first heartbeat.

    The worker's own heartbeat later updates liveness but keeps the enabled flag
    and capacity set here.
    """
    repository = JobRepository(database)
    now = datetime.now(UTC)
    repository.heartbeat(worker_id, [JobType.BATCH], now, now, None, None)
    stale_before = now - timedelta(seconds=30)
    repository.set_worker_enabled(worker_id, True, stale_before)
    repository.set_worker_capacity(worker_id, 1.0, 512, stale_before)


def _write_secret_once(path: Path, value: str) -> None:
    if not path.is_file():
        _write_secret(path, value)


def _write_secret(path: Path, value: str) -> None:
    if path.is_file() and path.read_text() == value + "\n":
        return
    path.write_text(value + "\n")
    path.chmod(0o600)


def _write_once(path: Path, data: bytes) -> None:
    if not path.exists():
        path.write_bytes(data)

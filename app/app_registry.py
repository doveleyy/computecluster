import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID

from app.app_contracts import (
    ContractError,
    ManagedInstalled,
    PublishedSource,
    Readiness,
    Registration,
    ReleaseManifest,
    host_path,
    parse_document,
)
from app.app_deployment_contracts import SWITCHING_KINDS, RetiredSnapshot, fingerprint
from app.config import InstanceFacts
from app.database import Database
from app.platform_apps import (
    ADMINISTRATOR_ONLY,
    BUILTIN_APPS,
    EVERYONE,
    PlatformApp,
    Role,
)

CORE_ROUTE_TREES = frozenset(
    {
        "/api",
        "/dashboard",
        "/datasets",
        "/docs",
        "/health",
        "/inputs",
        "/internal",
        "/job-groups",
        "/jobs",
        "/jobs-ui",
        "/openapi.json",
        "/platform",
        "/projects",
        "/ready",
        "/redoc",
        "/scripts",
        "/static",
        "/storage",
        "/version",
        "/workers",
    }
)
CORE_APP_IDS = frozenset({"home", "jobs", "dashboard"})


class RegistrationConflict(ContractError):
    pass


class AppNotServed(LookupError):
    pass


@dataclass(frozen=True)
class ProbeTarget:
    key: str
    name: str
    path: str
    port: int
    service: str
    ready_path: str
    version_path: str
    readiness: Readiness


@dataclass(frozen=True)
class ReleaseReceipt:
    app_id: str
    manifest_sha256: str
    outcome: Literal["recorded", "unchanged"]


@dataclass(frozen=True)
class AppSummary:
    app_id: str
    route: str
    label: str
    access: str
    source: Literal["legacy", "published"]
    installation: Literal["legacy-scripts", "platform"] | None
    release_count: int
    registration_sha256: str


def installs_required() -> bool:
    """Whether managed apps are served only from an accepted installation.

    Previews and local runs have no installer policy and serve a managed
    registration directly.
    """
    return "HOME_PLATFORM_INSTALLER_POLICY" in os.environ


def routes_overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


@lru_cache(maxsize=256)
def decode_registration(app_id: str, route: str, definition: str) -> Registration:
    try:
        value = json.loads(definition)
    except ValueError:
        value = None
    if not isinstance(value, dict):
        raise ContractError("Invalid JSON contract document")
    value.update(app_id=app_id, route=route)
    return parse_document(Registration, json.dumps(value).encode())


@dataclass(frozen=True)
class ManagedApp:
    registration: Registration
    installation: ManagedInstalled

    @property
    def app_id(self) -> str:
        return self.registration.app_id

    @property
    def route(self) -> str:
        return self.registration.route


@dataclass(frozen=True)
class AppCredential:
    credential_id: str
    app_id: str
    environment: str
    file_path: Path


@dataclass(frozen=True)
class IssuedCredential:
    credential_id: str
    app_id: str
    environment: str
    file_path: str
    outcome: Literal["issued", "rotated"]


class EnvironmentMismatch(ContractError):
    pass


class AppRegistry:
    def __init__(self, database: Database) -> None:
        self.database = database

    def managed_app(self, app_id: str) -> ManagedApp | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT route, definition_json FROM app_registrations WHERE app_id = ?",
                (app_id,),
            ).fetchone()
        if row is None:
            return None
        registration = decode_registration(app_id, row["route"], row["definition_json"])
        registration = self._installed_registration(registration)
        if not isinstance(registration.installation, ManagedInstalled):
            return None
        return ManagedApp(registration, registration.installation)

    def managed_route(self, path: str) -> ManagedApp | None:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT app_id, route, definition_json FROM app_registrations "
                "WHERE route = ? OR substr(?, 1, length(route) + 1) = route || '/'",
                (path, path),
            ).fetchall()
        for row in rows:
            registration = decode_registration(
                row["app_id"], row["route"], row["definition_json"]
            )
            registration = self._installed_registration(registration)
            if isinstance(registration.installation, ManagedInstalled):
                return ManagedApp(registration, registration.installation)
        return None

    def _installed_registration(self, registration: Registration) -> Registration:
        from app.app_deployments import installed

        facts = self.instance()
        if facts is None:
            return registration
        with self.database.connect() as connection:
            snapshot = installed(connection, registration.app_id, facts.environment)
            if snapshot is None:
                return registration
            row = connection.execute(
                (
                    "SELECT manifest_bytes FROM app_releases WHERE app_id = ? AND"
                    " manifest_sha256 = ?"
                ),
                (registration.app_id, snapshot.selection.manifest_sha256),
            ).fetchone()
        if row is None:
            raise ContractError("Installed release is unavailable")
        release = parse_document(ReleaseManifest, bytes(row[0]))
        return snapshot.registration.model_copy(
            update={"capabilities": release.capabilities}
        )

    def admission_refusal(self, app_id: str, *, require_installed: bool) -> str | None:
        """Why the gateway holds requests for this app, or None to admit them."""
        from app.app_deployments import installed, retirement

        facts = self.instance()
        kinds = sorted(SWITCHING_KINDS)
        with self.database.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM app_operations WHERE app_id = ? AND kind IN ("
                + ",".join("?" * len(kinds))
                + ") AND state IN ('switching', 'blocked')",
                (app_id, *kinds),
            ).fetchone():
                return "Application installation is pending"
            if facts is not None and retirement(connection, app_id, facts.environment):
                return "Application is retired"
            if require_installed and (
                facts is None
                or installed(connection, app_id, facts.environment) is None
            ):
                return (
                    "Application is not installed; approve a release from the Apps page"
                )
        return None

    def retirement(self, app_id: str) -> RetiredSnapshot | None:
        from app.app_deployments import retirement

        facts = self.instance()
        if facts is None:
            return None
        with self.database.connect() as connection:
            return retirement(connection, app_id, facts.environment)

    def publish_instance(self, facts: InstanceFacts) -> None:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT environment, app_api_url, timezone FROM platform_instance"
            ).fetchone()
            values = (facts.environment, facts.app_api_url, facts.timezone)
            if row is None:
                connection.execute(
                    "INSERT INTO platform_instance VALUES (1, ?, ?, ?)", values
                )
            elif tuple(row) != values:
                raise EnvironmentMismatch(
                    "Database application instance configuration does not match"
                )

    def instance(self) -> InstanceFacts | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT environment, app_api_url, timezone FROM platform_instance"
            ).fetchone()
        return InstanceFacts(*row) if row is not None else None

    def issue_credential(
        self, app_id: str, file_path: Path, *, rotate: bool = False
    ) -> IssuedCredential:
        try:
            host_path(str(file_path))
        except ValueError:
            raise ContractError("Invalid credential host file path") from None
        instance = self.instance()
        if instance is None:
            raise ContractError("Application instance is not configured")
        if self.managed_app(app_id) is None:
            raise ContractError("Application is not a managed installation")
        credential_id = secrets.token_hex(16)
        token = f"hpa1.{credential_id}.{secrets.token_urlsafe(32)}"
        verifier = hashlib.sha256(token.encode()).hexdigest()
        temporary: Path | None = None
        installed = False
        resolved = file_path.resolve()
        for registration in self._registrations():
            state = registration.bindings.state_directory
            if state is not None and (
                resolved == Path(state).resolve()
                or Path(state).resolve() in resolved.parents
            ):
                raise ContractError(
                    "Credential file must be outside application writable state"
                )
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                prefix=".app-credential-", dir=file_path.parent
            )
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as target:
                target.write(token.encode() + b"\n")
                target.flush()
                os.fsync(target.fileno())
            if file_path.exists() or file_path.is_symlink():
                raise ContractError(
                    "Credential target file already exists; use a fresh path"
                )
            os.link(temporary, file_path)
            installed = True
            temporary.unlink()
            temporary = None
            directory_fd = os.open(file_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                active = connection.execute(
                    "SELECT credential_id FROM app_credentials "
                    "WHERE app_id = ? AND revoked_at IS NULL",
                    (app_id,),
                ).fetchone()
                if active is not None and not rotate:
                    raise ContractError(
                        "An active credential exists; use --rotate to replace it"
                    )
                owner = connection.execute(
                    "SELECT app_id FROM app_credentials WHERE file_path = ? "
                    "AND revoked_at IS NULL AND app_id != ?",
                    (str(file_path), app_id),
                ).fetchone()
                if owner is not None:
                    raise ContractError(
                        "Credential file is already used by another application"
                    )
                if active is not None:
                    connection.execute(
                        "UPDATE app_credentials SET revoked_at = CURRENT_TIMESTAMP "
                        "WHERE credential_id = ?",
                        (active["credential_id"],),
                    )
                connection.execute(
                    "INSERT INTO app_credentials "
                    "(credential_id, app_id, environment, verifier, file_path) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        credential_id,
                        app_id,
                        instance.environment,
                        verifier,
                        str(file_path),
                    ),
                )
        except Exception:
            if installed:
                try:
                    file_path.unlink()
                except OSError:
                    raise ContractError(
                        "Credential issuance failed; active credential was retained; "
                        "an inactive new credential file remains"
                    ) from None
            raise
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        return IssuedCredential(
            credential_id,
            app_id,
            instance.environment,
            str(file_path),
            "rotated" if active is not None else "issued",
        )

    def verify_credential(self, token: str) -> AppCredential | None:
        if not re.fullmatch(r"hpa1\.[0-9a-f]{32}\.[A-Za-z0-9_-]{43}", token):
            return None
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT credential_id, app_id, environment, verifier, file_path "
                "FROM app_credentials WHERE credential_id = ? AND revoked_at IS NULL",
                (token.split(".")[1],),
            ).fetchone()
        if row is None or not hmac.compare_digest(
            row["verifier"], hashlib.sha256(token.encode()).hexdigest()
        ):
            return None
        return AppCredential(
            row["credential_id"],
            row["app_id"],
            row["environment"],
            Path(row["file_path"]),
        )

    def active_credential(self, app_id: str) -> AppCredential | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT credential_id, app_id, environment, file_path "
                "FROM app_credentials WHERE app_id = ? AND revoked_at IS NULL",
                (app_id,),
            ).fetchone()
        return (
            AppCredential(
                row["credential_id"],
                row["app_id"],
                row["environment"],
                Path(row["file_path"]),
            )
            if row is not None
            else None
        )

    def register(self, operator_json: bytes) -> Literal["recorded", "unchanged"]:
        registration = parse_document(Registration, operator_json)
        definition = json.dumps(
            registration.model_dump(mode="json", exclude={"app_id", "route"}),
            sort_keys=True,
            separators=(",", ":"),
        )
        if registration.app_id in CORE_APP_IDS or any(
            routes_overlap(registration.route, tree) for tree in CORE_ROUTE_TREES
        ):
            raise RegistrationConflict("Application ID or route is reserved")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT route, definition_json FROM app_registrations WHERE app_id = ?",
                (registration.app_id,),
            ).fetchone()
            if previous is not None:
                if (
                    decode_registration(
                        registration.app_id,
                        previous["route"],
                        previous["definition_json"],
                    )
                    == registration
                ):
                    return "unchanged"
                raise RegistrationConflict(
                    "Application registration already has another definition"
                )
            routes = connection.execute(
                "SELECT route FROM app_registrations"
            ).fetchall()
            if any(routes_overlap(registration.route, row["route"]) for row in routes):
                raise RegistrationConflict(
                    "Application route overlaps another registration"
                )
            connection.execute(
                "INSERT INTO app_registrations (app_id, route, definition_json) "
                "VALUES (?, ?, ?)",
                (registration.app_id, registration.route, definition),
            )
        return "recorded"

    def ingest_release(self, app_id: str, manifest_json: bytes) -> ReleaseReceipt:
        manifest = parse_document(ReleaseManifest, manifest_json)
        digest = hashlib.sha256(manifest_json).hexdigest()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT route, definition_json FROM app_registrations WHERE app_id = ?",
                (app_id,),
            ).fetchone()
            if row is None:
                raise ContractError("Application is not registered")
            registration = decode_registration(
                app_id, row["route"], row["definition_json"]
            )
            self._validate_release(registration, manifest)
            previous = connection.execute(
                "SELECT manifest_bytes FROM app_releases "
                "WHERE app_id = ? AND manifest_sha256 = ?",
                (app_id, digest),
            ).fetchone()
            if previous is not None:
                if bytes(previous["manifest_bytes"]) != manifest_json:
                    raise ContractError("Release digest conflicts with retained bytes")
                return ReleaseReceipt(app_id, digest, "unchanged")
            connection.execute(
                "INSERT INTO app_releases (app_id, manifest_sha256, manifest_bytes) "
                "VALUES (?, ?, ?)",
                (app_id, digest, manifest_json),
            )
        return ReleaseReceipt(app_id, digest, "recorded")

    @staticmethod
    def _validate_release(
        registration: Registration, manifest: ReleaseManifest
    ) -> None:
        if not isinstance(registration.source, PublishedSource):
            raise ContractError("Legacy source has no published release authority")
        if manifest.app_id != registration.app_id:
            raise ContractError("Release application ID does not match registration")
        if manifest.image.split("@", 1)[0] != registration.source.image_repository:
            raise ContractError("Release image repository is not permitted")
        if not set(manifest.capabilities).issubset(registration.capabilities):
            raise ContractError("Release requests an ungranted capability")
        if not set(manifest.secrets).issubset(
            secret.name for secret in registration.bindings.secret_files
        ):
            raise ContractError("Release requests an unknown secret binding")
        if any(
            item.timeout_seconds
            > registration.bindings.resources.maintenance_timeout_seconds
            for item in manifest.maintenance
        ):
            raise ContractError("Release maintenance timeout exceeds operator policy")

    def _registrations(self) -> tuple[Registration, ...]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT app_id, route, definition_json FROM app_registrations "
                "ORDER BY app_id"
            ).fetchall()
        return tuple(
            decode_registration(row["app_id"], row["route"], row["definition_json"])
            for row in rows
        )

    def _accepted_installs(self) -> frozenset[str]:
        facts = self.instance()
        if facts is None:
            return frozenset()
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT app_id FROM app_installed_history AS latest "
                "WHERE environment = ? AND kind = 'installed' AND generation = "
                "(SELECT max(generation) FROM app_installed_history WHERE app_id = "
                "latest.app_id AND environment = latest.environment)",
                (facts.environment,),
            ).fetchall()
        return frozenset(row[0] for row in rows)

    def _served(self) -> tuple[Registration, ...]:
        """Installed registrations the gateway would admit."""
        accepted = self._accepted_installs() if installs_required() else None
        return tuple(
            registration
            for registration in self._registrations()
            if registration.installation is not None
            and not (
                accepted is not None
                and isinstance(registration.installation, ManagedInstalled)
                and registration.app_id not in accepted
            )
        )

    def navigation(self, role: Role | None = None) -> tuple[PlatformApp, ...]:
        positioned = list(BUILTIN_APPS)
        for registration in self._served():
            presentation = registration.presentation
            positioned.append(
                (
                    presentation.position,
                    PlatformApp(
                        registration.app_id,
                        presentation.label,
                        registration.route + "/",
                        EVERYONE
                        if registration.access == "linked-user"
                        else ADMINISTRATOR_ONLY,
                        presentation.accent,
                        presentation.kicker,
                        presentation.description,
                    ),
                )
            )
        positioned.sort(key=lambda pair: (pair[0], pair[1].key))
        return tuple(app for _, app in positioned if role is None or role in app.roles)

    def pins(self, user_id: UUID) -> frozenset[str]:
        """The registered apps this user keeps in the bar. A preference only:
        navigation still decides what the user may open."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT app_id FROM app_pins WHERE user_id = ?", (str(user_id),)
            ).fetchall()
        return frozenset(row[0] for row in rows)

    def pin(self, user_id: UUID, app_id: str) -> None:
        if app_id not in {registration.app_id for registration in self._served()}:
            raise AppNotServed(app_id)
        with self.database.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO app_pins (user_id, app_id) VALUES (?, ?)",
                (str(user_id), app_id),
            )

    def unpin(self, user_id: UUID, app_id: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM app_pins WHERE user_id = ? AND app_id = ?",
                (str(user_id), app_id),
            )

    def monitoring(self) -> tuple[ProbeTarget, ...]:
        targets = []
        for registration in sorted(
            self._served(),
            key=lambda item: (item.presentation.position, item.app_id),
        ):
            installed = registration.installation
            if installed is None or installed.probe is None:
                continue
            probe = installed.probe
            targets.append(
                ProbeTarget(
                    registration.app_id,
                    registration.presentation.health_name,
                    registration.route,
                    installed.host_port,
                    probe.service,
                    probe.ready_path,
                    probe.version_path,
                    probe.readiness,
                )
            )
        return tuple(targets)

    def summaries(self) -> tuple[AppSummary, ...]:
        with self.database.connect() as connection:
            counts = dict(
                connection.execute(
                    "SELECT app_id, COUNT(*) FROM app_releases GROUP BY app_id"
                ).fetchall()
            )
        return tuple(
            AppSummary(
                item.app_id,
                item.route,
                item.presentation.label,
                item.access,
                item.source.kind,
                item.installation.owner if item.installation else None,
                counts.get(item.app_id, 0),
                fingerprint(item),
            )
            for item in self._registrations()
        )

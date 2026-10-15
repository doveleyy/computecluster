import hashlib
import os
import stat
import sys
from pathlib import Path
from typing import Annotated

from pydantic import Field, model_validator

from app.app_contracts import (
    Contract,
    ContractError,
    HostMount,
    HostPath,
    Identifier,
    Maintenance,
    ManagedInstalled,
    PublishedSource,
    Registration,
    SQLiteState,
    Text,
    UnitPath,
    parse_document,
    require_unique,
)
from app.app_deployment_contracts import fingerprint
from app.app_spool import read_owned


class PolicyRefused(ContractError):
    pass


MAINTENANCE_INTERVALS = (1, 2, 3, 4, 6, 8, 12, 24, 168)
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def maintenance_calendar(
    app_id: str, item: Maintenance, *, offset_minutes: int | None = None
) -> str:
    """systemd OnCalendar expression for one maintenance task.

    Monotonic timers re-armed on every daemon-reload (systemd 255 ran a 24 h
    task twice ten minutes apart in candidate-9 staging); calendar timers keep
    their wall-clock schedule across reload, reinstall and, with Persistent,
    reboot.
    """
    hours = item.interval_hours
    if hours not in MAINTENANCE_INTERVALS:
        raise ValueError(
            "Maintenance interval_hours must be one of "
            + ", ".join(str(value) for value in MAINTENANCE_INTERVALS)
        )
    digest = hashlib.sha256(f"{app_id}/{item.name}".encode()).digest()
    # A stable offset inside the task's own period spreads apps apart and
    # keeps daily tasks away from the midnight backup.
    offset = (
        int.from_bytes(digest[:4], "big") % (hours * 60)
        if offset_minutes is None
        else offset_minutes
    )
    if not 0 <= offset < hours * 60:
        raise ValueError("Maintenance offset must be within its interval")
    day, minute_of_day = divmod(offset, 24 * 60)
    hour, minute = divmod(minute_of_day, 60)
    if hours == 168:
        return f"{WEEKDAYS[day]} *-*-* {hour:02d}:{minute:02d}:00"
    if hours == 24:
        return f"*-*-* {hour:02d}:{minute:02d}:00"
    return f"*-*-* {hour:02d}/{hours}:{minute:02d}:00"


class MaintenanceSchedule(Contract):
    name: Identifier
    offset_minutes: Annotated[int, Field(ge=0, lt=168 * 60)]
    jitter_seconds: Annotated[int, Field(ge=0, le=3600)] = 0


class BackupSchedule(Contract):
    hour: Annotated[int, Field(ge=0, le=23)]
    minute: Annotated[int, Field(ge=0, le=59)]
    jitter_seconds: Annotated[int, Field(ge=0, le=3600)] = 0
    requires_mounts_for: Annotated[tuple[UnitPath, ...], Field(max_length=16)] = ()

    @model_validator(mode="after")
    def unique_mounts(self) -> "BackupSchedule":
        require_unique(self.requires_mounts_for)
        return self


class HostApp(Contract):
    registration: Registration
    uid: Annotated[int, Field(ge=1)]
    gid: Annotated[int, Field(ge=1)]
    credential_file: HostPath
    compatibility_argv: Annotated[tuple[Text, ...], Field(min_length=1, max_length=32)]
    state: SQLiteState | None = None
    maintenance: Annotated[tuple[Maintenance, ...], Field(max_length=32)] = ()
    maintenance_schedules: Annotated[
        tuple[MaintenanceSchedule, ...], Field(max_length=32)
    ] = ()
    backup_schedule: BackupSchedule | None = None
    mounts: Annotated[tuple[HostMount, ...], Field(max_length=32)] = ()

    @model_validator(mode="after")
    def managed_and_published(self) -> "HostApp":
        registration = self.registration
        if (
            not isinstance(registration.installation, ManagedInstalled)
            or not isinstance(registration.source, PublishedSource)
            or registration.bindings.backup_directory is None
        ):
            raise ValueError("Policy requires a managed app with private backups")
        names = tuple(mount.name for mount in self.mounts)
        require_unique(names)
        if set(names) != set(registration.bindings.integration_bindings):
            raise ValueError("Policy mounts must match the registered integrations")
        require_unique(tuple(item.name for item in self.maintenance))
        for item in self.maintenance:
            maintenance_calendar(registration.app_id, item)
        require_unique(tuple(item.name for item in self.maintenance_schedules))
        tasks = {item.name: item for item in self.maintenance}
        for schedule in self.maintenance_schedules:
            if schedule.name not in tasks:
                raise ValueError("Maintenance schedule names an unknown task")
            task = tasks[schedule.name]
            maintenance_calendar(
                registration.app_id, task, offset_minutes=schedule.offset_minutes
            )
            if schedule.jitter_seconds >= task.interval_hours * 3600:
                raise ValueError("Maintenance jitter must be within its interval")
        if self.backup_schedule is not None and self.state is None:
            raise ValueError("Backup schedule requires SQLite state")
        return self

    @property
    def source(self) -> PublishedSource:
        source = self.registration.source
        if not isinstance(source, PublishedSource):
            raise ContractError("Published source required")
        return source

    @property
    def installation(self) -> ManagedInstalled:
        installation = self.registration.installation
        if not isinstance(installation, ManagedInstalled):
            raise ContractError("Managed installation required")
        return installation


class HostPolicy(Contract):
    core_uid: Annotated[int, Field(ge=1)]
    core_gid: Annotated[int, Field(ge=1)]
    database: HostPath
    spool: HostPath
    journal: HostPath
    scratch: HostPath
    units: HostPath
    python: HostPath
    environment: str
    app_api_url: str
    timezone: str
    apps: Annotated[tuple[HostApp, ...], Field(min_length=1, max_length=32)]
    # A root-owned Docker config directory whose config.json holds the registry
    # login root pulls with. Only root reads it; no app or core process does.
    registry_credentials: HostPath | None = None

    @model_validator(mode="after")
    def unique_bindings(self) -> "HostPolicy":
        roots = [
            self.spool,
            self.journal,
            self.scratch,
            self.units,
            str(Path(self.database).parent),
        ]
        if self.registry_credentials is not None:
            roots.append(self.registry_credentials)
        ids, ports, uids = set(), set(), {self.core_uid}
        for app in self.apps:
            registration = app.registration
            if (
                registration.app_id in ids
                or app.installation.host_port in ports
                or app.uid in uids
            ):
                raise ValueError("Policy app IDs, ports and user IDs must be separate")
            ids.add(registration.app_id)
            ports.add(app.installation.host_port)
            uids.add(app.uid)
            roots.extend(
                (
                    str(registration.bindings.state_directory),
                    str(registration.bindings.backup_directory),
                    str(Path(app.credential_file).parent),
                )
            )
            roots.extend(
                str(Path(item.file_path).parent)
                for item in registration.bindings.secret_files
            )
            roots.extend(mount.source for mount in app.mounts)
        paths = [Path(item) for item in roots]
        for index, path in enumerate(paths):
            for other in paths[index + 1 :]:
                if path == other or path in other.parents or other in path.parents:
                    raise ValueError("Host policy roots must not alias or nest")
        return self

    def app(self, app_id: str) -> HostApp:
        for app in self.apps:
            if app.registration.app_id == app_id:
                return app
        raise ContractError("Application is outside host policy")


def root_path(path: Path, *, leaf_uid: int = 0, directory: bool = True) -> None:
    for parent in (*reversed(path.parents), path):
        info = parent.lstat()
        expected_uid = leaf_uid if parent == path else 0
        if (
            stat.S_ISLNK(info.st_mode)
            or info.st_uid != expected_uid
            or info.st_mode & 0o022
            or (parent != path and not stat.S_ISDIR(info.st_mode))
        ):
            raise ContractError("Host path ownership is unsafe")
    info = path.lstat()
    if directory != stat.S_ISDIR(info.st_mode):
        raise ContractError("Host path has an unexpected type")


def integration_sources(app: HostApp) -> None:
    # An unmounted share leaves its root-owned mount point behind; requiring
    # the app's ownership stops the app from starting against that empty
    # directory. validate_paths never calls this, so routine passes leave the
    # share asleep.
    for mount in app.mounts:
        try:
            info = Path(mount.source).lstat()
        except FileNotFoundError:
            raise ContractError("Integration mount source is missing") from None
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != app.uid:
            raise ContractError(
                "Integration mount source must be a directory owned by the app"
            )
        # The policy's alias and nesting check is lexical, so a symlinked
        # ancestor could point the bind at another root's directory.
        if os.path.realpath(mount.source) != os.path.normpath(mount.source):
            raise ContractError("Integration mount source resolves through a symlink")


def load_policy(path: Path) -> HostPolicy:
    root_path(path, directory=False)
    return parse_document(HostPolicy, read_owned(path, 0))


def policy_digest(path: Path) -> str:
    return fingerprint(load_policy(path))


def app_fingerprint(policy: HostPolicy, app_id: str) -> str:
    # Other apps, the request spool, the scratch root and the registry login
    # never reach this app's container or units, so editing them must not
    # invalidate its installation.
    shared = policy.model_dump(
        mode="json", exclude={"apps", "spool", "scratch", "registry_credentials"}
    )
    app = policy.app(app_id).model_dump(mode="json")
    for key in ("maintenance_schedules", "mounts"):
        if not app[key]:
            app.pop(key)
    if app["backup_schedule"] is None:
        app.pop("backup_schedule")
    return fingerprint({**shared, "app": app})


def registry_credentials(policy: HostPolicy) -> tuple[str, ...]:
    """Docker arguments that select the root-owned registry login, if any."""
    directory = policy.registry_credentials
    if directory is None:
        return ()
    for path, is_directory in (
        (Path(directory), True),
        (Path(directory) / "config.json", False),
    ):
        root_path(path, directory=is_directory)
        if path.lstat().st_mode & 0o077:
            raise ContractError("Registry credential must be private to root")
    return ("--config", directory)


def validate_paths(policy: HostPolicy) -> None:
    if not sys.flags.isolated:
        raise ContractError("Installer requires an isolated Python invocation")
    for entry in sys.path:
        path = Path(entry).resolve()
        if path.exists():
            root_path(path, directory=path.is_dir())
    for module in tuple(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if filename and Path(filename).exists():
            root_path(Path(filename).resolve(), directory=False)
    for value in (policy.journal, policy.scratch, policy.units):
        root_path(Path(value))
    if not Path(policy.scratch).lstat().st_mode & 0o001:
        raise ContractError("Scratch root must be traversable by application accounts")
    root_path(Path(policy.spool))
    root_path(Path(policy.spool) / "requests", leaf_uid=policy.core_uid)
    root_path(Path(policy.spool) / "receipts")
    root_path(Path(policy.python), directory=False)
    root_path(Path(policy.database).parent, leaf_uid=policy.core_uid)
    registry_credentials(policy)
    for app in policy.apps:
        root_path(
            Path(str(app.registration.bindings.state_directory)), leaf_uid=app.uid
        )
        backups = Path(str(app.registration.bindings.backup_directory))
        root_path(backups)
        if backups.lstat().st_mode & 0o077:
            raise ContractError("Backup directory must be private to root")
        root_path(Path(app.credential_file).parent, leaf_uid=policy.core_uid)
        read_owned(Path(app.credential_file), policy.core_uid)
        for item in app.registration.bindings.secret_files:
            root_path(Path(item.file_path), directory=False)
        if app.backup_schedule is not None:
            for mount in app.backup_schedule.requires_mounts_for:
                root_path(Path(mount))
    if os.geteuid() != 0:
        raise ContractError("Installer requires the root service identity")

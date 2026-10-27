import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar, Literal, get_args
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, Field

from app.app_contracts import (
    ContractError,
    Readiness,
    integration_variable,
    parse_document,
)
from app.app_deployment_contracts import (
    AcceptedSnapshot,
    ArchiveRecord,
    InstalledSnapshot,
    InstallerCapabilities,
    Kind,
    Operation,
    OperationHint,
    Receipt,
    ReceiptError,
    Rehearsal,
    RetiredSnapshot,
    decode_accepted,
    fingerprint,
    rehearsal_checks,
    running_of,
)
from app.app_deployments import Authority, ReconciliationPending
from app.app_publication import VerifiedPublication, verify_package
from app.app_registry import AppCredential, ManagedApp
from app.app_runtime import Mount, RuntimeSpec, docker_run_argv, runtime_spec
from app.app_spool import publish, read_owned, write_private
from app.config import InstanceFacts
from ops.installer_child import DEFERRED
from ops.installer_policy import (
    HostApp,
    HostPolicy,
    PolicyRefused,
    app_fingerprint,
    integration_sources,
    load_policy,
    maintenance_calendar,
    registry_credentials,
    root_path,
    validate_paths,
)

PENDING_RELOAD = ".daemon-reload-pending"
SCRATCH_ENTRY = re.compile(r"(?:rehearsal|input|backup)-[A-Za-z0-9_]+")
# epoll takes its timeout as a millisecond int, so one wait longer than about
# 24 days overflows it; the supervisor's year-long wait polls in short slices.
SELECT_INTERVAL = 60.0


def wait_interval(remaining: float) -> float:
    return min(remaining, SELECT_INTERVAL)


class HostOperationFailed(ContractError):
    def __init__(self, returncode: int) -> None:
        super().__init__("Host operation failed")
        self.returncode = returncode


class SwitchRefused(ContractError):
    """A refusal whose receipt names its own error instead of a generic failure."""

    error: ClassVar[ReceiptError] = "installation_failed"


class RecordsChanged(SwitchRefused):
    error = "records_changed"

    def __init__(self) -> None:
        super().__init__("Migration changes semantic application records")


class Contradictory(SwitchRefused):
    """A container root does not recognise; nothing is touched until recovery."""

    error = "recovery_required"

    def __init__(self) -> None:
        super().__init__("Container contradicts installation history")


class RetirementBackupFailed(SwitchRefused):
    error = "backup_failed"

    def __init__(self) -> None:
        super().__init__("Final archive could not be taken or verified")


class StateMissing(SwitchRefused):
    error = "state_missing"

    def __init__(self) -> None:
        super().__init__("Retired application state is missing")


class StateChanged(SwitchRefused):
    error = "state_changed"

    def __init__(self) -> None:
        super().__init__("Retired application state differs from its archive")


@dataclass(frozen=True)
class BackupFacts:
    """What backup() verified about the copy it published."""

    sha256: str
    content_sha256: str
    tables: tuple[str, ...]
    size_bytes: int


@dataclass(frozen=True)
class HostRelease:
    """What root may learn from a verified release.

    verify_package binds the image to the policy's repository; the port and
    paths only steer probes at the app's own container. Everything else the
    manifest declares comes from the root policy entry instead.
    """

    image: str
    port: int
    ready_path: str
    version_path: str
    manifest_sha256: str
    archive_sha256: str
    archive: bytes


def narrow(release: VerifiedPublication) -> HostRelease:
    runtime = release.manifest.runtime
    return HostRelease(
        release.manifest.image,
        runtime.port,
        runtime.ready_path,
        runtime.version_path,
        release.publication.manifest_sha256,
        release.artifact_sha256,
        release.archive,
    )


def sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def replace_file(path: Path, payload: bytes) -> None:
    temporary = path.with_name("." + path.name + "-" + str(uuid4()))
    publish(temporary, payload)
    os.replace(temporary, path)
    sync_directory(path.parent)


class ContainerConfig(BaseModel):
    labels: dict[str, str] = Field(alias="Labels")


class ContainerLimits(BaseModel):
    memory: int = Field(alias="Memory")
    memory_swap: int = Field(alias="MemorySwap")
    pids: int = Field(alias="PidsLimit")
    cpu: int = Field(alias="NanoCpus")


class ContainerState(BaseModel):
    running: bool = Field(alias="Running")
    pid: int = Field(default=0, alias="Pid")


class Container(BaseModel):
    image: str = Field(alias="Image")
    config: ContainerConfig = Field(alias="Config")
    limits: ContainerLimits = Field(alias="HostConfig")
    state: ContainerState = Field(alias="State")


def capture(
    argv: tuple[str, ...],
    *,
    timeout: int,
    limit: int,
    uid: int | None = None,
    gid: int | None = None,
) -> bytes:
    with subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        user=uid,
        group=gid,
        extra_groups=() if uid is not None else None,
        cwd="/",
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/nonexistent"},
    ) as process:
        assert process.stdout is not None and process.stderr is not None
        output = bytearray()
        size = 0
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                selector.register(process.stderr, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ContractError("Host operation timed out")
                    for key, _ in selector.select(wait_interval(remaining)):
                        chunk = os.read(key.fd, min(65536, limit + 1))
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        size += len(chunk)
                        if size > limit:
                            raise ContractError("Host operation output exceeds limit")
                        if key.fileobj is process.stdout:
                            output.extend(chunk)
            status = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if status:
                raise HostOperationFailed(status)
        except BaseException:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
    return bytes(output)


def response_body(client: httpx.Client, url: str) -> bytes:
    with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
        if (
            response.status_code != 200
            or response.headers.get("Content-Encoding", "identity") != "identity"
        ):
            raise ContractError("Application readiness failed")
        payload = bytearray()
        for chunk in response.iter_raw(chunk_size=4096):
            if len(payload) + len(chunk) > 65536:
                raise ContractError("Application response exceeds limit")
            payload.extend(chunk)
    return bytes(payload)


def response_json(client: httpx.Client, url: str) -> dict[str, object]:
    value = json.loads(response_body(client, url))
    if not isinstance(value, dict):
        raise ContractError("Application response must be an object")
    return value


def application_ready(client: httpx.Client, url: str, readiness: Readiness) -> bool:
    match readiness:
        case "v1-status":
            response_body(client, url)
            return True
        case "legacy-json":
            return response_json(client, url).get("status") == "ready"


def started_by(actual: Container, image_id: str, operation_id: UUID) -> bool:
    return actual.image == image_id and actual.config.labels.get(
        "home-platform.operation"
    ) == str(operation_id)


def as_installed(app: HostApp, snapshot: InstalledSnapshot) -> HostApp:
    """The policy entry with the registration accepted for ``snapshot``.

    The host policy may already carry a change awaiting approval, and an
    accepted generation must restart with the bindings it was accepted with.
    """
    names = snapshot.registration.bindings.integration_bindings
    return app.model_copy(
        update={
            "registration": snapshot.registration,
            "mounts": tuple(mount for mount in app.mounts if mount.name in names),
        }
    )


class Installer:
    def __init__(
        self,
        policy: HostPolicy,
        policy_path: Path = Path("/etc/home-platform/installer.json"),
    ) -> None:
        self.policy = policy
        self.digest = fingerprint(policy)
        self.journal = Path(policy.journal)
        self.policy_path = policy_path

    def command(self, *argv: str, timeout: int = 120, limit: int = 1024 * 1024) -> str:
        return capture(argv, timeout=timeout, limit=limit).decode()

    def child(self, uid: int, gid: int, *args: str) -> bytes:
        return capture(
            (self.policy.python, "-I", "-m", "ops.installer_child", *args),
            timeout=120,
            limit=48 * 1024**2,
            uid=uid,
            gid=gid,
        )

    def current_policy(self) -> None:
        if fingerprint(load_policy(self.policy_path)) != self.digest:
            raise ContractError("Host policy changed")

    def publication(
        self, app: HostApp, archive: bytes, artifact_id: int, archive_sha256: str
    ) -> VerifiedPublication:
        release = verify_package(archive, app.source, artifact_id, archive_sha256)
        if release.manifest.app_id != app.registration.app_id:
            raise ContractError("Release names another application")
        return release

    def verified(
        self, app: HostApp, archive: bytes, artifact_id: int, archive_sha256: str
    ) -> HostRelease:
        return narrow(self.publication(app, archive, artifact_id, archive_sha256))

    def release(
        self, app: HostApp, archive: bytes, artifact_id: int, archive_sha256: str
    ) -> HostRelease:
        release = self.publication(app, archive, artifact_id, archive_sha256)
        manifest = release.manifest
        if manifest.maintenance != app.maintenance or manifest.state != app.state:
            raise PolicyRefused(
                "Release maintenance or state differs from the host policy entry; "
                "update the host policy before approving this release"
            )
        return narrow(release)

    def current(self, app: HostApp) -> AcceptedSnapshot | None:
        """The accepted generation this journal holds, running or retired."""
        try:
            recorded = read_owned(self.current_path(app), 0)
        except FileNotFoundError:
            return None
        return decode_accepted(recorded)

    def running(self, app: HostApp) -> InstalledSnapshot | None:
        return running_of(self.current(app))

    def current_path(self, app: HostApp) -> Path:
        return self.journal / (app.registration.app_id + ".current.json")

    def authority(self, operation: Operation) -> tuple[Authority, HostApp, HostRelease]:
        self.current_policy()
        app_id = operation.approval.selection.app_id
        authority = Authority.model_validate_json(
            self.child(
                self.policy.core_uid,
                self.policy.core_gid,
                "authority",
                self.policy.database,
                str(operation.operation_id),
                app_fingerprint(self.policy, app_id),
            )
        )
        selected = authority.operation.approval.selection
        if authority.operation != operation:
            raise ContractError("Core operation changed")
        app = self.policy.app(app_id)
        if authority.registration != app.registration:
            raise PolicyRefused(
                "Approved registration differs from the host policy entry; "
                "update the host policy before approving this change"
            )
        if (
            authority.credential_path != app.credential_file
            or selected.environment != self.policy.environment
            or authority.app_api_url != self.policy.app_api_url
            or authority.timezone != self.policy.timezone
        ):
            raise ContractError("Core authority exceeds host policy")
        current = self.current(app)
        if authority.previous != current or selected.generation != (
            current.generation if current else 0
        ):
            raise ContractError("Core installation history differs from root journal")
        credential = read_owned(Path(app.credential_file), self.policy.core_uid).strip()
        if hashlib.sha256(credential).hexdigest() != authority.credential_verifier:
            raise ContractError("Credential file changed")
        release = self.release(
            app,
            base64.b64decode(authority.archive_base64, validate=True),
            authority.artifact_id,
            selected.archive_sha256,
        )
        if release.manifest_sha256 != selected.manifest_sha256:
            raise ContractError("Pinned release changed")
        return authority, app, release

    def image(self, image: str, *, pull: bool = True) -> str:
        if pull:
            self.command(
                "docker", *registry_credentials(self.policy), "pull", image, timeout=300
            )
        data = json.loads(self.command("docker", "image", "inspect", image))[0]
        if image not in data["RepoDigests"]:
            raise ContractError("Actual image digest differs")
        return str(data["Id"])

    def local_image(self, image: str) -> str:
        try:
            return self.image(image, pull=False)
        except ContractError:
            return self.image(image)

    def inspect(self, name: str) -> Container | None:
        listing = self.command(
            "docker",
            "container",
            "ls",
            "-a",
            "--filter",
            "name=^/" + name + "$",
            "--format",
            "{{.Names}}",
        )
        if not listing.strip():
            return None
        return Container.model_validate(
            json.loads(self.command("docker", "inspect", name))[0]
        )

    def owned(self, name: str, app_id: str) -> Container | None:
        data = self.inspect(name)
        if data is not None:
            labels = data.config.labels
            if (
                labels.get("home-platform.app") != app_id
                or labels.get("home-platform.installer") != "v1"
            ):
                raise ContractError("Container ownership is unknown")
        return data

    def remove(self, name: str, app_id: str) -> None:
        if self.owned(name, app_id) is not None:
            self.command("docker", "rm", "-f", name)

    def limits(self) -> None:
        info = json.loads(self.command("docker", "info", "--format", "{{json .}}"))
        if (
            not info.get("MemoryLimit")
            or not info.get("PidsLimit")
            or not info.get("CpuCfsQuota")
            or not info.get("SwapLimit")
        ):
            raise ContractError("Required resource controllers are unavailable")

    def backup(
        self,
        app: HostApp,
        sqlite_file: str,
        destination: Path,
        *,
        source: Path | None = None,
    ) -> BackupFacts:
        scratch = Path(tempfile.mkdtemp(prefix="backup-", dir=self.policy.scratch))
        os.chown(scratch, app.uid, app.gid)
        os.chmod(scratch, 0o700)
        try:
            result = json.loads(
                self.child(
                    app.uid,
                    app.gid,
                    "backup",
                    str(
                        source
                        or Path(str(app.registration.bindings.state_directory))
                        / sqlite_file
                    ),
                    str(scratch / "copy.sqlite"),
                )
            )
            payload = read_owned(scratch / "copy.sqlite", app.uid, 1024**3)
            digest = hashlib.sha256(payload).hexdigest()
            if digest != result["sha256"]:
                raise ContractError("Online backup changed")
            publish(destination, payload, mode=0o600)
            restored = scratch / "restored.sqlite"
            write_private(
                restored, read_owned(destination, 0, 1024**3), app.uid, app.gid
            )
            verified = json.loads(
                self.child(
                    app.uid,
                    app.gid,
                    "backup",
                    str(restored),
                    str(scratch / "verified.sqlite"),
                )
            )
            if (
                verified["tables"] != result["tables"]
                or verified["content_sha256"] != result["content_sha256"]
                or hashlib.sha256(read_owned(restored, app.uid, 1024**3)).hexdigest()
                != digest
            ):
                raise ContractError("Disposable restore failed")
            return BackupFacts(
                digest,
                str(result["content_sha256"]),
                tuple(str(name) for name in result["tables"]),
                len(payload),
            )
        finally:
            shutil.rmtree(scratch)

    def spec(
        self, authority: Authority, app: HostApp, release: HostRelease
    ) -> RuntimeSpec:
        registration = app.registration
        spec = runtime_spec(
            ManagedApp(registration, app.installation),
            AppCredential(
                authority.operation.approval.selection.credential_id,
                registration.app_id,
                self.policy.environment,
                self.credential_snapshot(authority, app),
            ),
            InstanceFacts(
                self.policy.environment, self.policy.app_api_url, self.policy.timezone
            ),
            release.port,
            app.mounts,
        )
        secrets = {}
        for secret in registration.bindings.secret_files:
            payload = read_owned(Path(secret.file_path), 0)
            digest = hashlib.sha256(payload).hexdigest()
            prefix = registration.app_id + "--" + secret.name + "--"
            destination = self.journal / "credentials" / (prefix + digest)
            publish(destination, payload)
            os.chown(destination, 0, app.gid)
            os.chmod(destination, 0o640)
            for superseded in destination.parent.glob(prefix + "*"):
                if superseded != destination and re.fullmatch(
                    re.escape(prefix) + "[0-9a-f]{64}", superseded.name
                ):
                    superseded.unlink()
            secrets[secret.file_path] = str(destination)
        return replace(
            spec,
            mounts=tuple(
                replace(mount, source=secrets.get(mount.source, mount.source))
                for mount in spec.mounts
            ),
        )

    def credential_snapshot(self, authority: Authority, app: HostApp) -> Path:
        selected = authority.operation.approval.selection
        directory = self.journal / "credentials"
        directory.mkdir(mode=0o700, exist_ok=True)
        payload = read_owned(Path(app.credential_file), self.policy.core_uid).strip()
        if hashlib.sha256(payload).hexdigest() != authority.credential_verifier:
            raise ContractError("Credential changed before container creation")
        destination = directory / (selected.app_id + "-" + selected.credential_id)
        publish(destination, payload)
        os.chown(destination, 0, app.gid)
        os.chmod(destination, 0o640)
        return destination

    def start(
        self,
        authority: Authority,
        app: HostApp,
        release: HostRelease,
        image: str,
        *,
        rehearsal: Path | None = None,
        command: tuple[str, ...] = (),
        installed_operation_id: UUID | None = None,
    ) -> str:
        if rehearsal is None:
            integration_sources(app)
        spec = self.spec(authority, app, release)
        argv = list(docker_run_argv(spec, image, (app.uid, app.gid)))
        name = "hp-app-" + app.registration.app_id
        if rehearsal is not None:
            bindings = app.registration.bindings
            withheld = {
                "PLATFORM_API_URL",
                "APP_CREDENTIAL_FILE",
                *map(integration_variable, bindings.integration_bindings),
            }
            name = "hp-rehearsal-" + app.registration.app_id
            spec = replace(
                spec,
                mounts=(
                    Mount(str(rehearsal), "/data", bool(command)),
                    Mount(
                        str(rehearsal.parent / "credential"),
                        "/run/secrets/app-credential",
                        True,
                    ),
                    *(
                        Mount(
                            str(rehearsal.parent / "secrets" / item.name),
                            "/run/secrets/" + item.name,
                            True,
                        )
                        for item in bindings.secret_files
                    ),
                ),
                environment=(
                    *(
                        (key, value)
                        for key, value in spec.environment
                        if key not in withheld
                    ),
                    ("PLATFORM_API_URL", "http://127.0.0.1:1/api/apps/v1"),
                    ("APP_CREDENTIAL_FILE", "/run/secrets/app-credential"),
                ),
            )
            argv = list(docker_run_argv(spec, image, (app.uid, app.gid)))
            for option in ("--publish", "--add-host"):
                index = argv.index(option)
                del argv[index : index + 2]
            argv[argv.index("--restart") + 1] = "no"
            argv[-1:-1] = ["--network", "none"]
        argv[argv.index("--name") + 1] = name
        argv[argv.index("--restart") + 1] = "no"
        # Name the driver and compression so a host daemon.json default cannot
        # turn these options into a configuration Docker refuses at start.
        argv[-1:-1] = [
            "--log-driver",
            "local",
            "--log-opt",
            "max-size=1m",
            "--log-opt",
            "max-file=1",
            "--log-opt",
            "compress=false",
        ]
        argv[-1:-1] = [
            "--label",
            "home-platform.installer=v1",
            "--label",
            "home-platform.operation="
            + str(installed_operation_id or authority.operation.operation_id),
        ]
        if command:
            argv[-1:-1] = ["--entrypoint", command[0]]
            argv.extend(command[1:])
        self.command(*argv)
        return name

    def probe(
        self,
        authority: Authority,
        app: HostApp,
        release: HostRelease,
        image_id: str,
    ) -> None:
        name = "hp-app-" + app.registration.app_id
        data = self.owned(name, app.registration.app_id)
        if data is None or data.image != image_id:
            raise ContractError("Running image differs")
        host = data.limits
        resources = app.registration.bindings.resources
        if (
            host.memory != resources.memory_bytes
            or host.memory_swap != resources.memory_bytes
            or host.pids != resources.pids_limit
            or host.cpu != int(resources.cpu_limit * 1_000_000_000)
        ):
            raise ContractError("Running limits differ")
        origin = f"http://127.0.0.1:{app.installation.host_port}"
        with httpx.Client(trust_env=False, timeout=3) as client:
            for _ in range(30):
                try:
                    ready = application_ready(
                        client,
                        origin + release.ready_path,
                        app.installation.probe.readiness,
                    )
                    version = response_json(client, origin + release.version_path)
                    if ready and version.get("service") == app.registration.app_id:
                        break
                except (httpx.HTTPError, ValueError):
                    pass
                time.sleep(1)
            else:
                raise ContractError("Application readiness failed")
            for headers in (
                {},
                {"Tailscale-User-Login": "forged@example.invalid"},
                {"X-Platform-User-Context": "forged"},
            ):
                with client.stream("GET", origin + "/", headers=headers) as response:
                    if response.status_code not in {401, 403}:
                        raise ContractError("Application authentication failed")

    def rehearsal_ready(self, name: str, app: HostApp, release: HostRelease) -> None:
        url = f"http://127.0.0.1:{release.port}{release.ready_path}"
        probe = (
            "import json,sys,urllib.request; "
            "o=urllib.request.build_opener(urllib.request.ProxyHandler({})); "
            "r=o.open(sys.argv[1],timeout=2); "
            "b=r.read(65537); "
            "sys.exit(0 if r.status==200 and len(b)<=65536 and "
            "(sys.argv[2]=='v1-status' or json.loads(b).get('status')=='ready') "
            "else 1)"
        )
        for _ in range(30):
            data = self.owned(name, app.registration.app_id)
            if data is None or not data.state.running or data.state.pid <= 1:
                raise ContractError("Rehearsal startup failed")
            try:
                self.command(
                    "nsenter",
                    "--target",
                    str(data.state.pid),
                    "--net",
                    "--",
                    self.policy.python,
                    "-I",
                    "-c",
                    probe,
                    url,
                    app.installation.probe.readiness,
                    timeout=5,
                )
            except ContractError:
                time.sleep(1)
                continue
            # The PID could have been reused by another process between inspect
            # and nsenter; the probe counts only if this container held it.
            after = self.owned(name, app.registration.app_id)
            if (
                after is None
                or not after.state.running
                or after.state.pid != data.state.pid
            ):
                raise ContractError("Rehearsal container changed during readiness")
            return
        raise ContractError("Rehearsal readiness failed")

    def semantic_state(
        self,
        authority: Authority,
        app: HostApp,
        release: HostRelease,
        image: str,
        directory: Path,
    ) -> str:
        name = self.start(
            authority,
            app,
            release,
            image,
            rehearsal=directory,
            command=app.compatibility_argv,
        )
        try:
            output = self.command(
                "docker", "logs", "--follow", name, timeout=60, limit=65536
            ).strip()
            if self.command("docker", "wait", name, timeout=60).strip() != "0":
                raise ContractError("Image cannot read application state")
            if not output or len(output.encode()) > 65536:
                raise ContractError("Semantic state output is empty or exceeds limit")
            return output
        finally:
            self.remove(name, app.registration.app_id)

    def existing_state(self, app: HostApp) -> bool:
        if app.state is None:
            return False
        directory = Path(str(app.registration.bindings.state_directory))
        root_path(directory, leaf_uid=app.uid)
        path = directory / app.state.sqlite_file
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(info.st_mode):
            raise ContractError("Application SQLite state must be a regular file")
        if (
            info.st_uid != app.uid
            or info.st_mode & 0o022
            or not info.st_mode & stat.S_IRUSR
        ):
            raise ContractError("Application SQLite state ownership is unsafe")
        return True

    def rehearse(
        self,
        authority: Authority,
        app: HostApp,
        release: HostRelease,
        image_id: str,
        backup: Path | None,
    ) -> Rehearsal:
        root = Path(tempfile.mkdtemp(prefix="rehearsal-", dir=self.policy.scratch))
        # The restore check runs as the app UID and must reach state/ below here;
        # group traversal exposes nothing else, since copies stay 0600 root-owned.
        os.chown(root, 0, app.gid)
        os.chmod(root, 0o710)
        publish(root / "credential", b"synthetic-rehearsal-credential\n")
        (root / "secrets").mkdir(mode=0o755)
        for secret in app.registration.bindings.secret_files:
            publish(root / "secrets" / secret.name, b"synthetic-rehearsal-secret\n")
        directory = root / "state"
        directory.mkdir(mode=0o700)
        os.chown(directory, app.uid, app.gid)
        name = "hp-rehearsal-" + app.registration.app_id
        backup_digest = None
        try:
            self.remove(name, app.registration.app_id)
            state = app.state
            if backup is not None and state is not None:
                payload = read_owned(backup, 0, 1024**3)
                backup_digest = hashlib.sha256(payload).hexdigest()
                write_private(directory / state.sqlite_file, payload, app.uid, app.gid)
            previous = self.current(app)
            # A restore rehearses against the image that last wrote the data.
            last = (
                previous.last_installed
                if isinstance(previous, RetiredSnapshot)
                else previous
            )
            old = self.retained_release(app, last) if last else None
            previous_id = self.image(old.image) if old else None
            before = None
            if old is not None:
                before = self.semantic_state(authority, app, old, old.image, directory)
            elif backup is not None:
                before = self.semantic_state(
                    authority, app, release, release.image, directory
                )
            self.start(authority, app, release, release.image, rehearsal=directory)
            self.rehearsal_ready(name, app, release)
            self.remove(name, app.registration.app_id)
            after = self.semantic_state(
                authority, app, release, release.image, directory
            )
            if before is not None and before != after:
                raise RecordsChanged()
            if old is not None and (
                self.semantic_state(authority, app, old, old.image, directory) != after
            ):
                raise RecordsChanged()
            if state is not None:
                check = directory.parent / (directory.name + ".sqlite")
                try:
                    self.backup(
                        app,
                        state.sqlite_file,
                        check,
                        source=directory / state.sqlite_file,
                    )
                finally:
                    check.unlink(missing_ok=True)
            return Rehearsal(
                selection=authority.operation.approval.selection,
                image_id=image_id,
                previous_image_id=previous_id,
                backup_sha256=backup_digest,
                checks=rehearsal_checks(previous_id),
            )
        finally:
            self.remove(name, app.registration.app_id)
            shutil.rmtree(root)

    def execute(self, operation_id: UUID) -> None:
        receipt_path = self.journal / (str(operation_id) + ".receipt.json")
        if receipt_path.exists():
            self.commit(parse_document(Receipt, read_owned(receipt_path, 0)))
            return
        operation = parse_document(
            Operation,
            self.child(
                self.policy.core_uid,
                self.policy.core_gid,
                "operation",
                self.policy.database,
                str(operation_id),
            ),
        )
        marker = self.journal / (str(operation_id) + ".switch.json")
        try:
            authority, app, release = self.authority(operation)
        except (ContractError, OSError, subprocess.SubprocessError, ValueError) as err:
            refused: Literal["policy_refused", "authority_changed"] = (
                "policy_refused"
                if isinstance(err, PolicyRefused)
                else "authority_changed"
            )
            self.commit(
                Receipt(
                    operation_id=operation_id,
                    selection=operation.approval.selection,
                    state="blocked" if marker.exists() else "failed",
                    error="recovery_required" if marker.exists() else refused,
                )
            )
            return
        selected = authority.operation.approval.selection
        current = self.current(app)
        running = running_of(current)
        target = (current.generation if current else 0) + 1
        try:
            self.limits()
            if operation.kind == "recover":
                if not self.admitted(operation_id):
                    return
                self.commit(self.recover(operation, authority, app, release))
                return
            if operation.kind == "retire":
                if not self.admitted(operation_id):
                    return
                self.commit(self.retire(operation, authority, app, running, marker))
                return
            image_id = self.image(release.image)
            self.retain_assets(release)
            state = app.state
            scratch = Path(tempfile.mkdtemp(prefix="input-", dir=self.policy.scratch))
            try:
                backup = None
                if state is not None and (
                    current is not None or self.existing_state(app)
                ):
                    backup = scratch / "input.sqlite"
                    if isinstance(current, RetiredSnapshot):
                        self.restore_input(app, state.sqlite_file, current, backup)
                    else:
                        self.backup(app, state.sqlite_file, backup)
                rehearsal = self.rehearse(authority, app, release, image_id, backup)
            finally:
                shutil.rmtree(scratch)
            if operation.kind == "rehearse":
                receipt = Receipt(
                    operation_id=operation_id,
                    selection=selected,
                    state="succeeded",
                    rehearsal=rehearsal,
                )
            else:
                # Refuse here, while the previous container still serves,
                # rather than after the switch has removed it.
                integration_sources(app)
                if not self.admitted(operation_id):
                    return
                self.authority(operation)
                name = "hp-app-" + selected.app_id
                actual = self.recognised(name, selected.app_id)
                switched = actual is not None and started_by(
                    actual, image_id, operation_id
                )
                if actual is not None and not (
                    switched
                    or (
                        running is not None
                        and started_by(actual, running.image_id, running.operation_id)
                    )
                ):
                    raise Contradictory()
                if (
                    state is not None
                    and not switched
                    and (
                        current is not None
                        or backup is not None
                        or self.existing_state(app)
                    )
                ):
                    self.pre_switch_backup(app, state.sqlite_file, operation_id, target)
                publish(marker, b"{}")
                if not switched:
                    self.stop_timers(app)
                    self.stop_supervisor(app)
                    self.remove(name, selected.app_id)
                    self.start(authority, app, release, release.image)
                elif actual is not None and not actual.state.running:
                    self.command("docker", "start", name)
                self.probe(authority, app, release, image_id)
                self.authority(operation)
                snapshot = InstalledSnapshot(
                    operation_id=operation_id,
                    generation=target,
                    selection=selected,
                    registration=app.registration,
                    image=release.image,
                    image_id=image_id,
                )
                self.lifecycle(app, True)
                self.authority(operation)
                self.prune_backups(app, snapshot.generation)
                receipt = Receipt(
                    operation_id=operation_id,
                    selection=selected,
                    state="succeeded",
                    installed=snapshot,
                    authorization_committed=True,
                )
        except (ContractError, OSError, subprocess.SubprocessError, ValueError) as err:
            state_: Literal["failed", "blocked", "recovered"] = "failed"
            if operation.kind == "recover":
                pass
            elif isinstance(err, Contradictory):
                state_ = "blocked"
            elif marker.exists():
                fell_back = self.fell_back(operation, app, current)
                state_ = "recovered" if fell_back else "blocked"
            receipt = Receipt(
                operation_id=operation_id,
                selection=selected,
                state=state_,
                error="recovery_required"
                if state_ == "blocked" or operation.kind == "recover"
                else err.error
                if isinstance(err, SwitchRefused)
                else "installation_failed",
            )
        self.commit(receipt)

    def recognised(self, name: str, app_id: str) -> Container | None:
        """The app's container, or None; one of unknown ownership blocks."""
        try:
            return self.owned(name, app_id)
        except ContractError:
            raise Contradictory() from None

    def fell_back(
        self, operation: Operation, app: HostApp, current: AcceptedSnapshot | None
    ) -> bool:
        """Return the host to ``current`` after a failed switch, if it can.

        A running generation restarts. A retired one is torn down again, so a
        failed restore never starts the release it replaced. A first install
        has nothing to fall back to and stays blocked.
        """
        if current is None:
            return False
        try:
            if isinstance(current, RetiredSnapshot):
                self.tear_down(app)
            else:
                fresh, _, _ = self.authority(operation)
                self.restore_previous(fresh, app)
        except (ContractError, OSError, subprocess.SubprocessError, ValueError):
            return False
        return True

    def tear_down(self, app: HostApp) -> None:
        self.stop_timers(app)
        self.remove("hp-app-" + app.registration.app_id, app.registration.app_id)
        self.lifecycle(app, False)

    def restore_input(
        self, app: HostApp, sqlite_file: str, retired: RetiredSnapshot, backup: Path
    ) -> None:
        """Copy the kept state for rehearsal, refusing data the archive did not record.

        The archive itself is never copied back over the live database.
        """
        if not self.existing_state(app):
            raise StateMissing()
        facts = self.backup(app, sqlite_file, backup)
        if (
            retired.archive is not None
            and facts.content_sha256 != retired.archive.content_sha256
        ):
            raise StateChanged()

    def retire(
        self,
        operation: Operation,
        authority: Authority,
        app: HostApp,
        running: InstalledSnapshot | None,
        marker: Path,
    ) -> Receipt:
        """Stop the app, archive its final state, remove its units.

        The archive is taken after the container is gone so it is the final
        state. The units go before the receipt, since a leftover unit is the
        one way a retired app could come back on its own.
        """
        selected = authority.operation.approval.selection
        if running is None:
            raise ContractError("Nothing is installed to retire")
        app_id = app.registration.app_id
        name = "hp-app-" + app_id
        actual = self.recognised(name, app_id)
        if actual is not None and not started_by(
            actual, running.image_id, running.operation_id
        ):
            raise Contradictory()
        publish(marker, b"{}")
        self.stop_timers(app)
        self.stop_supervisor(app)
        self.remove(name, app_id)
        archive = None
        if app.state is not None:
            try:
                archive = self.retirement_archive(
                    app, app.state.sqlite_file, selected.generation + 1, operation
                )
            except (
                ContractError,
                OSError,
                subprocess.SubprocessError,
                ValueError,
            ) as err:
                raise RetirementBackupFailed() from err
        self.lifecycle(app, False)
        self.authority(operation)
        return Receipt(
            operation_id=operation.operation_id,
            selection=selected,
            state="succeeded",
            retired=RetiredSnapshot(
                operation_id=operation.operation_id,
                generation=selected.generation + 1,
                selection=selected,
                registration=app.registration,
                last_installed=running,
                archive=archive,
            ),
            authorization_committed=True,
        )

    def retirement_archive(
        self, app: HostApp, sqlite_file: str, generation: int, operation: Operation
    ) -> ArchiveRecord:
        directory = Path(str(app.registration.bindings.backup_directory))
        # A retry at the same generation supersedes an earlier copy, as a
        # pre-switch backup does; the one kept is of the final quiescent state.
        for path in directory.glob(f"retired-g{generation}-*.sqlite"):
            if re.fullmatch(r"retired-g\d+-[0-9a-f-]{36}\.sqlite", path.name):
                read_owned(path, 0, 1024**3)
                path.unlink()
        file_name = f"retired-g{generation}-{operation.operation_id}.sqlite"
        facts = self.backup(app, sqlite_file, directory / file_name)
        return ArchiveRecord(
            file_name=file_name,
            sha256=facts.sha256,
            content_sha256=facts.content_sha256,
            tables=facts.tables,
            size_bytes=facts.size_bytes,
        )

    def advertise(self) -> None:
        """Publish the kinds this runtime answers, so core queues no other."""
        replace_file(
            Path(self.policy.spool) / "receipts" / "installer.json",
            InstallerCapabilities(kinds=get_args(Kind)).model_dump_json().encode(),
        )

    def commit(self, receipt: Receipt) -> None:
        # The journal receipt is the commit point. The current-installation
        # record derives from it, and the spool copy that core consumes goes
        # last, so a run interrupted anywhere here replays through this method
        # on the next request without a second host effect.
        name = str(receipt.operation_id)
        payload = receipt.model_dump_json().encode()
        receipt_path = self.journal / (name + ".receipt.json")
        if not receipt_path.exists():
            publish(receipt_path, payload)
        accepted = receipt.installed or receipt.retired
        if receipt.state == "succeeded" and accepted is not None:
            app = self.policy.app(accepted.selection.app_id)
            current = self.current(app)
            if accepted.generation == (current.generation if current else 0) + 1:
                replace_file(
                    self.current_path(app), accepted.model_dump_json().encode()
                )
        publish(Path(self.policy.spool) / "receipts" / (name + ".json"), payload)
        # A replay commits from the journal receipt, so the journal marker has no
        # reader left. The spool marker stays until core consumed the receipt: a
        # core that lost its last commit must still find the switch intent.
        (self.journal / (name + ".switch.json")).unlink(missing_ok=True)

    def sweep_markers(self) -> None:
        receipts = Path(self.policy.spool) / "receipts"
        for marker in receipts.glob("*.switch.json"):
            name = marker.name.removesuffix(".switch.json")
            consumed = (receipts / (name + ".json")).exists() and not (
                Path(self.policy.spool) / "requests" / (name + ".json")
            ).exists()
            if consumed:
                self.start_supervisor(
                    parse_document(Receipt, read_owned(receipts / (name + ".json"), 0))
                )
                marker.unlink(missing_ok=True)

    def start_supervisor(self, receipt: Receipt) -> None:
        unit = "hp-managed-" + receipt.selection.app_id + "--app.service"
        if (
            receipt.state in ("succeeded", "recovered")
            and (Path(self.policy.units) / unit).exists()
        ):
            self.command("systemctl", "start", "--no-block", unit)

    def admitted(self, operation_id: UUID) -> bool:
        publish(
            Path(self.policy.spool) / "receipts" / (str(operation_id) + ".switch.json"),
            OperationHint(operation_id=operation_id).model_dump_json().encode(),
        )
        for _ in range(60):
            if (
                self.child(
                    self.policy.core_uid,
                    self.policy.core_gid,
                    "state",
                    self.policy.database,
                    str(operation_id),
                ).strip()
                == b"switching"
            ):
                return True
            time.sleep(1)
        return False

    def retained_release(
        self, app: HostApp, snapshot: InstalledSnapshot
    ) -> HostRelease:
        release = self.verified(
            app,
            read_owned(
                self.journal / (snapshot.selection.archive_sha256 + ".zip"),
                0,
                32 * 1024**2,
            ),
            1,
            snapshot.selection.archive_sha256,
        )
        if release.image != snapshot.image:
            raise ContractError("Installed image differs from retained release")
        return release

    def restore_previous(self, authority: Authority, app: HostApp) -> None:
        previous = self.running(app)
        if previous is None:
            raise ContractError("Application has no running installation")
        app = as_installed(app, previous)
        old = self.retained_release(app, previous)
        if self.local_image(old.image) != previous.image_id:
            raise ContractError("Previous image changed")
        name = "hp-app-" + app.registration.app_id
        self.stop_timers(app)
        self.stop_supervisor(app)
        self.remove(name, app.registration.app_id)
        self.start(
            authority,
            app,
            old,
            old.image,
            installed_operation_id=previous.operation_id,
        )
        self.probe(authority, app, old, previous.image_id)
        self.lifecycle(app, True)

    def recover(
        self,
        operation: Operation,
        authority: Authority,
        app: HostApp,
        release: HostRelease,
    ) -> Receipt:
        selected = authority.operation.approval.selection
        assert operation.recovers is not None
        blocked = parse_document(
            Receipt,
            read_owned(self.journal / (str(operation.recovers) + ".receipt.json"), 0),
        )
        if (
            blocked.state != "blocked"
            or blocked.operation_id != operation.recovers
            or blocked.selection.app_id != selected.app_id
        ):
            raise ContractError("Recovery target changed")
        app_id = selected.app_id
        name = "hp-app-" + app_id
        actual = self.owned(name, app_id)
        label = actual.config.labels.get("home-platform.operation") if actual else None
        running = actual is not None and actual.state.running
        previous = self.running(app)
        installed = None
        accepted = self.current(app)
        if (
            running
            and actual is not None
            and label == str(operation.recovers)
            and blocked.selection == selected
            and actual.image == self.local_image(release.image)
        ):
            self.probe(authority, app, release, actual.image)
            self.lifecycle(app, True)
            installed = InstalledSnapshot(
                operation_id=operation.recovers,
                generation=(accepted.generation if accepted else 0) + 1,
                selection=selected,
                registration=app.registration,
                image=release.image,
                image_id=actual.image,
            )
        elif previous is not None:
            app = as_installed(app, previous)
            old = self.retained_release(app, previous)
            adopted = (
                running
                and actual is not None
                and actual.image == previous.image_id
                and label == str(previous.operation_id)
            )
            if adopted:
                try:
                    self.probe(authority, app, old, previous.image_id)
                except (ContractError, OSError, subprocess.SubprocessError, ValueError):
                    adopted = False
            if adopted:
                self.lifecycle(app, True)
            else:
                self.restore_previous(authority, app)
        else:
            self.tear_down(app)
        self.authority(operation)
        if installed is not None:
            self.prune_backups(app, installed.generation)
        return Receipt(
            operation_id=operation.operation_id,
            selection=selected,
            state="succeeded",
            installed=installed,
            authorization_committed=True,
        )

    def pre_switch_backup(
        self, app: HostApp, sqlite_file: str, operation_id: UUID, generation: int
    ) -> None:
        directory = Path(str(app.registration.bindings.backup_directory))
        # A retried attempt at the same generation supersedes the earlier copy; the
        # live database it was taken from has only moved forward since then.
        for path, found in self.pre_switch_backups(app):
            if found == generation:
                read_owned(path, 0, 1024**3)
                path.unlink()
        self.backup(
            app,
            sqlite_file,
            directory / f"pre-switch-g{generation}-{operation_id}.sqlite",
        )

    def pre_switch_backups(self, app: HostApp) -> list[tuple[Path, int]]:
        directory = Path(str(app.registration.bindings.backup_directory))
        found = []
        for path in directory.glob("pre-switch-g*.sqlite"):
            match = re.fullmatch(r"pre-switch-g(\d+)-[0-9a-f-]{36}\.sqlite", path.name)
            if match:
                found.append((path, int(match.group(1))))
        return found

    def prune_backups(self, app: HostApp, generation: int) -> None:
        for path, found in self.pre_switch_backups(app):
            if found < generation - 1:
                read_owned(path, 0, 1024**3)
                path.unlink()

    def retain_assets(self, release: HostRelease) -> None:
        publish(self.journal / (release.archive_sha256 + ".zip"), release.archive)

    def sweep_scratch(self) -> None:
        for path in Path(self.policy.scratch).iterdir():
            if SCRATCH_ENTRY.fullmatch(path.name) and stat.S_ISDIR(
                path.lstat().st_mode
            ):
                shutil.rmtree(path)

    def stop_timers(self, app: HostApp) -> None:
        for filename in self.owned_units(app):
            if filename.endswith(".timer"):
                self.command("systemctl", "disable", "--now", filename)
                self.command(
                    "systemctl", "stop", filename.removesuffix(".timer") + ".service"
                )

    def stop_supervisor(self, app: HostApp) -> None:
        unit = "hp-managed-" + app.registration.app_id + "--app.service"
        if unit in self.owned_units(app):
            self.command("systemctl", "stop", unit)

    def owned_units(self, app: HostApp) -> set[str]:
        prefix = "hp-managed-" + app.registration.app_id + "--"
        result = set()
        for path in Path(self.policy.units).glob(prefix + "*"):
            if not read_owned(path, 0).startswith(
                ("# Managed app " + app.registration.app_id + "\n").encode()
            ):
                raise ContractError("Lifecycle ownership is unknown")
            result.add(path.name)
        return result

    def write_unit(self, filename: str, content: str) -> None:
        units = Path(self.policy.units)
        payload = content.encode()
        try:
            if read_owned(units / filename, 0) == payload:
                return
        except FileNotFoundError:
            pass
        (units / PENDING_RELOAD).touch()
        sync_directory(units)
        replace_file(units / filename, payload)

    def reload_units(self) -> None:
        self.command("systemctl", "daemon-reload")
        with suppress(FileNotFoundError):
            (Path(self.policy.units) / PENDING_RELOAD).unlink()

    def helper_argv(self) -> str:
        return (
            f"{systemd_argument(self.policy.python)} -I -m ops.managed_installer "
            f"--policy {systemd_argument(str(self.policy_path))}"
        )

    def lifecycle(self, app: HostApp, installed: bool) -> None:
        prefix = "hp-managed-" + app.registration.app_id + "--"
        units = Path(self.policy.units)
        name = "hp-app-" + app.registration.app_id
        entries = {}
        if installed:
            mounts = (
                f"RequiresMountsFor={unit_paths(m.source for m in app.mounts)}\n"
                if app.mounts
                else ""
            )
            entries[prefix + "app.service"] = (
                "[Unit]\nAfter=docker.service\nRequires=docker.service\n"
                + mounts
                + "[Service]\nType=simple\nRestart=on-failure\nRestartSec=5\n"
                "KillMode=mixed\nTimeoutStopSec=30\n"
                f"SuccessExitStatus={DEFERRED}\n"
                f"ExecStart={self.helper_argv()} --run {app.registration.app_id}\n"
                "[Install]\nWantedBy=multi-user.target\n"
            )
            for item in app.maintenance:
                service = prefix + "maintenance-" + item.name
                args = " ".join(systemd_argument(value) for value in item.argv)
                entries[service + ".service"] = (
                    f"[Unit]\nRequires={prefix}app.service\nAfter={prefix}app.service\n"
                    f"[Service]\nType=oneshot\nTimeoutStartSec={item.timeout_seconds}\n"
                    f"ExecStart=/usr/bin/docker exec {name} {args}\n"
                )
                schedule = next(
                    (s for s in app.maintenance_schedules if s.name == item.name), None
                )
                calendar = maintenance_calendar(
                    app.registration.app_id,
                    item,
                    offset_minutes=schedule.offset_minutes if schedule else None,
                )
                jitter = (
                    f"RandomizedDelaySec={schedule.jitter_seconds}\n"
                    if schedule
                    else ""
                )
                entries[service + ".timer"] = (
                    f"[Timer]\nOnCalendar={calendar}\nPersistent=true\n"
                    + jitter
                    + "[Install]\nWantedBy=timers.target\n"
                )
            if app.state is not None:
                service = prefix + "backup"
                backup_schedule = app.backup_schedule
                dependencies = ""
                if backup_schedule and backup_schedule.requires_mounts_for:
                    paths = unit_paths(backup_schedule.requires_mounts_for)
                    dependencies = f"[Unit]\nRequiresMountsFor={paths}\n"
                entries[service + ".service"] = (
                    dependencies + "[Service]\nType=oneshot\n"
                    f"ExecStart={self.helper_argv()} --backup "
                    f"{app.registration.app_id}\n"
                )
                calendar = (
                    f"*-*-* {backup_schedule.hour:02d}:{backup_schedule.minute:02d}:00"
                    if backup_schedule
                    else "daily"
                )
                jitter = (
                    f"RandomizedDelaySec={backup_schedule.jitter_seconds}\n"
                    if backup_schedule
                    else ""
                )
                entries[service + ".timer"] = (
                    f"[Timer]\nOnCalendar={calendar}\nPersistent=true\n"
                    + jitter
                    + "[Install]\nWantedBy=timers.target\n"
                )
        owned = self.owned_units(app)
        self.stop_timers(app)
        for filename in owned - entries.keys():
            self.command("systemctl", "disable", "--now", filename)
            (units / filename).unlink()
        for filename, content in entries.items():
            self.write_unit(
                filename, "# Managed app " + app.registration.app_id + "\n" + content
            )
        self.reload_units()
        if entries:
            self.command("systemctl", "enable", prefix + "app.service")
        for filename in entries:
            if filename.endswith(".timer"):
                self.command("systemctl", "enable", "--now", filename)

    def trigger(self) -> None:
        requests = str(Path(self.policy.spool) / "requests").replace("%", "%%")
        entries = {
            "hp-managed-installer.service": (
                "[Unit]\nAfter=docker.service\nRequires=docker.service\n"
                f"[Service]\nType=oneshot\nExecStart={self.helper_argv()}\n"
            ),
            "hp-managed-installer.path": (
                f"[Path]\nPathChanged={requests}\nUnit=hp-managed-installer.service\n"
                "[Install]\nWantedBy=multi-user.target\n"
            ),
            "hp-managed-installer.timer": (
                "[Timer]\nOnBootSec=2min\nOnUnitActiveSec=5min\n"
                "Unit=hp-managed-installer.service\n[Install]\nWantedBy=timers.target\n"
            ),
        }
        for filename, content in entries.items():
            self.write_unit(filename, "# Managed installer\n" + content)
        if (Path(self.policy.units) / PENDING_RELOAD).exists():
            self.reload_units()
        self.command("systemctl", "enable", "--now", "hp-managed-installer.path")
        self.command("systemctl", "enable", "--now", "hp-managed-installer.timer")

    def installed_authority(self, app_id: str) -> Authority:
        try:
            payload = self.child(
                self.policy.core_uid,
                self.policy.core_gid,
                "installed",
                self.policy.database,
                app_id,
            )
        except HostOperationFailed as err:
            if err.returncode == DEFERRED:
                raise ReconciliationPending(
                    "Installation requires reconciliation"
                ) from err
            raise
        return Authority.model_validate_json(payload)

    def boot(self, app_id: str) -> None:
        self.current_policy()
        authority = self.installed_authority(app_id)
        app = self.policy.app(app_id)
        current = self.installed(authority, app)
        if (
            authority.registration != app.registration
            or current.selection.policy_sha256 != app_fingerprint(self.policy, app_id)
            or authority.credential_path != app.credential_file
            or authority.app_api_url != self.policy.app_api_url
            or authority.timezone != self.policy.timezone
            or current.selection.environment != self.policy.environment
        ):
            raise ContractError("Installed authority differs from host policy")
        release = self.retained_release(app, current)
        name = "hp-app-" + app_id
        actual = self.owned(name, app_id)
        exact = actual is not None and (
            actual.image == current.image_id
            and actual.config.labels.get("home-platform.operation")
            == str(current.operation_id)
        )
        if actual is not None and not exact:
            raise ContractError("Container contradicts installed generation")
        self.limits()
        if self.local_image(release.image) != current.image_id:
            raise ContractError("Installed image changed")
        self.credential_snapshot(authority, app)
        self.current_policy()
        fresh = self.installed_authority(app_id)
        if fresh != authority:
            raise ContractError("Installed authority changed before restart")
        if exact and actual is not None and actual.state.running:
            try:
                self.probe(authority, app, release, current.image_id)
                return
            except (ContractError, OSError, subprocess.SubprocessError, ValueError):
                pass
        if actual is not None:
            self.command("docker", "stop", name)
        self.remove(name, app_id)
        self.start(
            authority,
            app,
            release,
            release.image,
            installed_operation_id=current.operation_id,
        )
        try:
            self.probe(authority, app, release, current.image_id)
        except (ContractError, OSError, subprocess.SubprocessError, ValueError):
            self.remove(name, app_id)
            raise

    def installed(self, authority: Authority, app: HostApp) -> InstalledSnapshot:
        current = self.current(app)
        if current is None:
            raise ContractError("Application has no accepted installation")
        if authority.previous != current:
            raise ContractError("Core installation history differs from root journal")
        running = running_of(current)
        if running is None:
            raise ContractError("Application is retired")
        return running

    def daily_backup(self, app_id: str) -> None:
        app = self.policy.app(app_id)
        if app.state is None:
            return
        authority = Authority.model_validate_json(
            self.child(
                self.policy.core_uid,
                self.policy.core_gid,
                "installed",
                self.policy.database,
                app_id,
            )
        )
        self.installed(authority, app)
        directory = Path(str(app.registration.bindings.backup_directory))
        self.backup(
            app,
            app.state.sqlite_file,
            directory / ("daily-" + time.strftime("%Y%m%d-%H%M%S") + ".sqlite"),
        )
        for path in sorted(directory.glob("daily-*.sqlite"), reverse=True)[14:]:
            read_owned(path, 0, 1024**3)
            path.unlink()


def unit_paths(paths: Iterable[str]) -> str:
    return " ".join(
        '"' + path.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
        for path in paths
    )


def systemd_argument(value: str) -> str:
    return (
        '"'
        + value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        + '"'
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--policy", type=Path, default=Path("/etc/home-platform/installer.json")
    )
    parser.add_argument("--backup")
    parser.add_argument("--run")
    args = parser.parse_args()
    policy = load_policy(args.policy)
    validate_paths(policy)
    installer = Installer(policy, args.policy)
    with (Path(policy.journal) / "installer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        installer.sweep_scratch()
        installer.sweep_markers()
        installer.advertise()
        if args.run:
            try:
                installer.boot(args.run)
            except ReconciliationPending:
                raise SystemExit(DEFERRED) from None
            container_id = installer.command(
                "docker", "inspect", "--format", "{{.Id}}", "hp-app-" + args.run
            ).strip()

            def stop(signum: int, frame: object) -> None:
                installer.command("docker", "stop", container_id)
                raise SystemExit(0)

            signal.signal(signal.SIGTERM, stop)
            fcntl.flock(lock, fcntl.LOCK_UN)
            installer.command("docker", "wait", container_id, timeout=365 * 24 * 3600)
            raise ContractError("Application stopped")
        if args.backup:
            installer.daily_backup(args.backup)
            return
        installer.trigger()
        processed = 0
        for path in sorted((Path(policy.spool) / "requests").glob("*.json")):
            if processed >= 128:
                break
            try:
                hint = parse_document(OperationHint, read_owned(path, policy.core_uid))
                if path.name != str(hint.operation_id) + ".json":
                    raise ContractError("Operation file name differs")
                receipt = Path(policy.spool) / "receipts" / path.name
                if receipt.exists():
                    read_owned(receipt, 0)
                    continue
                processed += 1
                installer.execute(hint.operation_id)
            except (ContractError, OSError, ValueError, subprocess.SubprocessError):
                continue


if __name__ == "__main__":
    main()

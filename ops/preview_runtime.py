"""Only this guest-local module owns preview Docker execution."""

import json
import os
import re
import secrets
import shutil
import subprocess
import time
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import httpx
from pydantic import Field, StringConstraints

from app.accounts import AccountStore, UserCreate
from app.app_contracts import (
    Contract,
    ContractError,
    HostMount,
    HostPath,
    Identifier,
    ManagedInstalled,
    OperatorBindings,
    Presentation,
    Probe,
    PublishedSource,
    Registration,
    ResourcePolicy,
    SecretFile,
    Setting,
)
from app.app_previews import CHECKS, MAX_VERSION, PreviewResult, https_origin
from app.app_publication import VerifiedPublication
from app.app_registry import AppRegistry
from app.app_runtime import docker_run_argv, runtime_spec
from app.app_workloads import WorkloadStore
from app.config import InstanceFacts
from app.database import Database
from app.repository import JobRepository
from app.storage import member_storage_path, member_workspace_path
from ops.preview_checker import PORTABLE_CHECKS, RunningTarget, check_running

OWNER_LABEL = "home-platform.preview-owner"
ATTEMPT_LABEL = "home-platform.preview-attempt"


def reported_version(response: httpx.Response) -> str | None:
    if response.status_code != 200 or len(response.content) > 64 * 1024:
        return None
    try:
        value = response.json()
    except ValueError:
        return None
    version = value.get("version") if isinstance(value, dict) else None
    if not isinstance(version, str):
        return None
    version = version.strip()
    return version if re.fullmatch(f"[ -~]{{1,{MAX_VERSION}}}", version) else None


class Fixtures(Contract):
    secret_files: dict[str, HostPath] = Field(default_factory=dict)
    settings: dict[str, str] = Field(default_factory=dict)
    integrations: dict[Identifier, HostPath] = Field(default_factory=dict)


class GuestConfig(Contract):
    root_directory: HostPath
    public_origin: str
    control_token_file: HostPath
    core_image: Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
    worker_image: Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
    fixture_login: Annotated[str, StringConstraints(min_length=3, max_length=320)]
    apps: dict[Identifier, Fixtures]
    port: Annotated[int, Field(ge=1024, le=65535)] = 8090


class GuestRuntime:
    def __init__(self, config: GuestConfig) -> None:
        self.config = config
        https_origin(config.public_origin)
        self.root = Path(config.root_directory)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.owner = self.root.name
        if not self.owner.replace("-", "").isalnum():
            raise ContractError("Guest root basename must be alphanumeric")

    def docker(self, *args: str, timeout: int = 120) -> str:
        result = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode:
            raise ContractError("Guest Docker operation failed")
        return result.stdout.strip()

    def labels(self, attempt: UUID) -> list[str]:
        return [
            "--label",
            OWNER_LABEL + "=" + self.owner,
            "--label",
            ATTEMPT_LABEL + "=" + str(attempt),
        ]

    def name(self, attempt: UUID, role: str) -> str:
        return "hp-preview-" + attempt.hex + "-" + role

    def endpoint(self, attempt: UUID, role: Literal["app", "core"], port: int) -> str:
        network_name = self.name(attempt, "net")
        try:
            container = json.loads(
                self.docker("container", "inspect", self.name(attempt, role))
            )[0]
            network = json.loads(self.docker("network", "inspect", network_name))[0]
            expected = {
                OWNER_LABEL: self.owner,
                ATTEMPT_LABEL: str(attempt),
            }
            attachments = container["NetworkSettings"]["Networks"]
            if (
                container["State"]["Running"] is not True
                or any(
                    container["Config"]["Labels"].get(k) != v
                    for k, v in expected.items()
                )
                or any(network["Labels"].get(k) != v for k, v in expected.items())
                or network["Internal"] is not True
                or network["Driver"] != "bridge"
                or set(attachments) != {network_name}
                or attachments[network_name]["NetworkID"] != network["Id"]
                or container["HostConfig"]["PortBindings"]
            ):
                raise ValueError
            address = IPv4Address(attachments[network_name]["IPAddress"])
            if (
                not address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_unspecified
                or address.is_multicast
            ):
                raise ValueError
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ContractError(
                "Preview endpoint is not an owned internal container"
            ) from None
        return f"http://{address}:{port}"

    def cleanup(self, attempt: UUID) -> None:
        ids = self.docker(
            "ps",
            "-aq",
            "--filter",
            "label=" + OWNER_LABEL + "=" + self.owner,
            "--filter",
            "label=" + ATTEMPT_LABEL + "=" + str(attempt),
        ).split()
        if ids:
            self.docker("rm", "-f", *ids)
        networks = self.docker(
            "network",
            "ls",
            "-q",
            "--filter",
            "label=" + OWNER_LABEL + "=" + self.owner,
            "--filter",
            "label=" + ATTEMPT_LABEL + "=" + str(attempt),
        ).split()
        if networks:
            self.docker("network", "rm", *networks)

    def owned_attempts(self) -> set[UUID]:
        found = set()
        for kind in ("container", "network"):
            ids = self.docker(
                *(("ps", "-aq") if kind == "container" else ("network", "ls", "-q")),
                "--filter",
                "label=" + OWNER_LABEL + "=" + self.owner,
            ).split()
            if not ids:
                continue
            values = json.loads(self.docker(kind, "inspect", *ids))
            for value in values:
                labels = (
                    value["Config"]["Labels"]
                    if kind == "container"
                    else value["Labels"]
                )
                found.add(UUID(labels[ATTEMPT_LABEL]))
        return found

    def install(
        self, initial: PreviewResult, release: VerifiedPublication
    ) -> PreviewResult:
        attempt = initial.attempt_id
        manifest = release.manifest
        fixture = self.config.apps.get(manifest.app_id)
        if fixture is None or any(
            name not in fixture.secret_files for name in manifest.secrets
        ):
            raise ContractError("Synthetic application bindings are missing")
        if any(
            not Path(fixture.secret_files[name]).is_file() for name in manifest.secrets
        ):
            raise ContractError("Synthetic secret file is missing")
        if any(not Path(source).is_dir() for source in fixture.integrations.values()):
            raise ContractError("Synthetic integration directory is missing")
        directory = self.root / str(attempt)
        directory.mkdir(mode=0o700)
        integrations = []
        for name, source in fixture.integrations.items():
            copy = directory / "integrations" / name
            shutil.copytree(source, copy, symlinks=True)
            integrations.append(HostMount(name=name, source=str(copy)))
        core_dir = directory / "core"
        storage = core_dir / "storage"
        state = directory / "app"
        worker_dir = directory / "worker"
        for path in (core_dir, state, worker_dir):
            path.mkdir(mode=0o700)
        for name in ("api-token", "context-key", "bridge-token"):
            (core_dir / name).write_text(secrets.token_hex(32))
            (core_dir / name).chmod(0o600)
        core_name = self.name(attempt, "core")
        app_name = self.name(attempt, "app")
        route = f"/previews/{attempt}/{manifest.app_id}"
        instance = InstanceFacts(
            "preview-" + attempt.hex, f"http://{core_name}:8000/api/apps/v1", "UTC"
        )
        database = Database(core_dir / "core.db")
        database.initialize()
        # A fresh isolated preview owns only its selected application's registry.
        with database.connect() as connection:
            connection.execute("DELETE FROM app_registrations")
        registry = AppRegistry(database)
        registry.publish_instance(instance)
        registration = Registration(
            app_id=manifest.app_id,
            route=route,
            source=PublishedSource(
                kind="published",
                repository=release.publication.repository,
                branch=release.publication.branch,
                workflow=release.publication.workflow,
                image_repository=manifest.image.split("@", 1)[0],
            ),
            access="linked-user",
            capabilities=manifest.capabilities,
            presentation=Presentation(
                label=manifest.app_id,
                health_name=manifest.app_id,
                position=1,
                accent="#008080",
            ),
            bindings=OperatorBindings(
                # Previews never run maintenance, so admit whatever the release
                # declares rather than refusing it under the production default.
                resources=ResourcePolicy(
                    memory_bytes=256 * 1024**2,
                    cpu_limit=0.5,
                    maintenance_timeout_seconds=max(
                        (item.timeout_seconds for item in manifest.maintenance),
                        default=ResourcePolicy().maintenance_timeout_seconds,
                    ),
                ),
                state_directory=str(state),
                secret_files=tuple(
                    SecretFile(name=name, file_path=fixture.secret_files[name])
                    for name in manifest.secrets
                ),
                settings=tuple(
                    Setting(name=name, value=value)
                    for name, value in fixture.settings.items()
                ),
                integration_bindings=tuple(fixture.integrations),
            ),
            installation=ManagedInstalled(
                owner="platform",
                host_port=19001,
                probe=Probe(
                    service=manifest.app_id,
                    readiness="v1-status",
                    ready_path=manifest.runtime.ready_path,
                    version_path=manifest.runtime.version_path,
                ),
            ),
        )
        registry.register(registration.model_dump_json().encode())
        registry.ingest_release(manifest.app_id, release.files["manifest.json"])
        registry.issue_credential(manifest.app_id, directory / "app-credential")
        account = AccountStore(database)
        user = account.create(
            UserCreate(username="preview-user", password=secrets.token_hex(24))
        )
        account.link_external_identity(
            user.id, "tailscale", self.config.fixture_login, "Preview user"
        )
        for tree in (
            member_storage_path(user.id, "Home"),
            member_workspace_path(user.id, "Home/Workspace"),
            member_storage_path(user.id, "Shared"),
        ):
            (storage / tree).mkdir(parents=True, exist_ok=True, mode=0o700)
        for workload in manifest.workloads:
            archive = directory / workload.asset
            archive.write_bytes(release.files[workload.asset])
            WorkloadStore(database).install(
                manifest.app_id, initial.manifest_sha256, workload.id, archive
            )
        managed = registry.managed_app(manifest.app_id)
        credential = registry.active_credential(manifest.app_id)
        assert managed is not None and credential is not None
        app_command = list(
            docker_run_argv(
                runtime_spec(
                    managed,
                    credential,
                    instance,
                    manifest.runtime.port,
                    tuple(integrations),
                ),
                manifest.image,
                (os.getuid(), os.getgid()),
            )
        )
        database.close()
        self.docker("image", "inspect", self.config.core_image)
        self.docker("image", "inspect", self.config.worker_image)
        self.docker("pull", manifest.image, timeout=600)
        image_info = json.loads(self.docker("image", "inspect", manifest.image))[0]
        if manifest.image not in image_info["RepoDigests"]:
            raise ContractError(
                "Pulled image does not have the selected repository digest"
            )
        network = self.name(attempt, "net")
        self.docker("network", "create", "--internal", *self.labels(attempt), network)
        common = [
            "run",
            "--detach",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--memory",
            str(256 * 1024**2),
            "--cpus",
            "1",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=64m",
            "--network",
            network,
            *self.labels(attempt),
        ]
        env = {
            "HOME_PLATFORM_DB_PATH": str(core_dir / "core.db"),
            "HOME_PLATFORM_API_TOKEN_FILE": str(core_dir / "api-token"),
            "HOME_PLATFORM_APP_CONTEXT_KEY_FILE": str(core_dir / "context-key"),
            "HOME_PLATFORM_ENVIRONMENT": instance.environment,
            "HOME_PLATFORM_APP_API_URL": instance.app_api_url,
            "HOME_PLATFORM_UPLOAD_DIR": str(core_dir / "uploads"),
            "HOME_PLATFORM_ARTIFACT_DIR": str(core_dir / "artifacts"),
            "HOME_PLATFORM_STORAGE_DIR": str(storage),
            "HOME_PLATFORM_MEMBER_STORAGE_ENABLED": "true",
            "PREVIEW_APP_HOST": app_name,
            "PREVIEW_APP_PORT": str(manifest.runtime.port),
            "PREVIEW_BRIDGE_TOKEN_FILE": str(core_dir / "bridge-token"),
        }
        args = [
            *common,
            "--name",
            core_name,
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--mount",
            f"type=bind,src={core_dir},dst={core_dir}",
        ]
        for key, value in env.items():
            args.extend(("--env", key + "=" + value))
        self.docker(*args, self.config.core_image, "python", "-m", "ops.preview_core")
        app_command[app_command.index("--name") + 1] = app_name
        app_command[app_command.index("--restart") + 1] = "no"
        publish_index = app_command.index("--publish")
        del app_command[publish_index : publish_index + 2]
        host_index = app_command.index("--add-host")
        del app_command[host_index : host_index + 2]
        app_command[-1:-1] = ["--network", network, *self.labels(attempt)]
        self.docker(*app_command[1:])
        socket_gid = Path("/var/run/docker.sock").stat().st_gid
        worker_args = [
            *common,
            "--name",
            self.name(attempt, "worker"),
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--group-add",
            str(socket_gid),
            "--mount",
            f"type=bind,src={worker_dir},dst={worker_dir}",
            "--mount",
            f"type=bind,src={core_dir / 'api-token'},dst=/run/worker-token,readonly",
            "--mount",
            "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
            "--env",
            "HOME_PLATFORM_CONTAINER_IMAGE=" + self.config.worker_image,
            self.config.core_image,
            "python",
            "-m",
            "worker.main",
            "--url",
            f"http://{core_name}:8000",
            "--worker-id",
            "preview-" + attempt.hex,
            "--token-file",
            "/run/worker-token",
            "--data-dir",
            str(worker_dir),
            "--max-cache-bytes",
            str(512 * 1024**2),
        ]
        self.docker(*worker_args)
        worker_database = Database(core_dir / "core.db")
        try:
            deadline = time.monotonic() + 30
            while (
                JobRepository(worker_database).set_worker_enabled(
                    "preview-" + attempt.hex,
                    True,
                    datetime.now(UTC) - timedelta(seconds=30),
                )
                is None
            ):
                if time.monotonic() >= deadline:
                    raise ContractError("Synthetic worker did not register")
                time.sleep(1)
            JobRepository(worker_database).set_worker_capacity(
                "preview-" + attempt.hex,
                1.0,
                512,
                datetime.now(UTC) - timedelta(seconds=30),
            )
        finally:
            worker_database.close()
        return self.verify(initial, release, image_info["Id"])

    def verify(
        self, initial: PreviewResult, release: VerifiedPublication, image_id: str
    ) -> PreviewResult:
        manifest = release.manifest
        if (
            manifest.app_id != initial.app_id
            or release.publication.manifest_sha256 != initial.manifest_sha256
            or release.artifact_sha256 != initial.bundle_sha256
        ):
            raise ContractError("Preview release does not match selected attempt")
        directory = self.root / str(initial.attempt_id)
        route = f"/previews/{initial.attempt_id}/{initial.app_id}"
        headers = {
            "X-Preview-Bridge": (directory / "core/bridge-token").read_text(),
            "Tailscale-User-Login": self.config.fixture_login,
        }
        app_url = self.endpoint(initial.attempt_id, "app", manifest.runtime.port)
        core_url = self.endpoint(initial.attempt_id, "core", 8001)
        with httpx.Client(trust_env=False, timeout=5) as client:
            deadline = time.monotonic() + 60
            while True:
                try:
                    ready = client.get(app_url + manifest.runtime.ready_path)
                    if ready.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() >= deadline:
                    raise ContractError("Preview did not become ready")
                time.sleep(1)
            proxied = client.get(
                app_url + "/",
                headers={
                    "X-Platform-User-Context": "forged",
                    "X-Forwarded-Proto": "https",
                    "Tailscale-User-Login": self.config.fixture_login,
                },
            )
            try:
                version = reported_version(
                    client.get(app_url + manifest.runtime.version_path)
                )
            except httpx.HTTPError:
                version = None
        if proxied.status_code not in {401, 403}:
            raise ContractError("The application accepted forged proxy context")
        portable = check_running(
            release.files["manifest.json"],
            initial.manifest_sha256,
            RunningTarget(app_url, core_url, route, headers),
            directory,
        ).checks()
        containers = json.loads(
            self.docker(
                "container",
                "inspect",
                *[
                    self.name(initial.attempt_id, role)
                    for role in ("app", "core", "worker")
                ],
            )
        )
        if (
            len(containers) != 3
            or any(item["State"]["Running"] is not True for item in containers)
            or containers[0]["Image"] != image_id
        ):
            raise ContractError("Preview containers are not running the selected image")
        script = (
            "import urllib.request,urllib.error; "
            "r=urllib.request.Request("
            + repr(f"http://{self.name(initial.attempt_id, 'core')}:8000" + route + "/")
            + ",headers="
            + repr(
                {
                    "X-Forwarded-Proto": "https",
                    "Tailscale-User-Login": self.config.fixture_login,
                }
            )
            + "); "
            "\ntry:\n print(urllib.request.urlopen(r,timeout=5).status)"
            "\nexcept urllib.error.HTTPError as e:\n print(e.code)"
        )
        denied = self.docker(
            "run",
            "--rm",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "32",
            "--memory",
            str(64 * 1024**2),
            "--cpus",
            "0.1",
            "--network",
            self.name(initial.attempt_id, "net"),
            *self.labels(initial.attempt_id),
            self.config.core_image,
            "python",
            "-c",
            script,
        )
        if denied.strip() != "401":
            raise ContractError(
                "An application peer could impersonate the trusted proxy"
            )
        checks = {
            "manifest": portable.keys() == PORTABLE_CHECKS,
            "image": containers[0]["Image"] == image_id,
            "startup": all(item["State"]["Running"] is True for item in containers),
            "readiness": portable["health"] and portable["ready"],
            "identity": portable["version"]
            and portable["routed-access"]
            and portable["base-path"],
            "direct_denial": portable["direct-context"]
            and portable["forged-context"]
            and proxied.status_code in {401, 403},
            "forged_proxy_denial": denied.strip() == "401",
        }
        if checks.keys() != CHECKS or not all(checks.values()):
            raise ContractError("Preview readiness evidence is incomplete")
        return initial.model_copy(
            update={
                "state": "ready",
                "image_id": image_id,
                "preview_url": self.config.public_origin + route + "/",
                "checks": checks,
                "version": version,
            }
        )

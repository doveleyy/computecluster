from dataclasses import dataclass

from app.app_contracts import (
    ContractError,
    HostMount,
    ResourcePolicy,
    integration_variable,
    secret_variable,
)
from app.app_registry import AppCredential, ManagedApp
from app.config import InstanceFacts

CONTAINER_STATE_DIR = "/data"
CONTAINER_CREDENTIAL_FILE = "/run/secrets/app-credential"
INTEGRATIONS_DIR = "/integrations"
TMPFS = "/tmp:rw,nosuid,nodev,size=16m"


@dataclass(frozen=True)
class Mount:
    source: str
    target: str
    read_only: bool


@dataclass(frozen=True)
class RuntimeSpec:
    app_id: str
    environment: tuple[tuple[str, str], ...]
    mounts: tuple[Mount, ...]
    host_port: int
    container_port: int
    resources: ResourcePolicy


def runtime_spec(
    app: ManagedApp,
    credential: AppCredential,
    instance: InstanceFacts,
    container_port: int,
    integrations: tuple[HostMount, ...] = (),
) -> RuntimeSpec:
    bindings = app.registration.bindings
    if bindings.state_directory is None:
        raise ContractError("Managed state directory is missing")
    sources = {mount.name: mount for mount in integrations}
    if len(sources) != len(integrations) or sources.keys() != set(
        bindings.integration_bindings
    ):
        raise ContractError("Integration mounts differ from the registered bindings")
    if (
        credential.app_id != app.app_id
        or credential.environment != instance.environment
    ):
        raise ContractError("Runtime credential does not match application instance")
    environment = [
        ("APP_ID", app.app_id),
        ("APP_BASE_PATH", app.route),
        ("PLATFORM_API_URL", instance.app_api_url),
        ("APP_CREDENTIAL_FILE", CONTAINER_CREDENTIAL_FILE),
        ("APP_STATE_DIR", CONTAINER_STATE_DIR),
        ("TZ", instance.timezone),
    ]
    mounts = [
        Mount(bindings.state_directory, CONTAINER_STATE_DIR, False),
        Mount(str(credential.file_path), CONTAINER_CREDENTIAL_FILE, True),
    ]
    environment.extend((setting.name, setting.value) for setting in bindings.settings)
    for secret in bindings.secret_files:
        target = "/run/secrets/" + secret.name
        environment.append((secret_variable(secret.name), target))
        mounts.append(Mount(secret.file_path, target, True))
    for name in bindings.integration_bindings:
        target = INTEGRATIONS_DIR + "/" + name
        environment.append((integration_variable(name), target))
        mounts.append(Mount(sources[name].source, target, sources[name].read_only))
    return RuntimeSpec(
        app.app_id,
        tuple(environment),
        tuple(mounts),
        app.installation.host_port,
        container_port,
        bindings.resources,
    )


def docker_run_argv(
    spec: RuntimeSpec, image: str, user: tuple[int, int]
) -> tuple[str, ...]:
    argv = [
        "docker",
        "run",
        "--detach",
        "--name",
        "hp-app-" + spec.app_id,
        "--restart",
        "unless-stopped",
        "--user",
        f"{user[0]}:{user[1]}",
        "--read-only",
        "--tmpfs",
        TMPFS,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(spec.resources.pids_limit),
        "--memory",
        str(spec.resources.memory_bytes),
        "--memory-swap",
        str(spec.resources.memory_bytes),
        "--cpus",
        str(spec.resources.cpu_limit),
        "--add-host",
        "host.docker.internal:host-gateway",
        "--publish",
        f"127.0.0.1:{spec.host_port}:{spec.container_port}",
        "--label",
        "home-platform.app=" + spec.app_id,
    ]
    for mount in spec.mounts:
        argv.extend(
            (
                "--mount",
                f"type=bind,source={mount.source},target={mount.target}"
                + (",readonly" if mount.read_only else ""),
            )
        )
    for name, value in spec.environment:
        argv.extend(("--env", name + "=" + value))
    argv.append(image)
    return tuple(argv)

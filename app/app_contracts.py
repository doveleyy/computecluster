import json
import re
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

MAX_DOCUMENT_BYTES = 64 * 1024
Capability = Literal["identity", "jobs", "files"]
Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", max_length=63),
]
Text = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f\x7f]*$"),
]
Port = Annotated[int, Field(ge=1, le=65535)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ContractError(ValueError):
    pass


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def canonical_path(value: str) -> str:
    if not re.fullmatch(r"/(?:[A-Za-z0-9_.~-]+(?:/[A-Za-z0-9_.~-]+)*)?", value) or any(
        part in {".", ".."} for part in value.split("/")
    ):
        raise ValueError("Invalid canonical path")
    return value


def relative_path(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.~-]+(?:/[A-Za-z0-9_.~-]+)*", value) or any(
        part in {".", ".."} for part in value.split("/")
    ):
        raise ValueError("Invalid relative path")
    return value


def endpoint_path(value: str) -> str:
    if value == "/":
        return value
    if not value.startswith("/"):
        raise ValueError("Expected absolute internal path")
    relative_path(value[1:])
    return value


def basename(value: str) -> str:
    relative_path(value)
    if "/" in value:
        raise ValueError("Expected basename")
    return value


def host_path(value: str) -> str:
    if (
        not value.startswith("/")
        or any(character in value for character in "\x00\r\n,")
        or any(part in {".", ".."} for part in value.split("/"))
    ):
        raise ValueError("Expected absolute host path without dot segments or commas")
    return value


PathValue = Annotated[
    str, StringConstraints(max_length=256), AfterValidator(canonical_path)
]
EndpointPath = Annotated[
    str, StringConstraints(max_length=256), AfterValidator(endpoint_path)
]
RelativePath = Annotated[
    str, StringConstraints(max_length=256), AfterValidator(relative_path)
]
Basename = Annotated[str, StringConstraints(max_length=128), AfterValidator(basename)]
HostPath = Annotated[str, StringConstraints(max_length=1024), AfterValidator(host_path)]
IMAGE_REPOSITORY_PATTERN = (
    r"[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]+)?/"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
)
ImageRepository = Annotated[
    str,
    StringConstraints(
        pattern=f"^{IMAGE_REPOSITORY_PATTERN}$",
        max_length=256,
    ),
]
ImageReference = Annotated[
    str,
    StringConstraints(
        pattern=rf"^{IMAGE_REPOSITORY_PATTERN}@sha256:[0-9a-f]{{64}}$",
        max_length=335,
    ),
]
ImageId = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
SecretName = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$", max_length=63)
]
PLATFORM_VARIABLES = frozenset(
    {
        "APP_ID",
        "APP_BASE_PATH",
        "PLATFORM_API_URL",
        "APP_CREDENTIAL_FILE",
        "APP_STATE_DIR",
        "TZ",
    }
)


def secret_variable(name: str) -> str:
    return f"APP_SECRET_{name.upper().replace('-', '_')}_FILE"


def integration_variable(name: str) -> str:
    return f"APP_INTEGRATION_{name.upper().replace('-', '_')}_DIR"


def require_unique(values: tuple[object, ...]) -> None:
    if len(set(values)) != len(values):
        raise ValueError("Duplicate entries")


class PublishedSource(Contract):
    kind: Literal["published"]
    repository: Annotated[
        str,
        StringConstraints(
            pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$",
            max_length=256,
        ),
    ]
    branch: RelativePath
    workflow: RelativePath
    image_repository: ImageRepository


class LegacySource(Contract):
    kind: Literal["legacy"]
    owner: Literal["current-platform-checkout"] = "current-platform-checkout"


Readiness = Literal["legacy-json", "v1-status"]


class Probe(Contract):
    service: Identifier
    ready_path: EndpointPath = "/ready"
    version_path: EndpointPath = "/version"
    readiness: Readiness


class LegacyInstalled(Contract):
    owner: Literal["legacy-scripts"]
    host_port: Port
    probe: Probe | None = None


class ManagedInstalled(Contract):
    owner: Literal["platform"]
    host_port: Port
    probe: Probe

    @model_validator(mode="after")
    def require_v1_probe(self) -> Self:
        if self.probe.readiness != "v1-status":
            raise ValueError("Managed installation requires a v1-status probe")
        return self


class Presentation(Contract):
    label: Text
    health_name: Text
    position: Annotated[int, Field(ge=1, le=10000)]
    accent: Annotated[str, StringConstraints(pattern=r"^#[0-9a-fA-F]{6}$")]
    kicker: Annotated[str, StringConstraints(max_length=256)] = ""
    description: Annotated[str, StringConstraints(max_length=1024)] = ""


class SecretFile(Contract):
    name: SecretName
    file_path: HostPath


UnitPath = Annotated[HostPath, StringConstraints(pattern=r"^[^\x00-\x1f\x7f]*$")]


class HostMount(Contract):
    name: Identifier
    source: UnitPath
    read_only: bool = False


class Setting(Contract):
    name: Annotated[
        str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=128)
    ]
    value: Annotated[str, StringConstraints(max_length=4096)]


class ResourcePolicy(Contract):
    cpu_limit: Annotated[float, Field(gt=0, le=64, allow_inf_nan=False)] = 0.5
    memory_bytes: Annotated[int, Field(ge=1024 * 1024, le=1024**4)] = 256 * 1024 * 1024
    pids_limit: Annotated[int, Field(ge=1, le=65536)] = 128
    maintenance_timeout_seconds: Annotated[int, Field(ge=1, le=86400)] = 300


class OperatorBindings(Contract):
    secret_files: Annotated[tuple[SecretFile, ...], Field(max_length=32)] = ()
    settings: Annotated[tuple[Setting, ...], Field(max_length=64)] = ()
    state_directory: HostPath | None = None
    backup_directory: HostPath | None = None
    integration_bindings: Annotated[tuple[Identifier, ...], Field(max_length=32)] = ()
    resources: ResourcePolicy = ResourcePolicy()

    @model_validator(mode="after")
    def validate_names(self) -> Self:
        variables = tuple(secret_variable(secret.name) for secret in self.secret_files)
        require_unique(variables)
        require_unique(tuple(setting.name for setting in self.settings))
        require_unique(self.integration_bindings)
        if any(
            setting.name in PLATFORM_VARIABLES
            or setting.name.startswith(
                ("APP_SECRET_", "APP_INTEGRATION_", "HP_FIXTURE_")
            )
            or setting.name
            in {"HP_FIXTURE_USER", "APP_ALLOW_DEV_IDENTITY", "HOME_PLATFORM_DEV_USER"}
            for setting in self.settings
        ):
            raise ValueError("Reserved setting name")
        return self


class Registration(Contract):
    app_id: Identifier
    route: PathValue
    source: Annotated[PublishedSource | LegacySource, Field(discriminator="kind")]
    access: Literal["linked-user", "administrator"]
    capabilities: Annotated[tuple[Capability, ...], Field(min_length=1, max_length=3)]
    presentation: Presentation
    bindings: OperatorBindings = OperatorBindings()
    installation: (
        Annotated[LegacyInstalled | ManagedInstalled, Field(discriminator="owner")]
        | None
    ) = None

    @field_validator("route")
    @classmethod
    def require_lowercase_route(cls, value: str) -> str:
        if value != value.lower():
            raise ValueError("Application route must be lowercase")
        return value

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        require_unique(self.capabilities)
        if "identity" not in self.capabilities:
            raise ValueError("Identity capability required")
        if self.route == "/":
            raise ValueError("Application route cannot be root")
        if (
            isinstance(self.installation, ManagedInstalled)
            and self.bindings.state_directory is None
        ):
            raise ValueError("Managed installation requires a state directory")
        if isinstance(self.installation, ManagedInstalled) and any(
            secret.name == "app-credential" for secret in self.bindings.secret_files
        ):
            raise ValueError(
                "Managed installation reserves the app-credential secret name"
            )
        if (
            self.installation is not None
            and self.installation.probe is not None
            and self.installation.probe.readiness == "v1-status"
            and self.installation.probe.service != self.app_id
        ):
            raise ValueError("Version service must equal application ID")
        return self


class Runtime(Contract):
    port: Port
    health_path: EndpointPath
    ready_path: EndpointPath
    version_path: EndpointPath


class SQLiteState(Contract):
    sqlite_file: Basename


class Maintenance(Contract):
    name: Identifier
    argv: Annotated[tuple[Text, ...], Field(min_length=1, max_length=32)]
    interval_hours: Annotated[int, Field(ge=1, le=8760)]
    timeout_seconds: Annotated[int, Field(ge=1, le=86400)]


class Workload(Contract):
    id: Identifier
    asset: Basename
    sha256: Sha256
    entrypoint: RelativePath
    runtime: Literal["scientific-python:1"]


class ReleaseManifest(Contract):
    contract_version: Annotated[int, Field(ge=1, le=1)]
    app_id: Identifier
    source_commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    image: ImageReference
    runtime: Runtime
    capabilities: Annotated[tuple[Capability, ...], Field(min_length=1, max_length=3)]
    state: SQLiteState | None = None
    secrets: Annotated[tuple[SecretName, ...], Field(max_length=32)] = ()
    maintenance: Annotated[tuple[Maintenance, ...], Field(max_length=32)] = ()
    workloads: Annotated[tuple[Workload, ...], Field(max_length=32)] = ()

    @field_validator("state", mode="before")
    @classmethod
    def require_state_object(cls, value: object) -> object:
        if value is None:
            raise ValueError("State must be an object when present")
        return value

    @model_validator(mode="after")
    def validate_entries(self) -> Self:
        require_unique(self.capabilities)
        require_unique(tuple(secret_variable(name) for name in self.secrets))
        require_unique(tuple(item.name for item in self.maintenance))
        require_unique(tuple(item.id for item in self.workloads))
        if "identity" not in self.capabilities or (
            self.workloads and "jobs" not in self.capabilities
        ):
            raise ValueError("Required capability missing")
        return self


def parse_document[C: Contract](model: type[C] | TypeAdapter[C], payload: bytes) -> C:
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise ContractError("JSON document exceeds 65536 bytes")

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def nonfinite(_value: str) -> object:
        raise ValueError("Non-finite JSON number")

    try:
        json.loads(payload, object_pairs_hook=pairs, parse_constant=nonfinite)
        if isinstance(model, TypeAdapter):
            return model.validate_json(payload)
        return model.model_validate_json(payload)
    except ValidationError as error:
        details = []
        for item in error.errors(include_input=False, include_url=False)[:5]:
            # Of the messages these models raise, only this one quotes input.
            message = (
                "Input tag does not match any expected tag"
                if item["type"] == "union_tag_invalid"
                else item["msg"]
            )
            location = ".".join(str(part) for part in item["loc"])
            details.append(f"{location}: {message}" if location else message)
        raise ContractError(
            "Invalid JSON contract document: " + "; ".join(details)
        ) from None
    except (ValueError, RecursionError, UnicodeError):
        raise ContractError("Invalid JSON contract document") from None

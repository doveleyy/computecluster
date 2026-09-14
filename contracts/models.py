from datetime import datetime
from enum import StrEnum
from ipaddress import ip_address
from pathlib import PurePosixPath
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
    model_validator,
)

JobName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=100,
        pattern=r"^[^\x00-\x1f\x7f]+$",
    ),
]

TaskId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]

WorkerId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._-]+$",
    ),
]

InputName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z][A-Za-z0-9._-]*$",
    ),
]


class JobType(StrEnum):
    SLEEP = "sleep"
    # Kept only so historical records remain readable. New submissions use
    # SubmittableJobType, which deliberately excludes this retired handler.
    DATASET_SCRIPT = "dataset_script"
    PYTHON_BATCH = "python_batch"
    BATCH = "batch"


class SubmittableJobType(StrEnum):
    SLEEP = "sleep"
    PYTHON_BATCH = "python_batch"
    BATCH = "batch"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class FailureKind(StrEnum):
    EXECUTION_ERROR = "EXECUTION_ERROR"
    INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"
    MEMORY_LIMIT_EXCEEDED = "MEMORY_LIMIT_EXCEEDED"
    TIMED_OUT = "TIMED_OUT"
    WORKER_LOST = "WORKER_LOST"
    CANCELLED_BY_USER = "CANCELLED_BY_USER"


class SleepParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seconds: int = Field(ge=1, le=300)


class ScriptName(StrEnum):
    CSV_SUMMARY = "csv_summary"


class DatasetReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=100 * 1024**3)

    @model_validator(mode="after")
    def require_safe_https_source(self) -> "DatasetReference":
        if self.url.scheme != "https":
            raise ValueError("dataset URL must use HTTPS")
        hostname = self.url.host
        if hostname is None or hostname.lower() == "localhost":
            raise ValueError("dataset URL must have a public hostname")
        try:
            address = ip_address(hostname)
        except ValueError:
            return self
        if not address.is_global:
            raise ValueError("dataset URL cannot use a private or local IP address")
        return self


class UploadedDatasetReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload_id: UUID
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=10 * 1024**2)


class UploadedScriptReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload_id: UUID
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=256 * 1024)


class UploadedProjectReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload_id: UUID
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=20 * 1024**2)


class UploadedInputReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload_id: UUID
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=20 * 1024**2)


class StorageInputReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    path: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=100 * 1024**3)

    @field_validator("path")
    @classmethod
    def path_is_safe(cls, value: str) -> str:
        if "\\" in value:
            raise ValueError("storage path must use forward slashes")
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("storage path must be normalized and relative")
        return path.as_posix()


BatchInputSource = Annotated[
    DatasetReference | UploadedInputReference | StorageInputReference,
    Field(union_mode="left_to_right"),
]

DatasetSource = Annotated[
    DatasetReference | UploadedDatasetReference | StorageInputReference,
    Field(union_mode="left_to_right"),
]


class DatasetScriptParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script: ScriptName
    dataset: DatasetSource
    timeout_seconds: int = Field(default=300, ge=1, le=3600)


class PythonBatchParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script: UploadedScriptReference
    dataset: DatasetSource
    # Ceilings are generous because long, deliberately-throttled training runs
    # are a supported use: a grid search told to use a fraction of a core will
    # take many hours by design.
    timeout_seconds: int = Field(default=1800, ge=1, le=7 * 24 * 3600)
    # Fractions below 1.0 are the point, not an edge case — 0.5 means "use half
    # a core and stay cool". The container is hard-capped by CFS quota, so this
    # throttles rather than merely deprioritising.
    cpu_limit: float = Field(default=2.0, ge=0.1, le=8.0)
    memory_mb: int = Field(default=2048, ge=256, le=16384)


class BatchParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: UploadedProjectReference
    entrypoint: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$",
    )
    runtime: str = Field(
        pattern=r"^[a-z0-9][a-z0-9._-]{0,63}:[0-9][A-Za-z0-9._-]{0,31}$"
    )
    timeout_seconds: int = Field(ge=1, le=7 * 24 * 3600)
    cpu_limit: float = Field(ge=0.1, le=8.0)
    memory_mb: int = Field(ge=256, le=16384)
    array_index: int = Field(ge=1)
    environment: dict[str, str] = Field(default_factory=dict)
    inputs: dict[InputName, BatchInputSource] = Field(
        default_factory=dict, max_length=32
    )

    @field_validator("entrypoint")
    @classmethod
    def entrypoint_is_safe(cls, value: str) -> str:
        if any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("entrypoint must be a normalized project-relative path")
        return value

    @field_validator("environment")
    @classmethod
    def environment_is_safe(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 32:
            raise ValueError("at most 32 environment values are allowed")
        for key, item in value.items():
            if not key or len(key) > 32 or not key.replace("_", "A").isalnum():
                raise ValueError(f"invalid environment key {key!r}")
            if not key[0].isalpha() or key.startswith("HOME_PLATFORM_"):
                raise ValueError(f"invalid or reserved environment key {key!r}")
            if len(item) > 1000 or any(character in item for character in "\x00\r\n"):
                raise ValueError(f"invalid environment value for {key!r}")
        return value


JobParameters = Annotated[
    SleepParameters | DatasetScriptParameters | PythonBatchParameters | BatchParameters,
    Field(union_mode="left_to_right"),
]

JobCreateParameters = Annotated[
    SleepParameters | PythonBatchParameters | BatchParameters,
    Field(union_mode="left_to_right"),
]


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: SubmittableJobType
    parameters: JobCreateParameters
    name: JobName | None = None
    target_worker_id: WorkerId | None = None

    @model_validator(mode="after")
    def type_matches_parameters(self) -> "JobCreate":
        if self.type is SubmittableJobType.SLEEP and not isinstance(
            self.parameters, SleepParameters
        ):
            raise ValueError("sleep jobs require sleep parameters")
        if self.type is SubmittableJobType.PYTHON_BATCH and not isinstance(
            self.parameters, PythonBatchParameters
        ):
            raise ValueError("python_batch jobs require Python batch parameters")
        if self.type is SubmittableJobType.BATCH and not isinstance(
            self.parameters, BatchParameters
        ):
            raise ValueError("batch jobs require batch parameters")
        return self


class SleepResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slept_seconds: int = Field(ge=1, le=300)


class DatasetScriptResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script: ScriptName
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_bytes: int = Field(ge=1)
    rows: int = Field(ge=0)
    columns: list[Annotated[str, Field(max_length=200)]] = Field(max_length=1000)
    artifact_uri: str = Field(
        min_length=1,
        max_length=500,
        pattern=r"^worker://[A-Za-z0-9._-]+/[0-9a-f-]+/summary\.json$",
    )


class PythonBatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    exit_code: int = Field(ge=0, le=0)
    stdout: str = Field(max_length=8000)
    stderr: str = Field(max_length=8000)
    output_files: list[
        Annotated[
            str,
            Field(
                min_length=1,
                max_length=200,
                pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
            ),
        ]
    ] = Field(default_factory=list, max_length=100)
    artifact_uri: str = Field(
        min_length=1,
        max_length=500,
        pattern=r"^worker://[A-Za-z0-9._-]+/[0-9a-f-]+/$",
    )


class BatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    exit_code: int = Field(ge=0, le=0)
    stdout: str = Field(max_length=8000)
    stderr: str = Field(max_length=8000)
    output_files: list[
        Annotated[
            str,
            Field(
                min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
            ),
        ]
    ] = Field(default_factory=list, max_length=100)
    artifact_uri: str = Field(
        min_length=1,
        max_length=500,
        pattern=r"^worker://[A-Za-z0-9._-]+/[0-9a-f-]+/$",
    )


JobResult = Annotated[
    SleepResult | DatasetScriptResult | PythonBatchResult | BatchResult,
    Field(union_mode="left_to_right"),
]


class JobRead(BaseModel):
    id: UUID
    name: JobName | None = None
    type: JobType
    parameters: JobParameters
    target_worker_id: WorkerId | None = None
    group_id: UUID | None = None
    task_id: TaskId | None = None
    task_index: int | None = Field(default=None, ge=0)
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    worker_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: JobResult | None = None
    error: str | None = None
    failure_kind: FailureKind | None = None
    cancellation_requested: bool = False
    attempt: int = 0
    max_attempts: int = 3
    lease_token: UUID | None = None
    lease_expires_at: datetime | None = None


class JobGroupTaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: TaskId
    job: JobCreate


class JobGroupCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: JobName
    tasks: list[JobGroupTaskCreate] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def task_ids_are_unique(self) -> "JobGroupCreate":
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task_id values must be unique within a job group")
        return self


class JobGroupRead(BaseModel):
    id: UUID
    name: JobName
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    tasks: list[JobRead]


class BatchSubmissionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: UploadedProjectReference
    entrypoint: str = Field(default="submit.hp", min_length=1, max_length=200)
    target_worker_id: WorkerId | None = None
    inputs: dict[InputName, BatchInputSource] = Field(
        default_factory=dict, max_length=32
    )


class JobCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    lease_token: UUID
    result: JobResult


class JobFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: WorkerId
    lease_token: UUID
    failure_kind: FailureKind = FailureKind.EXECUTION_ERROR
    error: str = Field(min_length=1, max_length=1000)


class WorkerHeartbeatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cancellation_requested: bool = False


class GpuMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    percent: float | None = Field(default=None, ge=0, le=100)
    memory_used: int | None = Field(default=None, ge=0)
    memory_total: int | None = Field(default=None, ge=0)
    temperature_c: float | None = Field(default=None, ge=-50, le=200)


class WorkerMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str = Field(min_length=1, max_length=64)
    logical_cores: int = Field(ge=1)
    cpu_percent: float = Field(ge=0, le=100)
    memory_percent: float = Field(ge=0, le=100)
    memory_available: int = Field(ge=0)
    memory_total: int = Field(ge=0)
    storage_percent: float = Field(ge=0, le=100)
    storage_free: int = Field(ge=0)
    storage_total: int = Field(ge=0)
    temperature_c: float | None = Field(default=None, ge=-50, le=200)
    gpus: list[GpuMetrics] = Field(default_factory=list, max_length=16)


class WorkerClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    supported_types: list[JobType] = Field(
        default_factory=lambda: [JobType.SLEEP],
        min_length=1,
    )
    metrics: WorkerMetrics | None = None


class WorkerHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    supported_types: list[JobType] = Field(
        default_factory=lambda: [JobType.SLEEP],
        min_length=1,
    )
    current_job_id: UUID | None = None
    lease_token: UUID | None = None
    metrics: WorkerMetrics | None = None

    @model_validator(mode="after")
    def job_and_lease_are_paired(self) -> "WorkerHeartbeat":
        if (self.current_job_id is None) != (self.lease_token is None):
            raise ValueError("current_job_id and lease_token must be provided together")
        return self


class WorkerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(strict=True)


class WorkerCapacityUpdate(BaseModel):
    """Largest single container job this worker is allowed to accept."""

    model_config = ConfigDict(extra="forbid")

    max_job_cpu: float = Field(ge=0.1, le=8.0)
    max_job_memory_mb: int = Field(ge=256, le=16384)


class WorkerState(StrEnum):
    ONLINE = "ONLINE"
    BUSY = "BUSY"
    STALE = "STALE"


class WorkerRead(BaseModel):
    id: str
    enabled: bool
    supported_types: list[JobType]
    registered_at: datetime
    last_seen: datetime
    current_job_id: UUID | None = None
    max_job_cpu: float | None = None
    max_job_memory_mb: int | None = None
    metrics: WorkerMetrics | None = None
    state: WorkerState

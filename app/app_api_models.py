from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
RunStatus = Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"]


class PublicResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserResponse(PublicResponse):
    id: UUID
    display_name: str


class WorkloadResponse(PublicResponse):
    id: UUID
    release_sha256: Sha256
    workload_id: str
    inputs: list[str]


class WorkloadsResponse(PublicResponse):
    active_release_sha256: Sha256 | None
    items: list[WorkloadResponse]


class InputResponse(PublicResponse):
    id: UUID
    size_bytes: int = Field(gt=0)
    sha256: Sha256


class FileResponse(PublicResponse):
    name: str
    path: str
    kind: Literal["file"]
    size_bytes: int = Field(ge=0)


class DirectoryResponse(PublicResponse):
    name: str
    path: str
    kind: Literal["directory"]
    size_bytes: None


FileEntryResponse = Annotated[
    FileResponse | DirectoryResponse, Field(discriminator="kind")
]


class FilesResponse(PublicResponse):
    items: list[FileEntryResponse]


class TaskResponse(PublicResponse):
    index: int = Field(ge=0)
    status: RunStatus


class RunResponse(PublicResponse):
    id: UUID
    installation_id: UUID
    status: RunStatus
    tasks: list[TaskResponse]


class RunsResponse(PublicResponse):
    items: list[RunResponse]


class ResultResponse(PublicResponse):
    index: int = Field(ge=0)
    name: str
    size_bytes: int = Field(ge=0)
    sha256: Sha256
    download_url: str


class ResultsResponse(PublicResponse):
    outputs: list[ResultResponse]


class ErrorResponse(PublicResponse):
    detail: str

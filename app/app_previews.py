import re
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from pydantic import Field

from app.app_contracts import (
    Contract,
    ContractError,
    Identifier,
    Sha256,
    parse_document,
)
from app.database import Database

MAX_PREVIEW_ERROR = 1024
MAX_VERSION = 64


class PreviewResult(Contract):
    attempt_id: UUID
    app_id: Identifier
    manifest_sha256: Sha256
    bundle_sha256: Sha256
    state: Literal["installing", "ready", "failed", "retired"]
    image_id: str | None = None
    preview_url: str | None = None
    checks: dict[str, bool] = Field(default_factory=dict)
    error: Annotated[str, Field(max_length=MAX_PREVIEW_ERROR)] | None = None
    version: (
        Annotated[str, Field(min_length=1, max_length=MAX_VERSION, pattern=r"^[ -~]+$")]
        | None
    ) = None


CHECKS = frozenset(
    {
        "manifest",
        "image",
        "startup",
        "readiness",
        "identity",
        "direct_denial",
        "forged_proxy_denial",
    }
)


def https_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.port not in {None, 443}
        or any(c.isspace() for c in value)
    ):
        raise ContractError("Staging must use a configured HTTPS origin")
    return value


class PreviewService:
    def __init__(
        self, database: Database, client: httpx.AsyncClient, origin: str, token: str
    ) -> None:
        self.database = database
        self.client = client
        self.origin = https_origin(origin)
        if len(token) < 32:
            raise ContractError(
                "Staging credential must contain at least 32 characters"
            )
        self.token = token

    async def preview(self, app_id: str, manifest_sha256: str) -> PreviewResult:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            release = connection.execute(
                "SELECT artifact_sha256 FROM app_eligible_releases "
                "WHERE app_id = ? AND manifest_sha256 = ?",
                (app_id, manifest_sha256),
            ).fetchone()
            if release is None:
                raise ContractError("Preview requires a workflow-verified release")
            previous = connection.execute(
                "SELECT * FROM app_preview_attempts WHERE app_id = ? "
                "AND state != 'retired'",
                (app_id,),
            ).fetchone()
            if previous is not None:
                if previous["manifest_sha256"] != manifest_sha256:
                    raise ContractError(
                        "Retire the existing preview before selecting another release"
                    )
                attempt = UUID(previous["attempt_id"])
            else:
                attempt = uuid4()
                connection.execute(
                    "INSERT INTO app_preview_attempts (attempt_id, "
                    "app_id, manifest_sha256, bundle_sha256, state) "
                    "VALUES (?, ?, ?, ?, 'installing')",
                    (str(attempt), app_id, manifest_sha256, release[0]),
                )
        return await self._exchange(attempt, "PUT")

    async def status(self, attempt: UUID) -> PreviewResult:
        return await self._exchange(attempt, "GET")

    async def retire(self, attempt: UUID) -> PreviewResult:
        return await self._exchange(attempt, "DELETE")

    async def _exchange(self, attempt: UUID, method: str) -> PreviewResult:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM app_preview_attempts WHERE attempt_id = ?",
                (str(attempt),),
            ).fetchone()
            if row is None:
                raise ContractError("Preview attempt does not exist")
            archive = connection.execute(
                "SELECT archive FROM app_eligible_releases WHERE "
                "app_id = ? AND manifest_sha256 = ?",
                (row["app_id"], row["manifest_sha256"]),
            ).fetchone()[0]
        if row["state"] == "retired":
            return parse_document(PreviewResult, row["result_json"].encode())
        try:
            async with self.client.stream(
                method,
                self.origin + "/control/attempts/" + str(attempt),
                content=bytes(archive) if method == "PUT" else None,
                headers={
                    "Authorization": "Bearer " + self.token,
                    "X-App-ID": row["app_id"],
                    "X-Release-SHA256": row["manifest_sha256"],
                    "X-Bundle-SHA256": row["bundle_sha256"],
                    "Content-Type": "application/zip",
                },
                follow_redirects=False,
                timeout=45,
            ) as response:
                payload = bytearray()
                if (
                    method == "DELETE"
                    and response.status_code == 404
                    and row["result_json"] is None
                ):
                    # Staging refused this attempt before journaling it, so
                    # nothing remains there to retire.
                    retired = PreviewResult(
                        attempt_id=attempt,
                        app_id=row["app_id"],
                        manifest_sha256=row["manifest_sha256"],
                        bundle_sha256=row["bundle_sha256"],
                        state="retired",
                    )
                    payload.extend(retired.model_dump_json().encode())
                elif response.status_code != 200:
                    raise ContractError(
                        "Staging did not confirm this attempt; "
                        "reconcile before replacement"
                    )
                else:
                    async for chunk in response.aiter_bytes():
                        payload.extend(chunk)
                        if len(payload) > 65536:
                            raise ContractError(
                                "Staging response exceeds the byte limit"
                            )
            result = parse_document(PreviewResult, bytes(payload))
            if (
                result.attempt_id != attempt
                or result.app_id != row["app_id"]
                or result.manifest_sha256 != row["manifest_sha256"]
                or result.bundle_sha256 != row["bundle_sha256"]
            ):
                raise ContractError(
                    "Staging response does not match the pinned attempt"
                )
            expected_url = f"{self.origin}/previews/{attempt}/{result.app_id}/"
            if result.state == "ready" and (
                result.preview_url != expected_url
                or set(result.checks) != CHECKS
                or not all(result.checks.values())
                or result.image_id is None
                or re.fullmatch(r"sha256:[0-9a-f]{64}", result.image_id) is None
            ):
                raise ContractError("Staging readiness evidence is incomplete")
            if method == "DELETE" and result.state != "retired":
                raise ContractError("Staging has not confirmed retirement")
        except httpx.HTTPError:
            raise ContractError(
                "Staging outcome is uncertain; reconcile this attempt"
            ) from None
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE app_preview_attempts SET state = ?, "
                "result_json = ? WHERE attempt_id = ?",
                (result.state, result.model_dump_json(), str(attempt)),
            )
        return result

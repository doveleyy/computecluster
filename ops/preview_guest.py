"""Authenticated preview installer. Run only inside the dedicated guest."""

import argparse
import asyncio
import fcntl
import hmac
import json
import sqlite3
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Annotated, Any, cast
from uuid import UUID

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from starlette.responses import JSONResponse, Response

from app.app_contracts import (
    ContractError,
    PublishedSource,
    ReleaseManifest,
    canonical_path,
    parse_document,
)
from app.app_gateway import screen_request
from app.app_previews import PreviewResult
from app.app_publication import (
    MAX_PACKAGE_BYTES,
    MAX_STORE_BYTES,
    Publication,
    VerifiedPublication,
    sha256,
    unpack,
    verify_package,
)
from ops.preview_checker import CheckerRejected
from ops.preview_runtime import GuestConfig, GuestRuntime


class GuestJournal:
    def __init__(self, path: Path) -> None:
        self.path = path
        with self.connect() as connection:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS attempts (sequence INTEGER "
                "PRIMARY KEY AUTOINCREMENT, "
                "attempt_id TEXT UNIQUE NOT NULL, app_id TEXT NOT "
                "NULL, manifest_sha256 TEXT NOT NULL, "
                "bundle_sha256 TEXT NOT NULL, state TEXT NOT NULL, "
                "result TEXT NOT NULL, archive BLOB NOT NULL);"
                "CREATE UNIQUE INDEX IF NOT EXISTS active_app ON "
                "attempts(app_id) WHERE state != 'retired';"
                "CREATE UNIQUE INDEX IF NOT EXISTS active_install ON "
                "attempts(state) WHERE state = 'installing';"
                "CREATE TRIGGER IF NOT EXISTS immutable_attempt BEFORE "
                "UPDATE OF attempt_id, app_id, "
                "manifest_sha256, bundle_sha256, archive ON attempts BEGIN "
                "SELECT RAISE(ABORT, 'attempt identity is immutable'); END;"
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def row(self, attempt: UUID) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (str(attempt),)
            ).fetchone()
        if row is None:
            raise HTTPException(404, "Attempt not found")
        return cast(sqlite3.Row, row)

    def get(self, attempt: UUID) -> PreviewResult:
        return parse_document(PreviewResult, self.row(attempt)["result"].encode())

    def admit(
        self, initial: PreviewResult, archive: bytes
    ) -> tuple[PreviewResult, bool]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?",
                (str(initial.attempt_id),),
            ).fetchone()
            if row is not None:
                if (row["app_id"], row["manifest_sha256"], row["bundle_sha256"]) != (
                    initial.app_id,
                    initial.manifest_sha256,
                    initial.bundle_sha256,
                ):
                    raise HTTPException(409, "Attempt identity cannot change")
                return parse_document(PreviewResult, row["result"].encode()), False
            live = connection.execute(
                "SELECT count(*) FROM attempts WHERE state IN ('installing', 'ready')"
            ).fetchone()[0]
            if live >= 2:
                raise HTTPException(
                    409, "Both guest preview resource slots are occupied"
                )
            used, count = connection.execute(
                "SELECT coalesce(sum(length(archive)),0), count(*) FROM attempts"
            ).fetchone()
            if used + len(archive) > MAX_STORE_BYTES or count >= 1000:
                raise HTTPException(507, "Guest preview retention is full")
            try:
                connection.execute(
                    "INSERT INTO attempts (attempt_id, app_id, "
                    "manifest_sha256, bundle_sha256, state, result, archive) "
                    "VALUES (?, ?, ?, ?, 'installing', ?, ?)",
                    (
                        str(initial.attempt_id),
                        initial.app_id,
                        initial.manifest_sha256,
                        initial.bundle_sha256,
                        initial.model_dump_json(),
                        archive,
                    ),
                )
            except sqlite3.IntegrityError:
                raise HTTPException(
                    409, "An app preview or global installation slot is occupied"
                ) from None
        return initial, True

    def save(self, result: PreviewResult) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE attempts SET state = ?, result = ? WHERE attempt_id = ?",
                (result.state, result.model_dump_json(), str(result.attempt_id)),
            )

    def ready(self, app_id: str) -> UUID | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT attempt_id FROM attempts WHERE app_id = ? AND state = 'ready'",
                (app_id,),
            ).fetchone()
        return None if row is None else UUID(row[0])

    def all(self) -> list[PreviewResult]:
        with self.connect() as connection:
            rows = connection.execute("SELECT result FROM attempts").fetchall()
        return [parse_document(PreviewResult, row[0].encode()) for row in rows]


def bundle(initial: PreviewResult, archive: bytes) -> VerifiedPublication:
    if sha256(archive) != initial.bundle_sha256:
        raise ContractError("Uploaded bundle hash does not match attempt")
    files = unpack(archive)
    try:
        proof = parse_document(Publication, files["publication.json"])
        manifest = parse_document(ReleaseManifest, files["manifest.json"])
    except KeyError:
        raise ContractError("Uploaded bundle is missing metadata") from None
    source = PublishedSource(
        kind="published",
        repository=proof.repository,
        branch=proof.branch,
        workflow=proof.workflow,
        image_repository=manifest.image.split("@", 1)[0],
    )
    result = verify_package(archive, source, 1, initial.bundle_sha256)
    if (
        manifest.app_id != initial.app_id
        or proof.manifest_sha256 != initial.manifest_sha256
    ):
        raise ContractError("Uploaded bundle does not match selected app and release")
    return result


def create_guest(config: GuestConfig) -> FastAPI:
    runtime = GuestRuntime(config)
    journal = GuestJournal(runtime.root / "guest.db")
    token = Path(config.control_token_file).read_text().strip()
    if len(token) < 32:
        raise ContractError(
            "Guest control credential must contain at least 32 characters"
        )
    tasks: set[asyncio.Task[None]] = set()

    def fail(result: PreviewResult, cause: Exception | None = None) -> None:
        try:
            runtime.cleanup(result.attempt_id)
        except (ContractError, OSError, subprocess.TimeoutExpired):
            journal.save(
                result.model_copy(
                    update={
                        "state": "installing",
                        "preview_url": None,
                        "error": "Guest cleanup is uncertain; reconcile this attempt",
                    }
                )
            )
            return
        journal.save(
            result.model_copy(
                update={
                    "state": "failed",
                    "error": str(cause)
                    if isinstance(cause, CheckerRejected)
                    else "Guest installation or reconciliation failed",
                    "preview_url": None,
                }
            )
        )

    def reconcile() -> None:
        known = {item.attempt_id: item for item in journal.all()}
        for attempt in runtime.owned_attempts() - known.keys():
            runtime.cleanup(attempt)
        for result in known.values():
            if result.state == "ready":
                try:
                    release = bundle(
                        result, bytes(journal.row(result.attempt_id)["archive"])
                    )
                    journal.save(
                        runtime.verify(
                            result,
                            release,
                            result.image_id or "",
                        )
                    )
                except (
                    ValueError,
                    OSError,
                    httpx.HTTPError,
                    subprocess.TimeoutExpired,
                ) as error:
                    fail(result, error)
            elif result.state == "installing":
                fail(result)
            else:
                runtime.cleanup(result.attempt_id)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        with (runtime.root / "installer.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            await asyncio.to_thread(reconcile)
            yield
            if tasks:
                await asyncio.gather(*tasks)

    app = FastAPI(lifespan=lifespan, docs_url=None, openapi_url=None)
    app.state.journal = journal
    app.state.runtime = runtime

    def authorize(request: Request) -> None:
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied, "Bearer " + token):
            raise HTTPException(401, "Staging control credential required")

    def identity(request: Request, attempt: UUID) -> PreviewResult:
        try:
            return parse_document(
                PreviewResult,
                json.dumps(
                    {
                        "attempt_id": str(attempt),
                        "app_id": request.headers.get("X-App-ID"),
                        "manifest_sha256": request.headers.get("X-Release-SHA256"),
                        "bundle_sha256": request.headers.get("X-Bundle-SHA256"),
                        "state": "installing",
                    }
                ).encode(),
            )
        except ContractError:
            raise HTTPException(400, "Invalid attempt identity") from None

    def matching(request: Request, attempt: UUID) -> PreviewResult:
        incoming = identity(request, attempt)
        current = journal.get(attempt)
        if (incoming.app_id, incoming.manifest_sha256, incoming.bundle_sha256) != (
            current.app_id,
            current.manifest_sha256,
            current.bundle_sha256,
        ):
            raise HTTPException(409, "Attempt identity does not match")
        return current

    async def install(initial: PreviewResult, archive: bytes) -> None:
        try:
            result = await asyncio.to_thread(
                runtime.install,
                initial,
                bundle(initial, archive),
            )
            journal.save(result)
        except Exception as error:
            # A dead install task would otherwise hold the global install slot.
            await asyncio.to_thread(fail, initial, error)

    @app.put("/control/attempts/{attempt}")
    async def put_attempt(attempt: UUID, request: Request) -> JSONResponse:
        authorize(request)
        initial = identity(request, attempt)
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > MAX_PACKAGE_BYTES:
                raise HTTPException(413, "Preview bundle exceeds the byte limit")
        try:
            bundle(initial, bytes(payload))
        except (ContractError, ValueError):
            raise HTTPException(
                400, "Preview bundle does not match its declaration"
            ) from None
        current, admitted = journal.admit(initial, bytes(payload))
        if admitted:
            task = asyncio.create_task(install(initial, bytes(payload)))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        return JSONResponse(current.model_dump(mode="json"))

    @app.get("/control/attempts/{attempt}")
    async def get_attempt(attempt: UUID, request: Request) -> JSONResponse:
        authorize(request)
        return JSONResponse(matching(request, attempt).model_dump(mode="json"))

    @app.delete("/control/attempts/{attempt}")
    async def retire_attempt(attempt: UUID, request: Request) -> JSONResponse:
        authorize(request)
        current = matching(request, attempt)
        if current.state == "installing":
            raise HTTPException(409, "Wait for the active installation to finish")
        await asyncio.to_thread(runtime.cleanup, attempt)
        result = current.model_copy(update={"state": "retired", "preview_url": None})
        journal.save(result)
        return JSONResponse(result.model_dump(mode="json"))

    async def forward(
        request: Request, attempt: UUID, path: str, params: Mapping[str, str]
    ) -> Response:
        if (
            request.headers.get("Tailscale-User-Login", "").lower()
            != config.fixture_login.lower()
        ):
            raise HTTPException(401, "Linked preview identity required")
        headers = [
            (key, value)
            for key, value in screen_request(request.headers.raw)
            if key.lower() not in {b"x-preview-bridge", b"accept-encoding"}
        ]
        # This preview hop decodes the upstream response with httpx, which here
        # has neither brotli nor zstandard installed. Forwarding the browser's
        # own Accept-Encoding would let the app answer with br/zstd and hand the
        # browser undecoded bytes once content-encoding is stripped below. The
        # production gateway streams raw bytes instead and is unaffected.
        headers.append((b"accept-encoding", b"gzip, identity"))
        headers.append((b"tailscale-user-login", config.fixture_login.encode()))
        headers.append(
            (
                b"x-preview-bridge",
                (runtime.root / str(attempt) / "core/bridge-token").read_bytes(),
            )
        )
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > MAX_PACKAGE_BYTES:
                raise HTTPException(413, "Preview request exceeds the byte limit")
        async with httpx.AsyncClient(trust_env=False) as client:
            try:
                endpoint = await asyncio.to_thread(
                    runtime.endpoint, attempt, "core", 8001
                )
            except ContractError:
                raise HTTPException(503, "Preview ingress is unavailable") from None
            response = await client.request(
                request.method,
                endpoint + path,
                params=params,
                headers=headers,
                content=bytes(payload),
                timeout=30,
            )
        # httpx has decoded the body, so keep the Content-Length Starlette
        # computed from it rather than the upstream's, which counts encoded bytes.
        result = Response(response.content, response.status_code)
        result.raw_headers += [
            (name, value)
            for name, value in response.headers.raw
            if name.lower()
            not in {
                b"transfer-encoding",
                b"connection",
                b"content-encoding",
                b"content-length",
            }
        ]
        return result

    @app.api_route(
        "/previews/{attempt}/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def browser(attempt: UUID, path: str, request: Request) -> Response:
        current = journal.get(attempt)
        if current.state != "ready":
            raise HTTPException(409, "Preview is not ready")
        try:
            canonical_path("/" + path.rstrip("/"))
            if not path.startswith(current.app_id + "/"):
                raise ValueError
        except ValueError:
            raise HTTPException(
                400, "Preview path is outside the selected application"
            ) from None
        return await forward(
            request, attempt, f"/previews/{attempt}/{path}", request.query_params
        )

    @app.api_route("/platform/bar", methods=["GET", "HEAD"])
    async def platform_bar(
        app_id: Annotated[str, Query(alias="app")], request: Request
    ) -> Response:
        attempt = journal.ready(app_id)
        if attempt is None:
            raise HTTPException(404, "No ready preview for this application")
        return await forward(request, attempt, "/platform/bar", {"app": app_id})

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = parse_document(GuestConfig, args.config.read_bytes())
    uvicorn.run(
        create_guest(config),
        host="127.0.0.1",
        port=config.port,
        proxy_headers=False,
        access_log=False,
    )


if __name__ == "__main__":
    main()

"""Guest-only core entrypoint with separate app and trusted ingress peers."""

import asyncio
import hmac
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from starlette.responses import Response


async def bridge(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(
            os.environ["PREVIEW_APP_HOST"], int(os.environ["PREVIEW_APP_PORT"])
        )

        async def copy(
            source: asyncio.StreamReader, destination: asyncio.StreamWriter
        ) -> None:
            try:
                while data := await source.read(65536):
                    destination.write(data)
                    await destination.drain()
            finally:
                destination.close()

        await asyncio.gather(
            copy(reader, upstream_writer), copy(upstream_reader, writer)
        )
    except OSError:
        writer.close()


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    server = await asyncio.start_server(bridge, "127.0.0.1", 19001)
    app.state.client = httpx.AsyncClient(trust_env=False)
    try:
        yield
    finally:
        server.close()
        await server.wait_closed()
        await app.state.client.aclose()


ingress = FastAPI(lifespan=lifespan, docs_url=None, openapi_url=None)


@ingress.api_route(
    "/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
)
async def proxy(request: Request, path: str) -> Response:
    expected = Path(os.environ["PREVIEW_BRIDGE_TOKEN_FILE"]).read_text().strip()
    if not hmac.compare_digest(request.headers.get("X-Preview-Bridge", ""), expected):
        return Response(status_code=401)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 32 * 1024**2:
            return Response(status_code=413)
    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower()
        not in {"host", "content-length", "connection", "x-preview-bridge"}
        and not name.lower().startswith("x-forwarded-")
    }
    headers["X-Forwarded-Proto"] = "https"
    response = await request.app.state.client.request(
        request.method,
        "http://127.0.0.1:8000/" + path,
        params=request.query_params,
        headers=headers,
        content=bytes(body),
        timeout=30,
    )
    # httpx has decoded the body, so keep the Content-Length Starlette computed
    # from it rather than the upstream's, which counts the encoded bytes.
    outgoing = Response(response.content, response.status_code)
    outgoing.raw_headers += [
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
    return outgoing


async def main() -> None:
    core = uvicorn.Server(
        uvicorn.Config(
            "app.main:app",
            host="0.0.0.0",
            port=8000,
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1",
            access_log=False,
        )
    )
    proxy_server = uvicorn.Server(
        uvicorn.Config(
            ingress,
            host="0.0.0.0",
            port=8001,
            proxy_headers=False,
            access_log=False,
        )
    )
    await asyncio.gather(core.serve(), proxy_server.serve())


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        asyncio.run(main())

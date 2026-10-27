"""Checks a request passes before any route or application reads its body."""

import os
import re
from collections.abc import Callable
from secrets import compare_digest

import anyio
from fastapi import HTTPException, status
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings
from app.dashboard_auth import SESSION_COOKIE, session_identity
from app.dashboard_uploads import DATASET, INPUT, PROJECT, SCRIPT

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MULTIPART_ENVELOPE_BYTES = 64 * 1024
UPLOAD_LIMITS: dict[str, Callable[[Settings], int]] = {
    "/uploads/datasets": DATASET.max_bytes,
    "/jobs-ui/api/uploads": DATASET.max_bytes,
    "/uploads/scripts": SCRIPT.max_bytes,
    "/jobs-ui/api/script-uploads": SCRIPT.max_bytes,
    "/uploads/projects": PROJECT.max_bytes,
    "/jobs-ui/api/project-uploads": PROJECT.max_bytes,
    "/uploads/inputs": INPUT.max_bytes,
    "/jobs-ui/api/input-uploads": INPUT.max_bytes,
    "/jobs-ui/api/workspace/uploads": (
        lambda settings: settings.max_workspace_upload_bytes
    ),
}
ARTIFACT_UPLOAD = re.compile(r"/jobs/[^/]+/artifacts")


def cross_origin_write(request: Request) -> bool:
    """The Tailscale identity header and the SameSite cookie both reach the
    core from any page in the tailnet's one ts.net site, so only the browser's
    Origin shows a write came from the platform's own pages. Browsers name it
    on every write; workers and scripts send none and hold no ambient
    credential."""
    if request.method in SAFE_METHODS:
        return False
    allowed = {
        request.url.scheme + "://" + request.url.netloc,
        os.environ.get("HOME_PLATFORM_APPS_ORIGIN"),
    }
    return any(origin not in allowed for origin in request.headers.getlist("origin"))


def upload_limit(path: str) -> Callable[[Settings], int] | None:
    if ARTIFACT_UPLOAD.fullmatch(path):
        return lambda settings: settings.max_artifact_bytes
    return UPLOAD_LIMITS.get(path)


def credentialed(request: Request) -> bool:
    """Any platform credential. Each route still checks its own authority, but
    FastAPI parses a multipart body before running those dependencies."""
    expected = request.app.state.settings.api_token
    supplied = request.headers.get("x-api-token")
    if expected is None or (
        supplied is not None and compare_digest(supplied.encode(), expected.encode())
    ):
        return True
    return (
        session_identity(
            request,
            request.app.state.account_store,
            request.cookies.get(SESSION_COOKIE),
        )
        is not None
    )


def bounded(receive: Receive, limit: int) -> Receive:
    received = 0

    async def receive_bounded() -> Message:
        nonlocal received
        message = await receive()
        received += len(message.get("body", b""))
        if received > limit:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Upload exceeds the byte limit",
            )
        return message

    return receive_bounded


class RequestGuard:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if cross_origin_write(request):
            await JSONResponse(
                {"detail": "Same-origin request required"},
                status.HTTP_403_FORBIDDEN,
            )(scope, receive, send)
            return
        limit = upload_limit(scope["path"]) if request.method == "POST" else None
        if limit is None:
            await self.app(scope, receive, send)
            return
        if not await anyio.to_thread.run_sync(credentialed, request):
            await JSONResponse(
                {"detail": "Authentication required before upload"},
                status.HTTP_401_UNAUTHORIZED,
            )(scope, receive, send)
            return
        ceiling = limit(scope["app"].state.settings) + MULTIPART_ENVELOPE_BYTES
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > ceiling:
            await JSONResponse(
                {"detail": "Upload exceeds the byte limit"},
                status.HTTP_413_CONTENT_TOO_LARGE,
            )(scope, receive, send)
            return
        await self.app(scope, bounded(receive, ceiling), send)

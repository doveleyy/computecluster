import re
import sqlite3
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from functools import partial
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from urllib.parse import unquote, urlsplit

import anyio
import httpx
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, RedirectResponse, StreamingResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.app_identity import CONTEXT_HEADER, AppIdentity, Refusal
from app.app_registry import (
    CORE_ROUTE_TREES,
    AppRegistry,
    ManagedApp,
    installs_required,
)
from app.dashboard_auth import PLATFORM_COOKIE_PREFIX

UPSTREAM_TIMEOUT = httpx.Timeout(connect=3.0, read=120.0, write=30.0, pool=5.0)
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-connection",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
RESERVED_HEADERS = frozenset(
    {
        "host",
        "forwarded",
        "x-real-ip",
        "content-length",
        "cookie",
        "expect",
        "x-api-token",
        "x-service-identity-token",
    }
)
RESERVED_HEADER_PREFIXES = ("tailscale-", "x-platform-", "x-forwarded-")


class _NoCookies(DefaultCookiePolicy):
    def set_ok(self, cookie: Cookie, request: object) -> bool:
        return False


def create_upstream_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=UPSTREAM_TIMEOUT,
        follow_redirects=False,
        trust_env=False,
        cookies=CookieJar(policy=_NoCookies()),
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
    )


@dataclass(frozen=True)
class Forward:
    app: ManagedApp
    internal_path: bytes
    assertion: str


@dataclass(frozen=True)
class UpstreamFailure:
    status: int
    detail: str


@dataclass(frozen=True)
class Framing:
    """How the client delimited its request body: chunked, fixed, or absent."""

    chunked: bool
    length: int | None


class BodyMismatch(Exception):
    pass


def request_framing(headers: Sequence[tuple[bytes, bytes]]) -> Framing:
    lengths = set()
    chunked = False
    for name, value in headers:
        if name.lower() == b"transfer-encoding":
            chunked = True
        elif name.lower() == b"content-length":
            lengths.add(value.strip())
    # RFC 7230 3.3.3: a chunked body makes Content-Length meaningless, and the
    # server already framed the body that way, so chunked wins here too.
    if chunked:
        return Framing(True, None)
    if not lengths:
        return Framing(False, None)
    if len(lengths) != 1 or not next(iter(lengths)).isdigit():
        raise ValueError("Content-Length is not a single decimal number")
    return Framing(False, int(next(iter(lengths))))


async def fixed_length(
    stream: AsyncIterator[bytes], length: int
) -> AsyncIterator[bytes]:
    sent = 0
    async for chunk in stream:
        sent += len(chunk)
        if sent > length:
            raise BodyMismatch
        yield chunk
    if sent != length:
        raise BodyMismatch


def core_path(path: str) -> bool:
    return path == "/" or any(
        path == tree or path.startswith(tree + "/") for tree in CORE_ROUTE_TREES
    )


def reserved_cookie(name: str) -> bool:
    name = name.lower()
    for prefix in ("__host-", "__secure-"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
    return name.startswith(PLATFORM_COOKIE_PREFIX)


def connection_headers(headers: Sequence[tuple[str, str]]) -> set[str]:
    return {
        part.strip().lower()
        for name, value in headers
        if name.lower() == "connection"
        for part in value.split(",")
    }


def screen_request(
    headers: Sequence[tuple[bytes, bytes]],
) -> list[tuple[bytes, bytes]]:
    blocked = HOP_BY_HOP | connection_headers(
        [(key.decode("latin-1"), value.decode("latin-1")) for key, value in headers]
    )
    result = []
    cookies: list[bytes] = []
    for key, value in headers:
        name = key.decode("latin-1").lower()
        if name == "cookie":
            cookies.extend(
                part.strip()
                for part in value.split(b";")
                if b"=" in part
                and not reserved_cookie(
                    part.split(b"=", 1)[0].strip().decode("latin-1")
                )
            )
        elif (
            name not in blocked
            and name not in RESERVED_HEADERS
            and not name.startswith(RESERVED_HEADER_PREFIXES)
        ):
            result.append((key, value))
    if cookies and "cookie" not in blocked:
        result.append((b"cookie", b"; ".join(cookies)))
    return result


def forward_headers(
    headers: Sequence[tuple[bytes, bytes]], assertion: str
) -> list[tuple[bytes, bytes]]:
    return [
        *screen_request(headers),
        (CONTEXT_HEADER.lower().encode(), assertion.encode()),
    ]


def cookie_violation(value: str, route: str) -> str | None:
    parts = [part.strip() for part in value.split(";")]
    name, separator, cookie_value = parts[0].partition("=")
    if len(cookie_value) >= 2 and cookie_value[0] == cookie_value[-1] == '"':
        cookie_value = cookie_value[1:-1]
    if (
        not separator
        or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
        or reserved_cookie(name)
        or any(c in cookie_value for c in ',;\\" \t\r\n')
        or any(ord(c) < 0x21 or ord(c) > 0x7E for c in cookie_value)
    ):
        return "Reserved or invalid application cookie"
    paths = []
    for part in parts[1:]:
        attribute, separator, attribute_value = part.partition("=")
        if attribute.strip().lower() == "domain":
            return "Application cookie must not set Domain"
        if attribute.strip().lower() == "path":
            if not separator:
                return "Invalid application cookie Path"
            paths.append(attribute_value.strip())
    if len(paths) != 1:
        return "Application cookie requires one explicit Path"
    path = paths[0]
    if (
        not (path == route or path.startswith(route + "/"))
        or any(character in path for character in '%?#\\"')
        or any(part in {".", ".."} for part in path.split("/"))
        or "//" in path
    ):
        return "Application cookie Path is outside its route"
    return None


def rewrite_location(value: str, app: ManagedApp) -> str:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
            and parsed.port == app.installation.host_port
            and parsed.username is None
        ):
            return (
                app.route
                + (parsed.path or "/")
                + ("?" + parsed.query if parsed.query else "")
                + ("#" + parsed.fragment if parsed.fragment else "")
            )
    except ValueError:
        pass
    return value


def screen_response(
    headers: Sequence[tuple[str, str]], app: ManagedApp
) -> list[tuple[str, str]] | UpstreamFailure:
    blocked = HOP_BY_HOP | connection_headers(headers)
    # The client de-chunks a chunked upstream body, so a Content-Length the
    # upstream sent alongside Transfer-Encoding no longer describes anything.
    if any(key.lower() == "transfer-encoding" for key, _ in headers):
        blocked = blocked | {"content-length"}
    result = []
    for key, value in headers:
        name = key.lower()
        if name == "clear-site-data":
            return UpstreamFailure(
                502, "Application response attempts to clear platform data"
            )
        if name == "set-cookie":
            violation = cookie_violation(value, app.route)
            if violation is not None:
                return UpstreamFailure(502, violation)
        # Apps share the platform origin, so a wider service-worker scope
        # would let one app's worker intercept other routes.
        if name not in blocked and name != "service-worker-allowed":
            result.append(
                (key, rewrite_location(value, app) if name == "location" else value)
            )
    return result


def internal_path(scope: Scope, route: str) -> bytes | None:
    raw: bytes = scope.get("raw_path", scope["path"].encode())
    if not raw.startswith(route.encode() + b"/"):
        return None
    suffix = raw[len(route) :]
    decoded = unquote(suffix.decode("latin-1"))
    if (
        "\\" in decoded
        or any(part in {".", ".."} for part in decoded.split("/"))
        or any(encoded in suffix.lower() for encoded in (b"%2f", b"%5c", b"%00"))
    ):
        return None
    return suffix


class AppGateway:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"} or core_path(scope["path"]):
            await self.app(scope, receive, send)
            return
        state = scope["app"].state
        registry: AppRegistry = state.app_registry
        app = await anyio.to_thread.run_sync(registry.managed_route, scope["path"])
        if app is None:
            await self.app(scope, receive, send)
            return
        refusal = await anyio.to_thread.run_sync(
            partial(
                registry.admission_refusal,
                app.app_id,
                require_installed=installs_required(),
            )
        )
        if refusal is not None:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse({"detail": refusal}, 503)(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        request = Request(scope, receive)
        identity: AppIdentity = state.app_identity
        try:
            assertion = await anyio.to_thread.run_sync(identity.admit, app, request)
        except sqlite3.Error:
            assertion = Refusal(503, "Application identity is unavailable")
        if isinstance(assertion, Refusal):
            await JSONResponse(
                {"detail": assertion.detail},
                assertion.status,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        if scope["path"] == app.route:
            if scope.get("raw_path", app.route.encode()) != app.route.encode():
                await JSONResponse({"detail": "Invalid application path"}, 400)(
                    scope, receive, send
                )
                return
            query = scope.get("query_string", b"")
            location = (
                app.route + "/" + ("?" + query.decode("latin-1") if query else "")
            )
            await RedirectResponse(location, status_code=308)(scope, receive, send)
            return
        path = internal_path(scope, app.route)
        if path is None:
            await JSONResponse({"detail": "Invalid application path"}, 400)(
                scope, receive, send
            )
            return
        await proxy(
            state.gateway_client, request, Forward(app, path, assertion), receive, send
        )


async def proxy(
    client: httpx.AsyncClient,
    request: Request,
    forward: Forward,
    receive: Receive,
    send: Send,
) -> None:
    query = request.scope.get("query_string", b"")
    url = httpx.URL(
        scheme="http",
        host="127.0.0.1",
        port=forward.app.installation.host_port,
        raw_path=forward.internal_path + (b"?" + query if query else b""),
    )
    try:
        framing = request_framing(request.scope["headers"])
    except ValueError:
        await JSONResponse({"detail": "Invalid Content-Length"}, 400)(
            request.scope, receive, send
        )
        return
    forwarded = forward_headers(request.scope["headers"], forward.assertion)
    content: AsyncIterator[bytes] | None = None
    if framing.chunked:
        content = request.stream()
    elif framing.length is not None:
        forwarded.append((b"content-length", str(framing.length).encode()))
        content = fixed_length(request.stream(), framing.length)
    outgoing = httpx.Request(request.method, url, headers=forwarded, content=content)
    try:
        response = await client.send(outgoing, stream=True)
    except ClientDisconnect:
        return
    except BodyMismatch:
        await JSONResponse(
            {"detail": "Request body does not match Content-Length"}, 400
        )(request.scope, receive, send)
        return
    except httpx.TimeoutException:
        await JSONResponse({"detail": "Application upstream timed out"}, 504)(
            request.scope, receive, send
        )
        return
    except httpx.HTTPError:
        await JSONResponse({"detail": "Application upstream failed"}, 502)(
            request.scope, receive, send
        )
        return
    try:
        headers = screen_response(response.headers.multi_items(), forward.app)
        if isinstance(headers, UpstreamFailure):
            await JSONResponse({"detail": headers.detail}, headers.status)(
                request.scope, receive, send
            )
            return
        stream = StreamingResponse(
            response.aiter_raw(), status_code=response.status_code
        )
        stream.raw_headers = [
            (name.encode("latin-1"), value.encode("latin-1")) for name, value in headers
        ]
        await stream(request.scope, receive, send)
    finally:
        await response.aclose()

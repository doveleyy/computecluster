"""The HTML shells, the tailnet launcher page, the platform bar, and its pins."""

from html import escape
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.accounts import AccountStore, SessionIdentity
from app.app_registry import AppNotServed, AppRegistry
from app.dashboard_auth import (
    SESSION_COOKIE,
    AccountStoreDependency,
    same_origin,
    session_identity,
    tailscale_request_identity,
)
from app.platform_apps import (
    BUILTIN_KEYS,
    render_bar,
    render_launcher_tiles,
    viewer_role,
)

DASHBOARD_HTML = Path(__file__).with_name("dashboard.html").read_text()
JOBS_HTML = Path(__file__).with_name("jobs.html").read_text()
HOME_HTML = Path(__file__).with_name("home.html").read_text()

SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]
TailscaleLogin = Annotated[str | None, Header(alias="Tailscale-User-Login")]

router = APIRouter()


def operator_page(
    request: Request,
    account_store: AccountStoreDependency,
    session: str | None,
    html: str,
    member_destination: str,
) -> Response:
    """A dashboard page for the administrator; a member session is sent to its
    own Job Desk equivalent instead of an owner-token form it cannot use."""
    identity = session_identity(request, account_store, session)
    if identity is not None and not identity.is_admin:
        return RedirectResponse(
            member_destination, status_code=status.HTTP_303_SEE_OTHER
        )
    return HTMLResponse(html.replace("__PLATFORM_APP__", "dashboard"))


@router.get("/dashboard")
def dashboard(
    request: Request,
    account_store: AccountStoreDependency,
    session: SessionCookie = None,
) -> Response:
    return operator_page(request, account_store, session, DASHBOARD_HTML, "/jobs-ui")


@router.get("/dashboard/operations")
def dashboard_operations(
    request: Request,
    account_store: AccountStoreDependency,
    session: SessionCookie = None,
) -> Response:
    return operator_page(request, account_store, session, DASHBOARD_HTML, "/jobs-ui")


@router.get("/dashboard/jobs")
def dashboard_jobs_page(
    request: Request,
    account_store: AccountStoreDependency,
    session: SessionCookie = None,
) -> Response:
    return operator_page(request, account_store, session, JOBS_HTML, "/jobs-ui")


@router.get("/dashboard/files")
def dashboard_files_page(
    request: Request,
    account_store: AccountStoreDependency,
    session: SessionCookie = None,
) -> Response:
    return operator_page(request, account_store, session, JOBS_HTML, "/jobs-ui/files")


JOB_DESK_HTML = JOBS_HTML.replace("__PLATFORM_APP__", "jobs")


@router.get("/jobs-ui", response_class=HTMLResponse)
def jobs_page() -> str:
    return JOB_DESK_HTML


@router.get("/jobs-ui/new", response_class=HTMLResponse)
def jobs_submit_page() -> str:
    return JOB_DESK_HTML


@router.get("/jobs-ui/files", response_class=HTMLResponse)
def jobs_files_page() -> str:
    return JOB_DESK_HTML


def linked_user(
    request: Request, account_store: AccountStore, login: str | None
) -> SessionIdentity | None:
    tailnet = tailscale_request_identity(request, login, None)
    if tailnet is None:
        return None
    return account_store.resolve_external_identity("tailscale", tailnet[0])


def require_linked_user(
    request: Request,
    account_store: AccountStoreDependency,
    login: TailscaleLogin = None,
) -> SessionIdentity:
    if tailscale_request_identity(request, login, None) is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Open Home Platform through its private Tailscale URL",
        )
    identity = linked_user(request, account_store, login)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Link this Tailscale identity from your Job Desk account first",
        )
    return identity


LinkedUser = Annotated[SessionIdentity, Depends(require_linked_user)]


def require_same_origin(request: Request) -> None:
    """The Tailscale header is ambient, so a pin change must also prove it was
    sent from the platform's own pages."""
    if not same_origin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Same-origin request required"
        )


@router.get("/platform/bar", response_class=HTMLResponse)
def platform_bar(
    request: Request,
    account_store: AccountStoreDependency,
    app: str,
    session: SessionCookie = None,
    tailscale_login: TailscaleLogin = None,
) -> HTMLResponse:
    """The cross-app navigation strip every app frames. The role comes from the
    dashboard cookie alone, so a visitor without one sees the member set; the
    registered apps are those the linked Tailscale user pinned, so a request
    without a linked user sees only the builtins."""
    registry: AppRegistry = request.app.state.app_registry
    role = viewer_role(request, account_store, session)
    viewer = linked_user(request, account_store, tailscale_login)
    pins = registry.pins(viewer.id) if viewer is not None else frozenset()
    apps = registry.navigation()
    if app not in {item.key for item in apps}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown platform app"
        )
    return HTMLResponse(
        render_bar(
            tuple(
                item
                for item in apps
                if role in item.roles and (item.key in BUILTIN_KEYS or item.key in pins)
            ),
            app,
        ),
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "frame-ancestors 'self'",
            "X-Frame-Options": "SAMEORIGIN",
        },
    )


@router.put(
    "/platform/pins/{app_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_same_origin)],
)
def pin_app(request: Request, app_id: str, identity: LinkedUser) -> None:
    try:
        request.app.state.app_registry.pin(identity.id, app_id)
    except AppNotServed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown platform app"
        ) from None


@router.delete(
    "/platform/pins/{app_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_same_origin)],
)
def unpin_app(request: Request, app_id: str, identity: LinkedUser) -> None:
    request.app.state.app_registry.unpin(identity.id, app_id)


@router.get("/", response_class=HTMLResponse)
def home_page(
    request: Request,
    account_store: AccountStoreDependency,
    identity: LinkedUser,
    session: SessionCookie = None,
) -> str:
    registry: AppRegistry = request.app.state.app_registry
    role = viewer_role(request, account_store, session)
    return HOME_HTML.replace(
        "__DISPLAY_NAME__", escape(identity.username, quote=True)
    ).replace(
        "__TILES__",
        render_launcher_tiles(registry.navigation(role), registry.pins(identity.id)),
    )

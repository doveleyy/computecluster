"""The HTML shells, the tailnet launcher page, and the platform bar."""

from html import escape
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Cookie, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.app_registry import AppRegistry
from app.dashboard_auth import (
    SESSION_COOKIE,
    AccountStoreDependency,
    session_identity,
    tailscale_request_identity,
)
from app.platform_apps import (
    render_bar,
    render_launcher_tiles,
    viewer_role,
)

DASHBOARD_HTML = Path(__file__).with_name("dashboard.html").read_text()
JOBS_HTML = Path(__file__).with_name("jobs.html").read_text()
HOME_HTML = Path(__file__).with_name("home.html").read_text()

SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]

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


@router.get("/platform/bar", response_class=HTMLResponse)
def platform_bar(
    request: Request,
    account_store: AccountStoreDependency,
    app: str,
    session: SessionCookie = None,
) -> HTMLResponse:
    """The cross-app navigation strip every app frames; the role comes from the
    dashboard cookie alone, so a visitor without one sees the member set."""
    registry: AppRegistry = request.app.state.app_registry
    role = viewer_role(request, account_store, session)
    apps = registry.navigation()
    if app not in {item.key for item in apps}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown platform app"
        )
    return HTMLResponse(
        render_bar(tuple(item for item in apps if role in item.roles), app),
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "frame-ancestors 'self'",
            "X-Frame-Options": "SAMEORIGIN",
        },
    )


@router.get("/", response_class=HTMLResponse)
def home_page(
    request: Request,
    account_store: AccountStoreDependency,
    tailscale_login: Annotated[str | None, Header(alias="Tailscale-User-Login")] = None,
    tailscale_name: Annotated[str | None, Header(alias="Tailscale-User-Name")] = None,
    session: SessionCookie = None,
) -> str:
    request_identity = tailscale_request_identity(
        request, tailscale_login, tailscale_name
    )
    if request_identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Open Home Platform through its private Tailscale URL",
        )
    identity = account_store.resolve_external_identity("tailscale", request_identity[0])
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Link this Tailscale identity from your Job Desk account first",
        )
    role = viewer_role(request, account_store, session)
    return HOME_HTML.replace(
        "__DISPLAY_NAME__", escape(identity.username, quote=True)
    ).replace(
        "__TILES__",
        render_launcher_tiles(request.app.state.app_registry.navigation(role)),
    )

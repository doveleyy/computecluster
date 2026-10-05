"""The HTML shells and the tailnet launcher page."""

from html import escape
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from app.dashboard_auth import AccountStoreDependency, tailscale_request_identity

DASHBOARD_HTML = Path(__file__).with_name("dashboard.html").read_text()
JOBS_HTML = Path(__file__).with_name("jobs.html").read_text()
HOME_HTML = Path(__file__).with_name("home.html").read_text()

router = APIRouter()


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> str:
    return DASHBOARD_HTML


@router.get("/dashboard/operations", response_class=HTMLResponse)
def dashboard_operations() -> str:
    return DASHBOARD_HTML


@router.get("/dashboard/jobs", response_class=HTMLResponse)
def dashboard_jobs_page() -> str:
    return JOBS_HTML


@router.get("/dashboard/files", response_class=HTMLResponse)
def dashboard_files_page() -> str:
    return JOBS_HTML


@router.get("/jobs-ui", response_class=HTMLResponse)
def jobs_page() -> str:
    return JOBS_HTML


@router.get("/jobs-ui/new", response_class=HTMLResponse)
def jobs_submit_page() -> str:
    return JOBS_HTML


@router.get("/jobs-ui/files", response_class=HTMLResponse)
def jobs_files_page() -> str:
    return JOBS_HTML


@router.get("/", response_class=HTMLResponse)
def home_page(
    request: Request,
    account_store: AccountStoreDependency,
    tailscale_login: Annotated[str | None, Header(alias="Tailscale-User-Login")] = None,
    tailscale_name: Annotated[str | None, Header(alias="Tailscale-User-Name")] = None,
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
    return HOME_HTML.replace("__DISPLAY_NAME__", escape(identity.username, quote=True))

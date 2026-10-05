"""Who is calling a dashboard route, and what that caller is provisioned for.

Session cookies, the admin/member split, the token headers, and the
per-member storage and Workspace entitlements all resolve here so every route
group shares one definition of each.
"""

import time
from pathlib import Path
from secrets import compare_digest
from typing import Annotated, cast

from fastapi import Cookie, Depends, Header, HTTPException, Request, status

from app.accounts import AccountStore, SessionIdentity, decode_session

SESSION_COOKIE = "home_platform_dashboard"


def session_secret(request: Request) -> str:
    return cast(
        str,
        request.app.state.settings.api_token or request.app.state.anonymous_session,
    )


def get_account_store(request: Request) -> AccountStore:
    return cast(AccountStore, request.app.state.account_store)


AccountStoreDependency = Annotated[AccountStore, Depends(get_account_store)]


def require_dashboard_session(
    request: Request,
    account_store: AccountStoreDependency,
    supplied: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> SessionIdentity:
    claims = (
        decode_session(supplied, session_secret(request), int(time.time()))
        if supplied is not None
        else None
    )
    identity = (
        account_store.get_identity(claims[0], claims[1]) if claims is not None else None
    )
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Dashboard login required",
        )
    return identity


def require_admin_session(
    identity: Annotated[SessionIdentity, Depends(require_dashboard_session)],
) -> SessionIdentity:
    if not identity.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required",
        )
    return identity


def require_member_session(
    identity: Annotated[SessionIdentity, Depends(require_dashboard_session)],
) -> SessionIdentity:
    if identity.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Member access required; use the operator dashboard",
        )
    return identity


def require_api_token(
    request: Request,
    supplied: Annotated[str | None, Header(alias="X-API-Token")] = None,
) -> None:
    expected = request.app.state.settings.api_token
    if expected is not None and (
        supplied is None or not compare_digest(supplied, expected)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API token",
        )


def require_service_identity_token(
    request: Request,
    supplied: Annotated[str | None, Header(alias="X-Service-Identity-Token")] = None,
) -> None:
    expected = request.app.state.settings.service_identity_token
    if expected is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service identity resolution is not configured",
        )
    if supplied is None or not compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid service identity token",
        )


DashboardSession = Annotated[SessionIdentity, Depends(require_dashboard_session)]
AdminSession = Annotated[SessionIdentity, Depends(require_admin_session)]
MemberSession = Annotated[SessionIdentity, Depends(require_member_session)]
ApiToken = Annotated[None, Depends(require_api_token)]
ServiceIdentityToken = Annotated[None, Depends(require_service_identity_token)]


def owner_scope(identity: SessionIdentity) -> str | None:
    return None if identity.is_admin else str(identity.id)


def tailscale_request_identity(
    request: Request,
    login: str | None,
    display_name: str | None,
) -> tuple[str, str] | None:
    if request.url.scheme != "https" or login is None:
        return None
    normalized_login = login.strip().lower()
    if not normalized_login:
        return None
    return normalized_login, (display_name or login).strip()


def member_storage_available(request: Request, identity: SessionIdentity) -> bool:
    settings = request.app.state.settings
    return bool(
        settings.member_storage_enabled
        or identity.id in settings.member_storage_user_ids
    )


def require_member_storage(request: Request, identity: SessionIdentity) -> None:
    if not member_storage_available(request, identity):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Personal NAS storage is not provisioned yet",
        )


def member_workspace_available(request: Request, identity: SessionIdentity) -> bool:
    settings = request.app.state.settings
    return bool(
        settings.workspace_directory is not None
        and (
            settings.member_workspace_enabled
            or identity.id in settings.member_workspace_user_ids
        )
    )


def require_member_workspace(request: Request, identity: SessionIdentity) -> Path:
    if not member_workspace_available(request, identity):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Job Desk workspace editing is not provisioned yet",
        )
    return cast(Path, request.app.state.settings.workspace_directory)


def require_administrator_workspace(request: Request) -> Path:
    root = request.app.state.settings.workspace_directory
    if root is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Administrator Workspace management is not provisioned",
        )
    return cast(Path, root)

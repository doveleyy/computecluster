"""Sessions, the caller's own account, and identity resolution for services."""

import time
from secrets import compare_digest
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, Response, status

from app.accounts import (
    DashboardLogin,
    ExternalIdentityConflictError,
    ExternalIdentityRead,
    ExternalIdentityStatus,
    InvalidCurrentPasswordError,
    PasswordChange,
    PortalSession,
    ServiceIdentityResolve,
    SessionIdentity,
    encode_session,
)
from app.dashboard_auth import (
    SESSION_COOKIE,
    AccountStoreDependency,
    DashboardSession,
    MemberSession,
    ServiceIdentityToken,
    member_storage_available,
    session_secret,
    tailscale_request_identity,
)
from app.identity import ADMIN_USER_ID

router = APIRouter()


@router.post("/dashboard/login", status_code=status.HTTP_204_NO_CONTENT)
def login(
    credentials: DashboardLogin,
    request: Request,
    response: Response,
    account_store: AccountStoreDependency,
) -> None:
    expected = request.app.state.settings.api_token
    identity: SessionIdentity | None = None
    if credentials.token is not None:
        valid = expected is None or compare_digest(credentials.token, expected)
        if valid:
            identity = account_store.get_identity(UUID(ADMIN_USER_ID))
    elif credentials.username is not None and credentials.password is not None:
        identity = account_store.authenticate(
            credentials.username.strip().lower(), credentials.password
        )
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )
    expires_at = int(time.time()) + 30 * 24 * 60 * 60
    response.set_cookie(
        SESSION_COOKIE,
        encode_session(
            identity,
            session_secret(request),
            expires_at,
            account_store.session_version(identity.id),
        ),
        httponly=True,
        samesite="strict",
        max_age=30 * 24 * 60 * 60,
        path="/",
    )


@router.post("/dashboard/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/jobs-ui/api/session", response_model=PortalSession)
def jobs_portal_session(request: Request, identity: DashboardSession) -> PortalSession:
    return PortalSession(
        **identity.model_dump(),
        storage_enabled=(
            identity.is_admin or member_storage_available(request, identity)
        ),
    )


@router.get(
    "/jobs-ui/api/account/identities/tailscale",
    response_model=ExternalIdentityStatus,
)
def tailscale_identity_status(
    request: Request,
    identity: MemberSession,
    account_store: AccountStoreDependency,
    tailscale_login: Annotated[str | None, Header(alias="Tailscale-User-Login")] = None,
    tailscale_name: Annotated[str | None, Header(alias="Tailscale-User-Name")] = None,
) -> ExternalIdentityStatus:
    request_identity = tailscale_request_identity(
        request, tailscale_login, tailscale_name
    )
    return ExternalIdentityStatus(
        linked=account_store.external_identity(identity.id, "tailscale"),
        request_subject=(request_identity[0] if request_identity else None),
        request_display_name=(request_identity[1] if request_identity else None),
    )


@router.post(
    "/jobs-ui/api/account/identities/tailscale",
    response_model=ExternalIdentityRead,
)
def link_tailscale_identity(
    request: Request,
    identity: MemberSession,
    account_store: AccountStoreDependency,
    tailscale_login: Annotated[str | None, Header(alias="Tailscale-User-Login")] = None,
    tailscale_name: Annotated[str | None, Header(alias="Tailscale-User-Name")] = None,
) -> ExternalIdentityRead:
    request_identity = tailscale_request_identity(
        request, tailscale_login, tailscale_name
    )
    if request_identity is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Open Job Desk through its private HTTPS Tailscale URL",
        )
    try:
        return account_store.link_external_identity(
            identity.id,
            "tailscale",
            request_identity[0],
            request_identity[1],
        )
    except ExternalIdentityConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This account or Tailscale identity is already linked",
        ) from error


@router.delete(
    "/jobs-ui/api/account/identities/tailscale",
    status_code=status.HTTP_204_NO_CONTENT,
)
def unlink_tailscale_identity(
    identity: MemberSession,
    account_store: AccountStoreDependency,
) -> Response:
    account_store.unlink_external_identity(identity.id, "tailscale")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/internal/service-identities/resolve",
    response_model=SessionIdentity,
)
def resolve_service_identity(
    resolution: ServiceIdentityResolve,
    account_store: AccountStoreDependency,
    _: ServiceIdentityToken,
) -> SessionIdentity:
    identity = account_store.resolve_external_identity(
        resolution.provider, resolution.subject
    )
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Linked identity not found",
        )
    return identity


@router.post(
    "/jobs-ui/api/account/password",
    status_code=status.HTTP_204_NO_CONTENT,
)
def jobs_portal_change_password(
    password: PasswordChange,
    response: Response,
    identity: MemberSession,
    account_store: AccountStoreDependency,
) -> None:
    try:
        account_store.change_password(
            identity.id, password.current_password, password.new_password
        )
    except InvalidCurrentPasswordError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        ) from error
    response.delete_cookie(SESSION_COOKIE, path="/")

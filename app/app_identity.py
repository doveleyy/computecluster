import base64
import binascii
import hashlib
import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import Field
from starlette.requests import Request

from app.accounts import AccountStore, SessionIdentity
from app.app_contracts import Contract, ContractError, parse_document
from app.app_registry import AppRegistry, ManagedApp
from app.config import AppPlatformSettings
from app.dashboard_auth import (
    SESSION_COOKIE,
    session_claims,
    tailscale_request_identity,
)

CONTEXT_HEADER = "X-Platform-User-Context"
CONTEXT_LIFETIME_SECONDS = 120
CLOCK_SKEW_SECONDS = 30
MAX_CONTEXT_BYTES = 2048
_PURPOSE = b"home-platform/app-user-context\x00"


@dataclass(frozen=True)
class Refusal:
    status: Literal[401, 403, 503]
    detail: str


@dataclass(frozen=True)
class AdministratorBinding:
    account_id: UUID
    session_version: int


@dataclass(frozen=True)
class AppContext:
    app: ManagedApp
    user: SessionIdentity
    administrator: AdministratorBinding | None


class _AdminClaim(Contract):
    account: UUID
    session_version: int = Field(ge=1)


class _Claims(Contract):
    app: str = Field(min_length=1, max_length=63)
    env: str = Field(min_length=1, max_length=63)
    user: UUID
    provider: Literal["tailscale"]
    subject: str = Field(min_length=1, max_length=320)
    iat: int
    exp: int
    admin: _AdminClaim | None = None


class AppIdentity:
    def __init__(
        self,
        accounts: AccountStore,
        registry: AppRegistry,
        platform: AppPlatformSettings | None,
        clock: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        self.accounts = accounts
        self.registry = registry
        self.platform = platform
        self.clock = clock

    def admit(self, app: ManagedApp, request: Request) -> str | Refusal:
        if self.platform is None:
            return Refusal(503, "Managed applications are not configured")
        linked = tailscale_request_identity(
            request,
            request.headers.get("Tailscale-User-Login"),
            request.headers.get("Tailscale-User-Name"),
        )
        if linked is None:
            return Refusal(401, "Trusted proxy identity required")
        user = self.accounts.resolve_external_identity("tailscale", linked[0])
        if user is None:
            return Refusal(403, "Enabled linked account required")
        admin = None
        if app.registration.access == "administrator":
            claims = session_claims(request, request.cookies.get(SESSION_COOKIE))
            administrator = (
                self.accounts.get_identity(*claims) if claims is not None else None
            )
            if administrator is None or claims is None:
                return Refusal(401, "Administrator login required")
            if not administrator.is_admin:
                return Refusal(403, "Administrator access required")
            admin = _AdminClaim(account=administrator.id, session_version=claims[1])
        now = self.clock()
        return self._encode(
            _Claims(
                app=app.app_id,
                env=self.platform.instance.environment,
                user=user.id,
                provider="tailscale",
                subject=linked[0],
                iat=now,
                exp=now + CONTEXT_LIFETIME_SECONDS,
                admin=admin,
            )
        )

    def verify(
        self, authorization: str | None, assertion: str | None
    ) -> AppContext | Refusal:
        if self.platform is None:
            return Refusal(503, "Managed applications are not configured")
        if authorization is None or not authorization.startswith("Bearer "):
            return Refusal(401, "Missing or invalid application credential")
        credential = self.registry.verify_credential(authorization[7:])
        if (
            credential is None
            or credential.environment != self.platform.instance.environment
        ):
            return Refusal(401, "Missing or invalid application credential")
        app = self.registry.managed_app(credential.app_id)
        if app is None:
            return Refusal(403, "Managed installation required")
        claims = self._decode(assertion) if assertion is not None else None
        if (
            claims is None
            or claims.env != self.platform.instance.environment
            or claims.app != app.app_id
        ):
            return Refusal(401, "Missing or invalid user context")
        now = self.clock()
        if (
            claims.exp != claims.iat + CONTEXT_LIFETIME_SECONDS
            or now >= claims.exp
            or claims.iat > now + CLOCK_SKEW_SECONDS
        ):
            return Refusal(401, "Expired or invalid user context")
        user = self.accounts.resolve_external_identity(claims.provider, claims.subject)
        if user is None or user.id != claims.user:
            return Refusal(403, "Enabled linked account required")
        administrator = None
        if app.registration.access == "administrator":
            if claims.admin is None:
                return Refusal(403, "Administrator context required")
            identity = self.accounts.get_identity(
                claims.admin.account, claims.admin.session_version
            )
            if identity is None or not identity.is_admin:
                return Refusal(403, "Administrator authority revoked")
            administrator = AdministratorBinding(
                identity.id, claims.admin.session_version
            )
        elif claims.admin is not None:
            return Refusal(
                403, "Administrator context is not allowed for this application"
            )
        return AppContext(app, user, administrator)

    def _encode(self, claims: _Claims) -> str:
        if self.platform is None:
            raise RuntimeError("Managed applications are not configured")
        payload = base64.urlsafe_b64encode(
            claims.model_dump_json(exclude_none=True).encode()
        ).rstrip(b"=")
        message = b"v1." + payload
        signature = hmac.new(
            self.platform.context_key, _PURPOSE + message, hashlib.sha256
        ).digest()
        return (
            message + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")
        ).decode()

    def _decode(self, assertion: str) -> _Claims | None:
        if self.platform is None or len(assertion) > MAX_CONTEXT_BYTES:
            return None
        try:
            version, payload, signature = assertion.encode("ascii").split(b".")
            if version != b"v1":
                return None
            supplied = base64.b64decode(
                signature + b"=" * (-len(signature) % 4), altchars=b"-_", validate=True
            )
            expected = hmac.new(
                self.platform.context_key,
                _PURPOSE + version + b"." + payload,
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(supplied, expected):
                return None
            raw = base64.b64decode(
                payload + b"=" * (-len(payload) % 4), altchars=b"-_", validate=True
            )
            return parse_document(_Claims, raw)
        except (ValueError, UnicodeError, binascii.Error, ContractError):
            return None

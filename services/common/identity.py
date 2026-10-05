"""Proxy identity for the application services.

The reverse proxy adds Tailscale identity headers; the control plane maps a
Tailscale login to a stable Home Platform account. Each service decides what
to do with a resolved identity (which tables to adopt, which rows to ensure);
this module only carries the shared mechanism.
"""

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Annotated

from fastapi import Header, HTTPException, status

# How long a resolved Tailscale identity is trusted before asking the control
# plane again. Unlinking an identity therefore takes effect within this time.
IDENTITY_CACHE_SECONDS = 120.0


@dataclass(frozen=True)
class Identity:
    key: str
    display_name: str


class IdentityServiceUnavailableError(Exception):
    pass


def tailscale_key(subject: str) -> str:
    """The owner key a service used before identities were linked."""
    return f"tailscale:{subject}"


def read_secret_file(
    path_value: str, *, what: str = "identity token file"
) -> str | None:
    if not path_value:
        return None
    value = Path(path_value).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"{what} is empty: {path_value}")
    return value


def http_identity_resolver(
    resolver_url: str, resolver_token: str
) -> Callable[[str], Identity | None]:
    def resolve(subject: str) -> Identity | None:
        request = urllib.request.Request(
            resolver_url,
            data=json.dumps({"provider": "tailscale", "subject": subject}).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Service-Identity-Token": resolver_token,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise IdentityServiceUnavailableError from error
        except (OSError, ValueError) as error:
            raise IdentityServiceUnavailableError from error
        return Identity(
            key=f"home-platform:{payload['id']}",
            display_name=str(payload["username"]),
        )

    return resolve


class CachedResolver:
    """Remembers successful identity resolutions for a short time.

    Every request used to make an HTTP call to the control plane and commit
    identity writes. Unlinked (None) and unavailable results are never
    cached, so linking takes effect at once and outages are not hidden.
    ``on_resolved`` runs on every miss and an entry is stored only once it
    returns, so a failed run is retried by the next request.
    """

    def __init__(
        self,
        resolve: Callable[[str], Identity | None],
        on_resolved: Callable[[str, Identity], None],
        ttl: float = IDENTITY_CACHE_SECONDS,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._resolve = resolve
        self._on_resolved = on_resolved
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, Identity]] = {}

    def __call__(self, subject: str) -> Identity | None:
        now = self._clock()
        with self._lock:
            cached = self._entries.get(subject)
        if cached and now - cached[0] < self._ttl:
            return cached[1]
        identity = self._resolve(subject)
        if identity is None:
            with self._lock:
                self._entries.pop(subject, None)
            return None
        self._on_resolved(subject, identity)
        with self._lock:
            self._entries[subject] = (now, identity)
        return identity


def identity_dependency(
    *,
    dev_header: str,
    allow_dev_identity: bool,
    resolver: Callable[[str], Identity | None] | None,
    ensure_user: Callable[[Identity], None],
) -> Callable[..., Identity]:
    """Build the FastAPI dependency that turns proxy headers into an Identity.

    With a resolver, a Tailscale login must be linked to a platform account:
    unlinked logins are refused and resolver outages are reported, never
    cached. Without one, the login itself is the owner key. ``ensure_user``
    runs for those unresolved identities; the resolver's own ``on_resolved``
    covers linked ones.
    """

    def current_identity(
        tailscale_login: Annotated[
            str | None, Header(alias="Tailscale-User-Login")
        ] = None,
        tailscale_name: Annotated[
            str | None, Header(alias="Tailscale-User-Name")
        ] = None,
        dev_user: Annotated[str | None, Header(alias=dev_header)] = None,
    ) -> Identity:
        if tailscale_login:
            normalized = tailscale_login.strip().lower()
            if resolver is not None:
                try:
                    identity = resolver(normalized)
                except IdentityServiceUnavailableError as error:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Home Platform identity service is unavailable",
                    ) from error
                if identity is None:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=(
                            "Link this Tailscale identity from your Job Desk "
                            "account first"
                        ),
                    )
                return identity
            identity = Identity(
                key=tailscale_key(normalized),
                display_name=(tailscale_name or tailscale_login).strip(),
            )
        elif allow_dev_identity and dev_user:
            clean = dev_user.strip().lower()
            identity = Identity(key=f"development:{clean}", display_name=clean)
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Open this service through the private Tailscale URL",
            )
        ensure_user(identity)
        return identity

    return current_identity

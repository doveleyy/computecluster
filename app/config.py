import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from contracts.tokens import load_api_token

DEFAULT_APP_API_URL = "http://host.docker.internal:8000/api/apps/v1"


@dataclass(frozen=True)
class InstanceFacts:
    environment: str
    app_api_url: str
    timezone: str


@dataclass(frozen=True)
class AppPlatformSettings:
    instance: InstanceFacts
    context_key: bytes


def load_app_platform(api_token: str | None) -> AppPlatformSettings | None:
    environment = os.environ.get("HOME_PLATFORM_ENVIRONMENT")
    key_file = os.environ.get("HOME_PLATFORM_APP_CONTEXT_KEY_FILE")
    if environment is None and key_file is None:
        return None
    if not environment or not key_file:
        raise ValueError(
            "Application environment and context key file must be configured together"
        )
    if api_token is None:
        raise ValueError("Managed applications require an administrator API token")
    if (
        not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", environment)
        or len(environment) > 63
    ):
        raise ValueError("Invalid application environment")
    try:
        key = Path(key_file).read_bytes().strip()
    except OSError:
        raise ValueError("Application context key file could not be read") from None
    if len(key) < 32 or (api_token is not None and key == api_token.encode()):
        raise ValueError(
            "Application context key must be separate and at least 32 bytes"
        )
    api_url = os.environ.get("HOME_PLATFORM_APP_API_URL", DEFAULT_APP_API_URL)
    try:
        parsed = urlsplit(api_url)
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and parsed.path == "/api/apps/v1"
            and not parsed.query
            and not parsed.fragment
            and parsed.port != 0
            and not any(c.isspace() for c in api_url)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Invalid application API URL")
    timezone = os.environ.get("HOME_PLATFORM_APP_TIMEZONE", "UTC")
    try:
        ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError("Invalid application timezone") from None
    return AppPlatformSettings(InstanceFacts(environment, api_url, timezone), key)


@dataclass(frozen=True)
class Settings:
    database_path: Path
    api_token: str | None
    lease_seconds: int
    worker_stale_seconds: int
    recovery_interval_seconds: float
    max_attempts: int
    upload_directory: Path
    max_upload_bytes: int
    max_script_upload_bytes: int
    max_project_upload_bytes: int
    artifact_directory: Path
    max_artifact_bytes: int
    max_job_artifact_bytes: int
    max_artifact_store_bytes: int
    artifact_requires_mount: bool
    artifact_owner_scoped: bool
    storage_directory: Path
    member_storage_enabled: bool
    member_storage_user_ids: frozenset[UUID]
    workspace_directory: Path | None
    member_workspace_enabled: bool
    member_workspace_user_ids: frozenset[UUID]
    max_workspace_upload_bytes: int
    power_request_directory: Path | None
    service_identity_token: str | None
    app_platform: AppPlatformSettings | None = None
    release_discovery_enabled: bool = False
    release_read_token: str | None = None


def load_settings() -> Settings:
    release_discovery_enabled = os.environ.get(
        "HOME_PLATFORM_RELEASE_DISCOVERY", "false"
    )
    if release_discovery_enabled not in {"true", "false"}:
        raise ValueError("HOME_PLATFORM_RELEASE_DISCOVERY must be true or false")
    release_token_file = os.environ.get("HOME_PLATFORM_RELEASE_TOKEN_FILE")
    release_read_token = (
        Path(release_token_file).read_text().strip() if release_token_file else None
    )
    database_path = Path(
        os.environ.get("HOME_PLATFORM_DB_PATH", "data/home-platform.db")
    )
    api_token_file = os.environ.get("HOME_PLATFORM_API_TOKEN_FILE")
    api_token = load_api_token(
        default_file=Path(api_token_file) if api_token_file else None,
        required=api_token_file is not None,
    )
    lease_seconds = positive_int("HOME_PLATFORM_LEASE_SECONDS", 15)
    # Must comfortably exceed the worker's maximum idle poll interval
    # (worker.main.MAX_IDLE_POLL_SECONDS, 30s), or an idle worker that is
    # backing off correctly is reported STALE and skipped by best-fit
    # placement. Three missed polls is the threshold; a job already running is
    # protected by lease expiry, not by this.
    worker_stale_seconds = positive_int("HOME_PLATFORM_WORKER_STALE_SECONDS", 90)
    recovery_interval_seconds = positive_float(
        "HOME_PLATFORM_RECOVERY_INTERVAL_SECONDS", 2.0
    )
    max_attempts = positive_int("HOME_PLATFORM_MAX_ATTEMPTS", 3)
    upload_directory = Path(os.environ.get("HOME_PLATFORM_UPLOAD_DIR", "data/uploads"))
    max_upload_bytes = positive_int("HOME_PLATFORM_MAX_UPLOAD_BYTES", 10 * 1024**2)
    max_script_upload_bytes = positive_int(
        "HOME_PLATFORM_MAX_SCRIPT_UPLOAD_BYTES", 256 * 1024
    )
    max_project_upload_bytes = positive_int(
        "HOME_PLATFORM_MAX_PROJECT_UPLOAD_BYTES", 20 * 1024**2
    )
    artifact_directory = Path(
        os.environ.get("HOME_PLATFORM_ARTIFACT_DIR", "data/artifacts")
    )
    max_artifact_bytes = positive_int("HOME_PLATFORM_MAX_ARTIFACT_BYTES", 100 * 1024**2)
    max_job_artifact_bytes = positive_int(
        "HOME_PLATFORM_MAX_JOB_ARTIFACT_BYTES", 512 * 1024**2
    )
    # A backstop, not a policy. Nothing expires because it is old — results are
    # kept until you delete them. This only stops a runaway from filling the
    # disk, by evicting the least recently touched jobs once the store exceeds
    # the cap. Set it generously: eviction is a failure mode, not routine.
    max_artifact_store_bytes = positive_int(
        "HOME_PLATFORM_MAX_ARTIFACT_STORE_BYTES", 50 * 1024**3
    )
    # In a deployment the artifact directory is a mounted share. If the mount is
    # absent the mount point is an ordinary directory on the small system disk,
    # and writes would silently fill it. Set this so the API refuses to write
    # unless the intended filesystem is actually mounted.
    artifact_requires_mount = os.environ.get(
        "HOME_PLATFORM_ARTIFACT_REQUIRE_MOUNT", ""
    ).strip().lower() in {"1", "true", "yes"}
    artifact_owner_scoped = os.environ.get(
        "HOME_PLATFORM_ARTIFACT_OWNER_SCOPED", ""
    ).strip().lower() in {"1", "true", "yes"}
    storage_directory = Path(
        os.environ.get("HOME_PLATFORM_STORAGE_DIR", "/srv/home-platform/storage/nas")
    )
    member_storage_enabled = os.environ.get(
        "HOME_PLATFORM_MEMBER_STORAGE_ENABLED", ""
    ).strip().lower() in {"1", "true", "yes"}
    member_storage_user_ids = frozenset(
        UUID(value.strip())
        for value in os.environ.get("HOME_PLATFORM_MEMBER_STORAGE_USER_IDS", "").split(
            ","
        )
        if value.strip()
    )
    workspace_value = os.environ.get("HOME_PLATFORM_WORKSPACE_DIR", "").strip()
    workspace_directory = Path(workspace_value) if workspace_value else None
    member_workspace_enabled = os.environ.get(
        "HOME_PLATFORM_MEMBER_WORKSPACE_ENABLED", ""
    ).strip().lower() in {"1", "true", "yes"}
    member_workspace_user_ids = frozenset(
        UUID(value.strip())
        for value in os.environ.get(
            "HOME_PLATFORM_MEMBER_WORKSPACE_USER_IDS", ""
        ).split(",")
        if value.strip()
    )
    max_workspace_upload_bytes = positive_int(
        "HOME_PLATFORM_MAX_WORKSPACE_UPLOAD_BYTES", 256 * 1024**2
    )
    power_request_value = os.environ.get("HOME_PLATFORM_POWER_REQUEST_DIR", "").strip()
    power_request_directory = Path(power_request_value) if power_request_value else None
    service_identity_token_file = os.environ.get(
        "HOME_PLATFORM_SERVICE_IDENTITY_TOKEN_FILE"
    )
    service_identity_token = (
        load_api_token(
            explicit_file=Path(service_identity_token_file),
            required=True,
        )
        if service_identity_token_file
        else None
    )
    return Settings(
        release_discovery_enabled=release_discovery_enabled == "true",
        release_read_token=release_read_token,
        database_path=database_path,
        api_token=api_token,
        lease_seconds=lease_seconds,
        worker_stale_seconds=worker_stale_seconds,
        recovery_interval_seconds=recovery_interval_seconds,
        max_attempts=max_attempts,
        upload_directory=upload_directory,
        max_upload_bytes=max_upload_bytes,
        max_script_upload_bytes=max_script_upload_bytes,
        max_project_upload_bytes=max_project_upload_bytes,
        artifact_directory=artifact_directory,
        max_artifact_bytes=max_artifact_bytes,
        max_job_artifact_bytes=max_job_artifact_bytes,
        max_artifact_store_bytes=max_artifact_store_bytes,
        artifact_requires_mount=artifact_requires_mount,
        artifact_owner_scoped=artifact_owner_scoped,
        storage_directory=storage_directory,
        member_storage_enabled=member_storage_enabled,
        member_storage_user_ids=member_storage_user_ids,
        workspace_directory=workspace_directory,
        member_workspace_enabled=member_workspace_enabled,
        member_workspace_user_ids=member_workspace_user_ids,
        max_workspace_upload_bytes=max_workspace_upload_bytes,
        power_request_directory=power_request_directory,
        service_identity_token=service_identity_token,
        app_platform=load_app_platform(api_token),
    )


def positive_int(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def positive_float(name: str, default: float) -> float:
    value = float(os.environ.get(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value

"""User credentials, roles, upload ownership, and signed browser sessions."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.database import Database

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1


class UserRole(StrEnum):
    MEMBER = "MEMBER"
    ADMIN = "ADMIN"


class SessionIdentity(BaseModel):
    id: UUID
    username: str
    role: UserRole

    @property
    def is_admin(self) -> bool:
        return self.role is UserRole.ADMIN


class PortalSession(SessionIdentity):
    storage_enabled: bool


class UserRead(SessionIdentity):
    disabled: bool
    created_at: datetime


class ExternalIdentityRead(BaseModel):
    provider: str
    subject: str
    display_name: str
    linked_at: datetime


class ExternalIdentityStatus(BaseModel):
    linked: ExternalIdentityRead | None
    request_subject: str | None
    request_display_name: str | None


class ServiceIdentityResolve(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["tailscale"]
    subject: str = Field(min_length=1, max_length=320)


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(
        min_length=3,
        max_length=32,
        pattern=r"^[a-z][a-z0-9._-]*$",
    )
    password: str = Field(min_length=12, max_length=128)
    role: Literal[UserRole.MEMBER] = UserRole.MEMBER


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    disabled: bool


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)

    @model_validator(mode="after")
    def password_must_change(self) -> PasswordChange:
        if self.current_password == self.new_password:
            raise ValueError("new password must be different")
        return self


class PasswordReset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_password: str = Field(min_length=12, max_length=128)


class DashboardLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str | None = Field(default=None, min_length=1)
    username: str | None = Field(default=None, min_length=1, max_length=32)
    password: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def exactly_one_login_method(self) -> DashboardLogin:
        token_login = self.token is not None
        password_login = self.username is not None or self.password is not None
        if token_login == password_login:
            raise ValueError("provide either an owner token or username and password")
        if password_login and (self.username is None or self.password is None):
            raise ValueError("username and password must be provided together")
        return self


class UserExistsError(Exception):
    pass


class UserNotFoundError(Exception):
    pass


class InvalidCurrentPasswordError(Exception):
    pass


class ExternalIdentityConflictError(Exception):
    pass


class AccountStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, user_create: UserCreate) -> UserRead:
        user_id = uuid4()
        now = datetime.now(UTC)
        password_hash = hash_password(user_create.password)
        try:
            with self.database.connect() as connection:
                row = connection.execute(
                    """
                    INSERT INTO users (
                        id, username, role, password_hash, disabled, created_at
                    ) VALUES (?, ?, ?, ?, 0, ?)
                    RETURNING *
                    """,
                    (
                        str(user_id),
                        user_create.username,
                        user_create.role.value,
                        password_hash,
                        now.isoformat(),
                    ),
                ).fetchone()
        except sqlite3.IntegrityError as error:
            # Keep sqlite details out of the HTTP boundary. The only expected
            # integrity conflict here is the unique normalized username.
            if "UNIQUE constraint failed: users.username" in str(error):
                raise UserExistsError(user_create.username) from error
            raise
        return self._row_to_user(row)

    def authenticate(self, username: str, password: str) -> SessionIdentity | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
        if (
            row is None
            or bool(row["disabled"])
            or row["password_hash"] is None
            or not verify_password(password, row["password_hash"])
        ):
            return None
        return self._row_to_identity(row)

    def get_identity(
        self, user_id: UUID, session_version: int | None = None
    ) -> SessionIdentity | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE id = ?", (str(user_id),)
            ).fetchone()
        if (
            row is None
            or bool(row["disabled"])
            or (
                session_version is not None
                and int(row["session_version"]) != session_version
            )
        ):
            return None
        return self._row_to_identity(row)

    def session_version(self, user_id: UUID) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT session_version FROM users WHERE id = ?", (str(user_id),)
            ).fetchone()
        if row is None:
            raise UserNotFoundError(user_id)
        return int(row["session_version"])

    def list(self) -> list[UserRead]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM users ORDER BY role DESC, username"
            ).fetchall()
        return [self._row_to_user(row) for row in rows]

    def set_disabled(self, user_id: UUID, disabled: bool) -> UserRead:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                UPDATE users SET disabled = ?
                WHERE id = ? AND role = 'MEMBER'
                RETURNING *
                """,
                (int(disabled), str(user_id)),
            ).fetchone()
        if row is None:
            raise UserNotFoundError(user_id)
        return self._row_to_user(row)

    def change_password(
        self, user_id: UUID, current_password: str, new_password: str
    ) -> None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT password_hash FROM users WHERE id = ? AND disabled = 0",
                (str(user_id),),
            ).fetchone()
            if (
                row is None
                or row["password_hash"] is None
                or not verify_password(current_password, row["password_hash"])
            ):
                raise InvalidCurrentPasswordError
            connection.execute(
                """
                UPDATE users
                SET password_hash = ?, session_version = session_version + 1
                WHERE id = ?
                """,
                (hash_password(new_password), str(user_id)),
            )

    def reset_password(self, user_id: UUID, new_password: str) -> None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                UPDATE users
                SET password_hash = ?, session_version = session_version + 1
                WHERE id = ? AND role = 'MEMBER'
                RETURNING id
                """,
                (hash_password(new_password), str(user_id)),
            ).fetchone()
        if row is None:
            raise UserNotFoundError(user_id)

    def workload_owners(self) -> dict[str, dict[str, str]]:
        with self.database.connect() as connection:
            job_rows = connection.execute(
                """
                SELECT jobs.id, users.username
                FROM jobs JOIN users ON users.id = jobs.owner_user_id
                """
            ).fetchall()
            group_rows = connection.execute(
                """
                SELECT job_groups.id, users.username
                FROM job_groups JOIN users ON users.id = job_groups.owner_user_id
                """
            ).fetchall()
        return {
            "jobs": {row["id"]: row["username"] for row in job_rows},
            "groups": {row["id"]: row["username"] for row in group_rows},
        }

    def link_external_identity(
        self,
        user_id: UUID,
        provider: str,
        subject: str,
        display_name: str,
    ) -> ExternalIdentityRead:
        provider = normalize_external_provider(provider)
        subject = normalize_external_subject(subject)
        now = datetime.now(UTC)
        with self.database.connect() as connection:
            subject_row = connection.execute(
                """
                SELECT user_id FROM external_identities
                WHERE provider = ? AND subject = ?
                """,
                (provider, subject),
            ).fetchone()
            user_row = connection.execute(
                """
                SELECT subject FROM external_identities
                WHERE user_id = ? AND provider = ?
                """,
                (str(user_id), provider),
            ).fetchone()
            if (subject_row is not None and subject_row["user_id"] != str(user_id)) or (
                user_row is not None and user_row["subject"] != subject
            ):
                raise ExternalIdentityConflictError
            row = connection.execute(
                """
                INSERT INTO external_identities (
                    provider, subject, user_id, display_name, linked_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(provider, subject) DO UPDATE SET
                    display_name = excluded.display_name
                RETURNING provider, subject, display_name, linked_at
                """,
                (
                    provider,
                    subject,
                    str(user_id),
                    display_name.strip(),
                    now.isoformat(),
                ),
            ).fetchone()
        return self._row_to_external_identity(row)

    def external_identity(
        self, user_id: UUID, provider: str
    ) -> ExternalIdentityRead | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT provider, subject, display_name, linked_at
                FROM external_identities
                WHERE user_id = ? AND provider = ?
                """,
                (str(user_id), normalize_external_provider(provider)),
            ).fetchone()
        return self._row_to_external_identity(row) if row is not None else None

    def unlink_external_identity(self, user_id: UUID, provider: str) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM external_identities
                WHERE user_id = ? AND provider = ?
                """,
                (str(user_id), normalize_external_provider(provider)),
            )
        return cursor.rowcount == 1

    def resolve_external_identity(
        self, provider: str, subject: str
    ) -> SessionIdentity | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT users.*
                FROM external_identities
                JOIN users ON users.id = external_identities.user_id
                WHERE external_identities.provider = ?
                  AND external_identities.subject = ?
                  AND users.disabled = 0
                """,
                (
                    normalize_external_provider(provider),
                    normalize_external_subject(subject),
                ),
            ).fetchone()
        return self._row_to_identity(row) if row is not None else None

    def record_upload(self, upload_id: UUID, owner_user_id: UUID, kind: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO uploads (id, owner_user_id, kind, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    str(upload_id),
                    str(owner_user_id),
                    kind,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def upload_belongs_to(self, upload_id: UUID, owner_user_id: UUID) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM uploads WHERE id = ? AND owner_user_id = ?",
                (str(upload_id), str(owner_user_id)),
            ).fetchone()
        return row is not None

    def forget_uploads(self, upload_ids: set[UUID]) -> None:
        if not upload_ids:
            return
        placeholders = ", ".join("?" for _ in upload_ids)
        with self.database.connect() as connection:
            connection.execute(
                f"DELETE FROM uploads WHERE id IN ({placeholders})",
                tuple(str(upload_id) for upload_id in upload_ids),
            )

    @staticmethod
    def _row_to_identity(row: sqlite3.Row) -> SessionIdentity:
        return SessionIdentity(
            id=UUID(row["id"]),
            username=row["username"],
            role=UserRole(row["role"]),
        )

    @classmethod
    def _row_to_user(cls, row: sqlite3.Row) -> UserRead:
        identity = cls._row_to_identity(row)
        return UserRead(
            **identity.model_dump(),
            disabled=bool(row["disabled"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _row_to_external_identity(row: sqlite3.Row) -> ExternalIdentityRead:
        return ExternalIdentityRead(
            provider=row["provider"],
            subject=row["subject"],
            display_name=row["display_name"],
            linked_at=datetime.fromisoformat(row["linked_at"]),
        )


def normalize_external_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    if normalized != "tailscale":
        raise ValueError("unsupported external identity provider")
    return normalized


def normalize_external_subject(subject: str) -> str:
    normalized = subject.strip().lower()
    if not normalized or len(normalized) > 320:
        raise ValueError("invalid external identity subject")
    return normalized


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P
    )
    return "$".join(
        (
            "scrypt",
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            base64.urlsafe_b64encode(salt).decode(),
            base64.urlsafe_b64encode(derived).decode(),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_text, digest_text = encoded.split("$")
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_text)
        expected = base64.urlsafe_b64decode(digest_text)
        actual = hashlib.scrypt(
            password.encode(), salt=salt, n=int(n), r=int(r), p=int(p)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def encode_session(
    identity: SessionIdentity,
    secret: str,
    expires_at: int,
    session_version: int = 1,
) -> str:
    payload = json.dumps(
        {
            "user_id": str(identity.id),
            "expires_at": expires_at,
            "session_version": session_version,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
    signature = hmac.new(secret.encode(), encoded, hashlib.sha256).digest()
    return encoded.decode() + "." + base64.urlsafe_b64encode(signature).decode()


def decode_session(value: str, secret: str, now: int) -> tuple[UUID, int] | None:
    try:
        encoded, signature_text = value.split(".", 1)
        supplied = base64.urlsafe_b64decode(signature_text)
        expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, expected):
            return None
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
        if int(payload["expires_at"]) < now:
            return None
        return UUID(payload["user_id"]), int(payload.get("session_version", 1))
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, binascii.Error):
        return None

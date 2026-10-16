"""Unprivileged release verification and core writes for operator onboarding."""

import argparse
import asyncio
import ctypes
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
from pathlib import Path

import httpx

from app.app_contracts import (
    MAX_DOCUMENT_BYTES,
    ContractError,
    PublishedSource,
    Registration,
    ReleaseManifest,
    parse_document,
)
from app.app_github import GitHubPublications
from app.app_registry import AppRegistry, decode_registration
from app.app_spool import publish, read_owned
from app.database import Database
from app.migrations import MIGRATIONS
from ops.onboarding import CoreStatus


async def verified_release(
    source: PublishedSource, token: str | None
) -> ReleaseManifest:
    async with httpx.AsyncClient(trust_env=False) as client:
        publications = GitHubPublications(client, token)
        listing = await publications.listing(source, None)
        if not listing.releases:
            raise ContractError("Repository has no stable publication")
        release = await publications.verify(source, listing.releases[0]["id"])
        return release.manifest


def status(registry: AppRegistry, app_id: str) -> CoreStatus:
    with sqlite3.connect(
        registry.database.path.as_uri() + "?mode=ro", uri=True
    ) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        registrations = tuple(
            decode_registration(row["app_id"], row["route"], row["definition_json"])
            for row in connection.execute(
                "SELECT app_id, route, definition_json FROM app_registrations"
            )
        )
        credential = connection.execute(
            "SELECT credential_id, file_path, verifier FROM app_credentials "
            "WHERE app_id = ? AND revoked_at IS NULL",
            (app_id,),
        ).fetchone()
        instance = connection.execute(
            "SELECT environment, app_api_url, timezone FROM platform_instance"
        ).fetchone()
    valid = False
    if credential is not None:
        try:
            path = Path(credential["file_path"])
            token = read_owned(path, os.geteuid(), 1024).decode().strip()
            valid = (
                not path.lstat().st_mode & 0o077
                and re.fullmatch(r"hpa1\.[0-9a-f]{32}\.[A-Za-z0-9_-]{43}", token)
                is not None
                and token.split(".")[1] == credential["credential_id"]
                and hmac.compare_digest(
                    hashlib.sha256(token.encode()).hexdigest(), credential["verifier"]
                )
            )
        except (OSError, ValueError, UnicodeError):
            pass
    return CoreStatus(
        registrations=registrations,
        credential_path=credential["file_path"] if credential else None,
        credential_valid=valid,
        instance_configured=instance is not None,
        environment=instance["environment"] if instance else None,
        app_api_url=instance["app_api_url"] if instance else None,
        timezone=instance["timezone"] if instance else None,
    )


def ensure_credential(registry: AppRegistry, app_id: str, path: Path) -> None:
    instance = registry.instance()
    if instance is None or registry.managed_app(app_id) is None:
        raise ContractError("Managed registration and core instance are required")
    active = registry.active_credential(app_id)
    if active is not None:
        current = status(registry, app_id)
        if current.credential_path != str(path) or not current.credential_valid:
            raise ContractError("Active application credential conflicts")
        return
    try:
        payload = read_owned(path, os.geteuid(), 1024)
    except FileNotFoundError:
        token = f"hpa1.{secrets.token_hex(16)}.{secrets.token_urlsafe(32)}"
        publish(path, token.encode() + b"\n", mode=0o600)
    else:
        token = payload.decode().strip()
    if not re.fullmatch(r"hpa1\.[0-9a-f]{32}\.[A-Za-z0-9_-]{43}", token):
        raise ContractError("Incomplete credential file has invalid format")
    if path.lstat().st_mode & 0o077:
        raise ContractError("Application credential must be private")
    with registry.database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute(
            "SELECT 1 FROM app_credentials WHERE credential_id = ? OR "
            "(app_id = ? AND revoked_at IS NULL) OR "
            "(file_path = ? AND revoked_at IS NULL)",
            (token.split(".")[1], app_id, str(path)),
        ).fetchone():
            raise ContractError("Credential database changed; retry after review")
        connection.execute(
            "INSERT INTO app_credentials "
            "(credential_id, app_id, environment, verifier, file_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                token.split(".")[1],
                app_id,
                instance.environment,
                hashlib.sha256(token.encode()).hexdigest(),
                str(path),
            ),
        )


def main() -> None:
    if os.geteuid() == 0 or ctypes.CDLL(None).prctl(38, 1, 0, 0, 0) != 0:
        raise ContractError("Unprivileged no-new-privileges child required")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("release", "status", "register", "credential"))
    parser.add_argument("database", type=Path)
    parser.add_argument("app_id", nargs="?", default="")
    args = parser.parse_args()
    payload = sys.stdin.buffer.read(MAX_DOCUMENT_BYTES + 1)
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise ContractError("Onboarding request exceeds byte limit")
    if args.kind == "release":
        request = json.loads(payload)
        source = PublishedSource.model_validate(request["source"])
        release = asyncio.run(verified_release(source, request.get("token")))
        print(release.model_dump_json(exclude_none=True))
        return
    database = Database(args.database)
    try:
        info = args.database.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ContractError("Core database ownership is unsafe")
        with sqlite3.connect(
            args.database.as_uri() + "?mode=ro", uri=True
        ) as connection:
            version = connection.execute(
                "SELECT coalesce(max(version), 0) FROM schema_migrations"
            ).fetchone()[0]
        if version != MIGRATIONS[-1][0]:
            raise ContractError("Core database must be migrated before onboarding")
        registry = AppRegistry(database)
        if args.kind == "status":
            print(status(registry, args.app_id).model_dump_json())
        elif args.kind == "register":
            registration = parse_document(Registration, payload)
            registry.register(registration.model_dump_json().encode())
            print("{}")
        else:
            request = json.loads(payload)
            ensure_credential(registry, args.app_id, Path(request["path"]))
            print("{}")
    finally:
        database.close()


if __name__ == "__main__":
    try:
        main()
    except (ContractError, OSError, ValueError, sqlite3.Error, httpx.HTTPError):
        print("Onboarding child refused the operation", file=sys.stderr)
        raise SystemExit(1) from None

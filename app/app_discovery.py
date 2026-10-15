import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from app.app_contracts import ContractError, PublishedSource
from app.app_github import GitHubPublications, ProviderUnavailable, PublicationPending
from app.app_publication import MAX_STORE_BYTES, VerifiedPublication
from app.app_registry import AppRegistry, decode_registration
from app.database import Database

logger = logging.getLogger(__name__)
MAX_VERIFY_ATTEMPTS = 5
RETRY_SECONDS = 300


class RetainedStoreFull(Exception):
    """The platform store has no room; the release itself is not at fault."""


@dataclass(frozen=True)
class DiscoveryResult:
    app_id: str
    latest_sha256: str | None
    waiting: tuple[int, ...]
    error: str | None
    last_success: str | None
    abandoned: tuple[int, ...]


class ReleaseDiscovery:
    def __init__(
        self,
        database: Database,
        provider: GitHubPublications,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database = database
        self.provider = provider
        self.clock = clock

    async def discover(self, app_id: str) -> DiscoveryResult:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT route, definition_json FROM app_registrations WHERE app_id = ?",
                (app_id,),
            ).fetchone()
            if row is None:
                raise ContractError("Application is not registered")
            registration = decode_registration(
                app_id, row["route"], row["definition_json"]
            )
            if not isinstance(registration.source, PublishedSource):
                raise ContractError("Application has no published source")
            connection.execute(
                "INSERT OR IGNORE INTO app_discovery (app_id) VALUES (?)", (app_id,)
            )
            cursor = connection.execute(
                "SELECT * FROM app_discovery WHERE app_id = ?", (app_id,)
            ).fetchone()
        waiting: set[int] = set(json.loads(cursor["waiting_json"]))
        etag = cursor["etag"]
        error = None
        now = self.clock()
        try:
            listing = await self.provider.listing(registration.source, etag)
            etag = listing.etag
            ids = set(waiting)
            if listing.releases is not None:
                ids.update(item["id"] for item in listing.releases)
            for release_id in sorted(ids, reverse=True):
                with self.database.connect() as connection:
                    known = connection.execute(
                        "SELECT 1 FROM app_eligible_releases WHERE "
                        "app_id = ? AND release_id = ?",
                        (app_id, release_id),
                    ).fetchone()
                    attempt = connection.execute(
                        "SELECT attempts, next_attempt FROM app_release_attempts "
                        "WHERE app_id = ? AND release_id = ?",
                        (app_id, release_id),
                    ).fetchone()
                attempts = attempt["attempts"] if attempt else 0
                if known or attempts >= MAX_VERIFY_ATTEMPTS:
                    waiting.discard(release_id)
                    continue
                if attempt and attempt["next_attempt"] > now:
                    waiting.add(release_id)
                    error = "A release could not be verified"
                    continue
                try:
                    publication = await self.provider.verify(
                        registration.source, release_id
                    )
                    AppRegistry(self.database)._validate_release(
                        registration, publication.manifest
                    )
                    self._retain(app_id, release_id, publication)
                    waiting.discard(release_id)
                except (ProviderUnavailable, RetainedStoreFull):
                    raise
                except PublicationPending:
                    waiting.add(release_id)
                except ContractError:
                    attempts += 1
                    with self.database.connect() as connection:
                        connection.execute(
                            "INSERT OR REPLACE INTO app_release_attempts "
                            "VALUES (?, ?, ?, ?)",
                            (
                                app_id,
                                release_id,
                                attempts,
                                now + RETRY_SECONDS * 2 ** (attempts - 1),
                            ),
                        )
                    if attempts < MAX_VERIFY_ATTEMPTS:
                        waiting.add(release_id)
                    else:
                        waiting.discard(release_id)
                    error = "A release could not be verified"
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE app_discovery SET etag = ?, waiting_json = ?, error = ?, "
                    "last_success = CURRENT_TIMESTAMP WHERE app_id = ?",
                    (
                        etag,
                        json.dumps(sorted(waiting, reverse=True)[:20]),
                        error,
                        app_id,
                    ),
                )
        except RetainedStoreFull:
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE app_discovery SET error = ? WHERE app_id = ?",
                    ("Retained release store is full", app_id),
                )
        except (ContractError, httpx.HTTPError):
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE app_discovery SET error = ? WHERE app_id = ?",
                    ("Release provider is unavailable", app_id),
                )
        return self.status(app_id)

    def _retain(
        self, app_id: str, release_id: int, release: VerifiedPublication
    ) -> None:
        digest = release.publication.manifest_sha256
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            known = connection.execute(
                "SELECT artifact_sha256 FROM app_eligible_releases "
                "WHERE app_id = ? AND manifest_sha256 = ?",
                (app_id, digest),
            ).fetchone()
            if known:
                if known[0] != release.artifact_sha256:
                    raise ContractError("Recorded release proof cannot be replaced")
                return
            used = connection.execute(
                "SELECT coalesce(sum(length(archive)), 0) FROM app_eligible_releases"
            ).fetchone()[0]
            if used + len(release.archive) > MAX_STORE_BYTES:
                raise RetainedStoreFull
            connection.execute(
                "INSERT INTO app_eligible_releases "
                "(app_id, manifest_sha256, release_id, artifact_id, "
                "artifact_sha256, archive, commit_title, committed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    app_id,
                    digest,
                    release_id,
                    release.artifact_id,
                    release.artifact_sha256,
                    release.archive,
                    release.commit_title,
                    release.committed_at,
                ),
            )
            connection.execute(
                "UPDATE app_discovery SET latest_sha256 = (SELECT manifest_sha256 "
                "FROM app_eligible_releases WHERE app_id = ? ORDER BY "
                "release_id DESC LIMIT 1) "
                "WHERE app_id = ?",
                (app_id, app_id),
            )

    def status(self, app_id: str) -> DiscoveryResult:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM app_discovery WHERE app_id = ?", (app_id,)
            ).fetchone()
            abandoned = tuple(
                item[0]
                for item in connection.execute(
                    "SELECT release_id FROM app_release_attempts a WHERE app_id = ? "
                    "AND attempts >= ? AND NOT EXISTS (SELECT 1 FROM "
                    "app_eligible_releases e WHERE e.app_id = a.app_id "
                    "AND e.release_id = a.release_id) ORDER BY release_id DESC",
                    (app_id, MAX_VERIFY_ATTEMPTS),
                )
            )
        if row is None:
            return DiscoveryResult(app_id, None, (), None, None, abandoned)
        return DiscoveryResult(
            app_id,
            row["latest_sha256"],
            tuple(json.loads(row["waiting_json"])),
            row["error"],
            row["last_success"],
            abandoned,
        )

    async def all(self) -> list[DiscoveryResult]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT app_id, route, definition_json FROM "
                "app_registrations ORDER BY app_id"
            ).fetchall()
        results = []
        for row in rows:
            try:
                registration = decode_registration(*row)
            except ContractError:
                logger.warning(
                    "Release discovery skipped invalid registration %s", row["app_id"]
                )
                continue
            if isinstance(registration.source, PublishedSource):
                results.append(await self.discover(row["app_id"]))
        return results


async def discovery_loop(database: Database, token: str | None) -> None:
    async with httpx.AsyncClient(trust_env=False) as client:
        discovery = ReleaseDiscovery(database, GitHubPublications(client, token))
        while True:
            try:
                await discovery.all()
            except (sqlite3.Error, OSError):
                logger.warning("Release discovery could not access local records")
            await asyncio.sleep(300)

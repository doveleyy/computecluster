import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from app.app_contracts import ContractError, PublishedSource, parse_document
from app.app_publication import (
    ARTIFACT_NAME,
    MAX_PACKAGE_BYTES,
    Publication,
    VerifiedPublication,
    verify_package,
)

API = "https://api.github.com"
MAX_COMMIT_TITLE = 200


class PublicationPending(ContractError):
    pass


class ProviderUnavailable(ContractError):
    pass


def commit_title(run: dict[str, Any]) -> str | None:
    head = run.get("head_commit")
    message = head.get("message") if isinstance(head, dict) else None
    if not isinstance(message, str):
        message = run.get("display_title")
    if not isinstance(message, str):
        return None
    line = next(iter(message.strip().splitlines()), "")
    title = " ".join("".join(c if c.isprintable() else " " for c in line).split())
    if len(title) > MAX_COMMIT_TITLE:
        title = title[: MAX_COMMIT_TITLE - 1] + "…"
    return title or None


def committed_at(run: dict[str, Any]) -> str | None:
    head = run.get("head_commit")
    value = head.get("timestamp") if isinstance(head, dict) else None
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ReleaseListing:
    etag: str | None
    releases: list[dict[str, Any]] | None


class GitHubPublications:
    def __init__(self, client: httpx.AsyncClient, token: str | None = None) -> None:
        self.client = client
        self.token = token

    async def _get(
        self,
        path: str,
        *,
        binary: bool = False,
        etag: str | None = None,
        accept: Literal[
            "application/vnd.github+json", "application/octet-stream"
        ] = "application/vnd.github+json",
    ) -> tuple[int, bytes, str | None]:
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if etag:
            headers["If-None-Match"] = etag
        url = API + path
        limit = MAX_PACKAGE_BYTES if binary else 1024 * 1024
        for redirect in range(3):
            async with self.client.stream(
                "GET", url, headers=headers, follow_redirects=False, timeout=30
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location", "")
                    parsed = urlsplit(location)
                    if (
                        not binary
                        or redirect == 2
                        or parsed.scheme != "https"
                        or parsed.username is not None
                        or parsed.password is not None
                        or parsed.port not in {None, 443}
                        or not parsed.hostname
                        or not (
                            parsed.hostname.endswith(".githubusercontent.com")
                            or parsed.hostname.endswith(".blob.core.windows.net")
                        )
                    ):
                        raise ContractError("Untrusted GitHub download redirect")
                    url = location
                    headers = {"Accept": "application/octet-stream"}
                    continue
                if response.status_code == 304 and etag:
                    return 304, b"", etag
                if response.status_code >= 500 or response.status_code in {403, 429}:
                    raise ProviderUnavailable("GitHub is unavailable or rate limited")
                if response.status_code != 200:
                    raise ContractError("GitHub publication is unavailable")
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > limit:
                        raise ContractError("GitHub response exceeds the byte limit")
                return 200, bytes(chunks), response.headers.get("etag")
        raise ContractError("Too many GitHub redirects")

    async def _json(self, path: str) -> Any:
        _, body, _ = await self._get(path)
        try:
            return json.loads(body)
        except (ValueError, UnicodeError):
            raise ContractError("Invalid GitHub JSON response") from None

    async def listing(
        self, source: PublishedSource, etag: str | None
    ) -> ReleaseListing:
        status, body, new_etag = await self._get(
            f"/repos/{source.repository}/releases?per_page=20", etag=etag
        )
        if status == 304:
            return ReleaseListing(new_etag, None)
        try:
            values = json.loads(body)
            if not isinstance(values, list) or len(values) > 20:
                raise ValueError
            releases = []
            for item in values:
                if not isinstance(item, dict) or type(item.get("id")) is not int:
                    raise ValueError
                if item.get("draft") is False and item.get("prerelease") is False:
                    releases.append(item)
            return ReleaseListing(new_etag, releases)
        except (ValueError, UnicodeError):
            raise ContractError("Invalid GitHub release listing") from None

    async def verify(
        self, source: PublishedSource, release_id: int
    ) -> VerifiedPublication:
        base = f"/repos/{source.repository}"
        release = await self._json(f"{base}/releases/{release_id}")
        try:
            if release["draft"] or release["prerelease"] or release["id"] != release_id:
                raise ValueError
            assets = release["assets"]
            if not isinstance(assets, list) or len(assets) > 34:
                raise ValueError
            by_name = {asset["name"]: asset for asset in assets}
            if len(by_name) != len(assets):
                raise ValueError
            metadata = by_name["publication.json"]
            payload = await self._asset(base, metadata)
            proof = parse_document(Publication, payload)
            run = await self._json(
                f"{base}/actions/runs/{proof.run_id}/attempts/{proof.run_attempt}"
            )
            if (
                run["id"] != proof.run_id
                or run["run_attempt"] != proof.run_attempt
                or run["repository"]["full_name"] != source.repository
                or run["head_repository"]["full_name"] != source.repository
                or run["event"] != "push"
                or run["head_branch"] != source.branch
                or run["path"] != source.workflow
                or run["head_sha"] != proof.source_commit
            ):
                raise ContractError(
                    "Workflow source does not match registered authority"
                )
            if run["status"] != "completed":
                raise PublicationPending("Publication workflow is still running")
            if run["conclusion"] != "success":
                raise ContractError("Publication workflow did not succeed")
            response = await self._json(
                f"{base}/actions/runs/{proof.run_id}/artifacts?per_page=100"
            )
            candidates = [
                item
                for item in response["artifacts"]
                if item["name"] == f"{ARTIFACT_NAME}-{proof.run_attempt}"
            ]
            if response["total_count"] > 100 or len(candidates) != 1:
                raise ContractError("Expected one workflow publication artifact")
            artifact = candidates[0]
            digest = artifact["digest"]
            if (
                artifact["expired"]
                or type(artifact["id"]) is not int
                or not isinstance(digest, str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                or artifact["size_in_bytes"] > MAX_PACKAGE_BYTES
                or artifact["workflow_run"]["id"] != proof.run_id
                or artifact["workflow_run"]["head_sha"] != proof.source_commit
            ):
                raise ContractError("Invalid workflow artifact authority")
            _, archive, _ = await self._get(
                f"{base}/actions/artifacts/{artifact['id']}/zip", binary=True
            )
            verified = verify_package(archive, source, artifact["id"], digest[7:])
            if verified.files["publication.json"] != payload or set(by_name) != set(
                verified.files
            ):
                raise ContractError("Release assets differ from workflow publication")
            for name, expected in verified.files.items():
                if (
                    name != "publication.json"
                    and await self._asset(base, by_name[name]) != expected
                ):
                    raise ContractError("Release asset differs from workflow bytes")
            return replace(
                verified, commit_title=commit_title(run), committed_at=committed_at(run)
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, ContractError):
                raise
            raise ContractError("Invalid GitHub publication evidence") from None

    async def _asset(self, base: str, asset: dict[str, Any]) -> bytes:
        if (
            type(asset["id"]) is not int
            or asset["id"] <= 0
            or type(asset["size"]) is not int
            or not 0 < asset["size"] <= MAX_PACKAGE_BYTES
            or asset["state"] != "uploaded"
        ):
            raise ContractError("Invalid release asset identity or size")
        _, payload, _ = await self._get(
            f"{base}/releases/assets/{asset['id']}",
            binary=True,
            accept="application/octet-stream",
        )
        if len(payload) != asset["size"]:
            raise ContractError("Release asset size changed")
        return payload

import hashlib
import io
import stat
import zipfile
import zlib
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from app.app_contracts import (
    Basename,
    Contract,
    ContractError,
    PublishedSource,
    ReleaseManifest,
    Sha256,
    parse_document,
)

MAX_PACKAGE_BYTES = 32 * 1024**2
MAX_STORE_BYTES = 512 * 1024**2
ARTIFACT_NAME = "home-platform-publication"


class Publication(Contract):
    publication_version: Literal[1]
    repository: str
    branch: str
    workflow: str
    source_commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    run_id: Annotated[int, Field(gt=0)]
    run_attempt: Annotated[int, Field(gt=0)]
    manifest_sha256: Sha256
    assets: Annotated[dict[Basename, Sha256], Field(max_length=33)]


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def unpack(payload: bytes) -> dict[str, bytes]:
    if len(payload) > MAX_PACKAGE_BYTES:
        raise ContractError("Publication package exceeds the byte limit")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = archive.infolist()
            if not 1 <= len(infos) <= 34:
                raise ContractError("Publication package has too many entries")
            names: set[str] = set()
            total = 0
            for info in infos:
                name = info.filename
                if (
                    name != info.orig_filename
                    or not name
                    or name in {".", ".."}
                    or "/" in name
                    or "\\" in name
                    or name.casefold() in names
                    or stat.S_IFMT(info.external_attr >> 16) not in {0, stat.S_IFREG}
                    or info.flag_bits & 1
                    or info.compress_type
                    not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                ):
                    raise ContractError("Publication package has unsafe entries")
                names.add(name.casefold())
                total += info.file_size
                if total > MAX_PACKAGE_BYTES:
                    raise ContractError("Expanded publication exceeds the byte limit")
            return {info.filename: archive.read(info) for info in infos}
    except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, OSError):
        raise ContractError("Invalid publication archive") from None


@dataclass(frozen=True)
class VerifiedPublication:
    publication: Publication
    manifest: ReleaseManifest
    files: dict[str, bytes]
    artifact_id: int
    artifact_sha256: str
    archive: bytes
    commit_title: str | None = None
    committed_at: str | None = None


def verify_package(
    payload: bytes, source: PublishedSource, artifact_id: int, artifact_sha256: str
) -> VerifiedPublication:
    if sha256(payload) != artifact_sha256:
        raise ContractError("Actions artifact digest does not match downloaded bytes")
    files = unpack(payload)
    if "publication.json" not in files or "manifest.json" not in files:
        raise ContractError("Publication metadata and manifest are required")
    publication = parse_document(Publication, files["publication.json"])
    manifest = parse_document(ReleaseManifest, files["manifest.json"])
    expected = {"manifest.json": publication.manifest_sha256}
    if len({item.asset for item in manifest.workloads}) != len(
        manifest.workloads
    ) or any(
        item.asset in {"manifest.json", "publication.json"}
        for item in manifest.workloads
    ):
        raise ContractError("Workload asset names must be unique and unreserved")
    expected.update({item.asset: item.sha256 for item in manifest.workloads})
    if manifest.image.split("@", 1)[0] != source.image_repository:
        raise ContractError("Release image repository is not permitted")
    if (
        publication.repository != source.repository
        or publication.branch != source.branch
        or publication.workflow != source.workflow
        or publication.source_commit != manifest.source_commit
        or publication.assets != expected
        or set(files) != {*expected, "publication.json"}
        or any(sha256(files[name]) != digest for name, digest in expected.items())
    ):
        raise ContractError(
            "Publication does not bind the configured source and assets"
        )
    return VerifiedPublication(
        publication, manifest, files, artifact_id, artifact_sha256, payload
    )

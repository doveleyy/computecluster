"""Fixed portable checker process and report boundary for preview guests."""

import json
import os
import selectors
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.app_contracts import Contract, ContractError, Sha256, parse_document
from app.app_previews import MAX_PREVIEW_ERROR
from app.app_publication import sha256

CHECKER_SHA256 = "9de8dcd53d4968cd2cf7f57ef818f41b52c756b093d4818e258d60465977c3b4"
ASSET_DIRECTORY = Path(__file__).resolve().parent / "assets"
MAX_CHECKER_BYTES = 2 * 1024**2
MAX_TARGET_BYTES = 4096
MAX_REPORT_BYTES = 65536
MAX_ERROR_BYTES = 4096
MAX_FINDING_DETAIL = 120
CHECKER_TIMEOUT = 180
CheckName = Literal[
    "health",
    "ready",
    "version",
    "direct-context",
    "forged-context",
    "routed-access",
    "base-path",
]
PORTABLE_CHECKS = frozenset(
    {
        "health",
        "ready",
        "version",
        "direct-context",
        "forged-context",
        "routed-access",
        "base-path",
    }
)


@dataclass(frozen=True)
class RunningTarget:
    direct_origin: str
    routed_origin: str
    base_path: str
    routed_headers: Mapping[str, str] = field(repr=False)

    def to_bytes(self) -> bytes:
        return json.dumps(
            {
                "direct_origin": self.direct_origin,
                "routed_origin": self.routed_origin,
                "base_path": self.base_path,
                "routed_headers": dict(self.routed_headers),
            }
        ).encode()


class Finding(Contract):
    check: CheckName
    passed: bool
    detail: Annotated[str, Field(max_length=512)]


class CheckerRejected(ContractError):
    """The checker reported failing findings; the message is safe to show."""


class CheckerReport(Contract):
    checker_version: Literal["0.1.1"]
    contract_version: Annotated[int, Field(ge=1, le=1)]
    mode: Literal["final"]
    manifest_sha256: Sha256
    findings: Annotated[tuple[Finding, ...], Field(min_length=7, max_length=7)]

    @model_validator(mode="after")
    def complete(self) -> Self:
        if {item.check for item in self.findings} != PORTABLE_CHECKS:
            raise ValueError("Checker finding set is incomplete")
        return self

    def checks(self) -> dict[str, bool]:
        return {item.check: item.passed for item in self.findings}


def checker_artifact() -> Path:
    artifact = ASSET_DIRECTORY / "hp-app-check.pyz"
    pin = ASSET_DIRECTORY / "hp-app-check.sha256"
    try:
        if artifact.is_symlink() or pin.is_symlink():
            raise ValueError
        with pin.open("rb") as stream:
            pinned = stream.read(256)
        with artifact.open("rb") as stream:
            payload = stream.read(MAX_CHECKER_BYTES + 1)
        if (
            pinned != (CHECKER_SHA256 + "\n").encode()
            or len(payload) > MAX_CHECKER_BYTES
            or sha256(payload) != CHECKER_SHA256
        ):
            raise ValueError
    except (OSError, ValueError):
        raise ContractError("Preview checker artifact does not match its pin") from None
    return artifact


def run_checker(artifact: Path, manifest: Path, target: bytes) -> tuple[int, bytes]:
    command = [
        sys.executable,
        "-I",
        str(artifact),
        "--manifest",
        str(manifest),
        "--target-stdin",
        "--json",
    ]
    try:
        with subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            assert process.stdin is not None
            assert process.stdout is not None
            assert process.stderr is not None
            output = bytearray()
            errors = bytearray()
            deadline = time.monotonic() + CHECKER_TIMEOUT
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdin, selectors.EVENT_WRITE, target)
                    selector.register(process.stdout, selectors.EVENT_READ, output)
                    selector.register(process.stderr, selectors.EVENT_READ, errors)
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError
                        for key, _ in selector.select(remaining):
                            if key.fileobj is process.stdin:
                                written = os.write(key.fd, key.data)
                                if written == len(key.data):
                                    selector.unregister(process.stdin)
                                    process.stdin.close()
                                else:
                                    selector.modify(
                                        process.stdin,
                                        selectors.EVENT_WRITE,
                                        key.data[written:],
                                    )
                                continue
                            chunk = os.read(key.fd, 4096)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            key.data.extend(chunk)
                            limit = (
                                MAX_REPORT_BYTES
                                if key.fileobj is process.stdout
                                else MAX_ERROR_BYTES
                            )
                            if len(key.data) > limit:
                                raise ValueError
                status = process.wait(timeout=max(0.001, deadline - time.monotonic()))
                return status, bytes(output)
            except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired):
                process.kill()
                process.wait()
                raise ContractError(
                    "Preview checker failed or exceeded its process limits"
                ) from None
    except OSError:
        raise ContractError("Preview checker could not start") from None


def failure_summary(report: CheckerReport, target: RunningTarget) -> str:
    """Name failing checks for operators without echoing routed header values."""
    failures = []
    for item in report.findings:
        if item.passed:
            continue
        detail = item.detail
        for value in target.routed_headers.values():
            if value:
                detail = detail.replace(value, "[redacted]")
        detail = "".join(c if c.isprintable() else " " for c in detail)
        failures.append(f"{item.check} ({detail[:MAX_FINDING_DETAIL]})")
    summary = "Preview checker did not prove the final contract: " + "; ".join(failures)
    return summary[:MAX_PREVIEW_ERROR]


def check_running(
    manifest_bytes: bytes,
    manifest_sha256: str,
    target: RunningTarget,
    directory: Path,
) -> CheckerReport:
    if (
        len(manifest_bytes) > MAX_REPORT_BYTES
        or sha256(manifest_bytes) != manifest_sha256
    ):
        raise ContractError("Preview checker manifest does not match selected release")
    target_bytes = target.to_bytes()
    if len(target_bytes) > MAX_TARGET_BYTES:
        raise ContractError("Preview checker target exceeds its input limit")
    artifact = checker_artifact()
    with tempfile.TemporaryDirectory(prefix="checker-", dir=directory) as temporary:
        manifest = Path(temporary) / "manifest.json"
        manifest.write_bytes(manifest_bytes)
        manifest.chmod(0o600)
        status, payload = run_checker(artifact, manifest, target_bytes)
    try:
        report = parse_document(CheckerReport, payload)
        if report.manifest_sha256 != manifest_sha256:
            raise ValueError
    except (ContractError, ValueError):
        raise ContractError(
            "Preview checker did not prove the final contract"
        ) from None
    if not all(item.passed for item in report.findings):
        raise CheckerRejected(failure_summary(report, target))
    if status != 0:
        raise ContractError("Preview checker did not prove the final contract")
    return report

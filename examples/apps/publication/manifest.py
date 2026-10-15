"""Validate release manifests with the pinned standalone checker artifact."""

import argparse
import hashlib
import sys
from pathlib import Path

CHECKER_SHA256 = "9de8dcd53d4968cd2cf7f57ef818f41b52c756b093d4818e258d60465977c3b4"
VENDOR = Path(__file__).resolve().parent / "vendor"
PLACEHOLDERS = (b"${SOURCE_COMMIT}", b"${IMAGE}", b"${SHA256}")


def validate_manifest(raw, *, allow_placeholders=False):
    artifact = VENDOR / "hp-app-check.pyz"
    pin = VENDOR / "hp-app-check.sha256"
    if artifact.is_symlink() or pin.is_symlink():
        raise ValueError("Checker artifact must be a regular vendored file")
    with pin.open("rb") as stream:
        pinned = stream.read(256)
    with artifact.open("rb") as stream:
        payload = stream.read(2 * 1024**2 + 1)
    if (
        pinned != (CHECKER_SHA256 + "\n").encode()
        or hashlib.sha256(payload).hexdigest() != CHECKER_SHA256
    ):
        raise ValueError("Checker artifact does not match its pin")
    sys.path.insert(0, str(artifact))
    from hp_devtools.manifest import parse_manifest

    return parse_manifest(
        raw,
        template=allow_placeholders and any(marker in raw for marker in PLACEHOLDERS),
    )


def read_manifest(path, *, allow_placeholders=False):
    with path.open("rb") as stream:
        raw = stream.read(65537)
    return validate_manifest(raw, allow_placeholders=allow_placeholders)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--template", action="store_true")
    args = parser.parse_args()
    read_manifest(args.path, allow_placeholders=args.template)


if __name__ == "__main__":
    main()

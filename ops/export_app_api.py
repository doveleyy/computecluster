"""Export the app HTTP contract without loading core startup state."""

import argparse
import json
from pathlib import Path

from fastapi import FastAPI

from app.app_api import create_app_router

REFERENCE = Path(__file__).resolve().parents[1] / "docs/apps/api-reference.json"


def reference_bytes() -> bytes:
    app = FastAPI(title="Home Platform app API", version="1")
    app.include_router(create_app_router())
    schema = app.openapi()
    schema["components"]["securitySchemes"] = {
        "AppCredential": {"type": "http", "scheme": "bearer"},
        "UserContext": {
            "type": "apiKey",
            "in": "header",
            "name": "X-Platform-User-Context",
        },
    }
    for methods in schema["paths"].values():
        for operation in methods.values():
            operation["security"] = [{"AppCredential": [], "UserContext": []}]
            operation["parameters"] = [
                parameter
                for parameter in operation.get("parameters", [])
                if not (
                    parameter["in"] == "header"
                    and parameter["name"].lower()
                    in {"authorization", "x-platform-user-context"}
                )
            ]
            if not operation["parameters"]:
                del operation["parameters"]
    return (json.dumps(schema, indent=2, sort_keys=True) + "\n").encode()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=REFERENCE)
    args = parser.parse_args()
    payload = reference_bytes()
    if args.check:
        if not args.output.exists() or args.output.read_bytes() != payload:
            parser.error(f"API reference is stale: {args.output}")
        return 0
    args.output.write_bytes(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

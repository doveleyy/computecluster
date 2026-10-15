"""Export the public release manifest schema without core startup state."""

import argparse
import json
from pathlib import Path
from typing import Any

from app.app_contracts import MAX_DOCUMENT_BYTES, ReleaseManifest

REFERENCE = Path(__file__).resolve().parents[1] / "docs/apps/release-schema.json"
CONFORMANCE = Path(__file__).resolve().parents[1] / "docs/apps/release-conformance.json"


def reference_bytes() -> bytes:
    schema = ReleaseManifest.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "Home Platform application release manifest v1"
    schema["x-home-platform-contract-version"] = 1
    properties = schema["properties"]
    properties["state"] = {"$ref": "#/$defs/SQLiteState"}
    properties["capabilities"]["uniqueItems"] = True
    properties["capabilities"]["contains"] = {"const": "identity"}
    properties["secrets"]["uniqueItems"] = True
    schema["allOf"] = [
        {
            "if": {
                "required": ["workloads"],
                "properties": {"workloads": {"minItems": 1}},
            },
            "then": {"properties": {"capabilities": {"contains": {"const": "jobs"}}}},
        }
    ]
    definitions = schema["$defs"]
    for name in ("health_path", "ready_path", "version_path"):
        path = definitions["Runtime"]["properties"][name]
        path["pattern"] = r"^/(?:[A-Za-z0-9_.~-]+(?:/[A-Za-z0-9_.~-]+)*)?$"
        path["not"] = {"pattern": r"(?:^|/)\.{1,2}(?:/|$)"}
    for path in (
        definitions["SQLiteState"]["properties"]["sqlite_file"],
        definitions["Workload"]["properties"]["asset"],
    ):
        path["pattern"] = r"^[A-Za-z0-9_.~-]+$"
        path["not"] = {"enum": [".", ".."]}
    entrypoint = definitions["Workload"]["properties"]["entrypoint"]
    entrypoint["pattern"] = r"^[A-Za-z0-9_.~-]+(?:/[A-Za-z0-9_.~-]+)*$"
    entrypoint["not"] = {"pattern": r"(?:^|/)\.{1,2}(?:/|$)"}
    close_pattern_ends(schema)
    schema["x-home-platform-constraints"] = {
        "identity-required": "Every release includes the identity capability.",
        "workloads-require-jobs": "A nonempty workloads list requires jobs.",
        "unique-capabilities": "Capability names are unique.",
        "unique-workload-ids": "Workload id values are unique across workloads.",
        "unique-maintenance-names": "Maintenance name values are unique.",
        "unique-normalized-secret-names": (
            "Secret names are unique after uppercase conversion and replacing "
            "hyphens with underscores."
        ),
        "canonical-paths": (
            "Paths contain only ASCII letters, digits, underscore, dot, tilde, "
            "hyphen, and permitted separators. Segments are nonempty and never "
            "dot or dot-dot. Endpoints are absolute and may be root. Entrypoints "
            "are relative. SQLite filenames and assets are basenames."
        ),
        "state-object-when-present": "Omit state or supply an object. Null is refused.",
        "strict-integer-representation": (
            "Integer fields require JSON integer numbers. Booleans, strings, "
            "and numbers written with a fraction or exponent are refused, "
            "including mathematically integral numbers."
        ),
    }
    schema["x-home-platform-document"] = {
        "max_bytes": MAX_DOCUMENT_BYTES,
        "duplicate_keys": "refused in every object",
        "nonfinite_numbers": "refused",
    }
    return (json.dumps(schema, indent=2, sort_keys=True) + "\n").encode()


def close_pattern_ends(node: dict[str, Any] | list[Any]) -> None:
    if isinstance(node, dict):
        pattern = node.get("pattern")
        if isinstance(pattern, str) and pattern.endswith("$"):
            node["pattern"] = pattern + r"(?![\s\S])"
    children = node.values() if isinstance(node, dict) else node
    for child in children:
        if isinstance(child, dict | list):
            close_pattern_ends(child)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=REFERENCE)
    args = parser.parse_args()
    payload = reference_bytes()
    if args.check:
        if not args.output.exists() or args.output.read_bytes() != payload:
            parser.error(f"Release schema is stale: {args.output}")
        return 0
    args.output.write_bytes(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

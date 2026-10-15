import copy
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.app_contracts import (
    ContractError,
    OperatorBindings,
    Registration,
    ReleaseManifest,
    parse_document,
)
from ops.export_app_contract import CONFORMANCE, REFERENCE, reference_bytes

CORPUS = json.loads(CONFORMANCE.read_bytes())
ROOT = Path(__file__).resolve().parents[1]


def test_release_schema_is_current_and_requires_no_core_startup():
    assert REFERENCE.read_bytes() == reference_bytes() == reference_bytes()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from ops.export_app_contract import reference_bytes; "
            "import sys; reference_bytes(); "
            "assert not {'app.main', 'app.config', 'app.database'} "
            "& sys.modules.keys()",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_export_check_refuses_stale_schema_without_rewriting(tmp_path):
    target = tmp_path / "release-schema.json"
    target.write_bytes(reference_bytes())
    command = [
        sys.executable,
        "-m",
        "ops.export_app_contract",
        "--check",
        "--output",
        str(target),
    ]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    target.write_bytes(b"{}\n")
    result = subprocess.run(command, cwd=ROOT, capture_output=True, check=False)
    assert result.returncode != 0
    assert b"Release schema is stale" in result.stderr
    assert target.read_bytes() == b"{}\n"


def test_release_schema_contains_only_public_release_contract():
    schema = json.loads(reference_bytes())
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["x-home-platform-contract-version"] == 1
    assert set(schema["$defs"]) == {"Runtime", "SQLiteState", "Maintenance", "Workload"}
    assert set(schema["properties"]) == set(ReleaseManifest.model_fields)
    assert all(
        definition["additionalProperties"] is False
        for definition in [schema, *schema["$defs"].values()]
    )
    properties = schema["properties"]
    assert properties["state"] == {"$ref": "#/$defs/SQLiteState"}
    assert "state" not in schema["required"]
    assert properties["capabilities"]["uniqueItems"] is True
    assert properties["capabilities"]["contains"] == {"const": "identity"}
    assert schema["allOf"][0]["then"]["properties"]["capabilities"] == {
        "contains": {"const": "jobs"}
    }
    assert set(schema["x-home-platform-constraints"]) == {
        "identity-required",
        "workloads-require-jobs",
        "unique-capabilities",
        "unique-workload-ids",
        "unique-maintenance-names",
        "unique-normalized-secret-names",
        "canonical-paths",
        "state-object-when-present",
        "strict-integer-representation",
    }
    assert schema["x-home-platform-document"] == {
        "max_bytes": 65536,
        "duplicate_keys": "refused in every object",
        "nonfinite_numbers": "refused",
    }


@pytest.mark.parametrize("case", CORPUS["cases"], ids=lambda case: case["id"])
def test_release_conformance_corpus_matches_core_parser(case):
    payload = json.dumps(case["manifest"]).encode()
    if case["valid"]:
        result = parse_document(ReleaseManifest, payload)
        assert result.app_id == case["manifest"]["app_id"]
    else:
        with pytest.raises(ContractError):
            parse_document(ReleaseManifest, payload)


def test_release_conformance_corpus_is_versioned_and_bounded():
    assert CORPUS["corpus_version"] == CORPUS["contract_version"] == 1
    cases = CORPUS["cases"]
    assert 18 <= len(cases) <= 26
    assert len({case["id"] for case in cases}) == len(cases)
    assert sum(case["valid"] for case in cases) == 4
    assert all(set(case) == {"id", "valid", "manifest"} for case in cases)


def test_schema_endpoint_pattern_matches_canonical_path_boundary():
    schema = json.loads(reference_bytes())
    path = schema["$defs"]["Runtime"]["properties"]["health_path"]
    for value in ("/", "/health", "/checks/Ready.json"):
        assert re.search(path["pattern"], value)
        assert not re.search(path["not"]["pattern"], value)
    for value in ("health", "/health?ready=1", "/health\n", "/checks//health"):
        assert not re.search(path["pattern"], value)
    for value in ("/checks/../health", "/./health", "/.."):
        assert re.search(path["not"]["pattern"], value)

    manifest = copy.deepcopy(CORPUS["cases"][0]["manifest"])
    manifest["app_id"] += "\n"
    with pytest.raises(ContractError):
        parse_document(ReleaseManifest, json.dumps(manifest).encode())
    assert not re.search(schema["properties"]["app_id"]["pattern"], manifest["app_id"])


@pytest.mark.parametrize(
    "name",
    [
        "HP_FIXTURE_USER",
        "APP_ALLOW_DEV_IDENTITY",
        "HOME_PLATFORM_DEV_USER",
        "HP_FIXTURE_PROFILE",
        "HP_FIXTURE_",
        "PLATFORM_API_URL",
        "APP_CREDENTIAL_FILE",
        "APP_SECRET_REPORT_KEY_FILE",
    ],
)
def test_operator_registration_cannot_enable_kit_fake_identity_profile(name):
    bindings = {"settings": [{"name": name, "value": "1"}]}
    with pytest.raises(ContractError, match="Reserved setting name"):
        parse_document(OperatorBindings, json.dumps(bindings).encode())
    registration = {
        "app_id": "example-reports",
        "route": "/reports",
        "source": {
            "kind": "published",
            "repository": "example/example-reports",
            "branch": "main",
            "workflow": ".github/workflows/publish.yml",
            "image_repository": "ghcr.io/example/example-reports",
        },
        "access": "linked-user",
        "capabilities": ["identity", "jobs", "files"],
        "presentation": {
            "label": "Example reports",
            "health_name": "Example reports",
            "position": 1,
            "accent": "#123456",
        },
        "bindings": bindings,
    }
    with pytest.raises(ContractError, match="Reserved setting name"):
        parse_document(Registration, json.dumps(registration).encode())
    registration["bindings"] = {
        "settings": [{"name": "REPORTS_THEME", "value": "dark"}]
    }
    accepted = parse_document(Registration, json.dumps(registration).encode())
    assert accepted.capabilities == ("identity", "jobs", "files")
    assert accepted.bindings.settings[0].name == "REPORTS_THEME"

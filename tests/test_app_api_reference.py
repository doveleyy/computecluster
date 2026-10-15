import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from app.database import Database
from ops.export_app_api import REFERENCE, reference_bytes


def test_api_reference_is_current_and_requires_no_core_startup():
    assert REFERENCE.read_bytes() == reference_bytes()
    assert reference_bytes() == reference_bytes()
    with patch.object(Database, "initialize", side_effect=AssertionError("DB startup")):
        assert reference_bytes() == REFERENCE.read_bytes()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from ops.export_app_api import reference_bytes; "
            "import sys; reference_bytes(); "
            "assert 'app.main' not in sys.modules",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_api_reference_describes_wire_contract():
    schema = json.loads(reference_bytes())
    paths = schema["paths"]
    assert set(paths) == {
        "/api/apps/v1/user",
        "/api/apps/v1/workloads",
        "/api/apps/v1/workloads/{installation_id}/uploads",
        "/api/apps/v1/workloads/{installation_id}/file-handles",
        "/api/apps/v1/files",
        "/api/apps/v1/inputs/{input_id}",
        "/api/apps/v1/runs",
        "/api/apps/v1/runs/{run_id}",
        "/api/apps/v1/runs/{run_id}/cancel",
        "/api/apps/v1/runs/{run_id}/results",
        "/api/apps/v1/runs/{run_id}/results/{index}/{name}",
    }
    for methods in paths.values():
        for operation in methods.values():
            assert operation["security"] == [{"AppCredential": [], "UserContext": []}]
            assert not any(
                parameter["name"].lower()
                in {"authorization", "x-platform-user-context"}
                for parameter in operation.get("parameters", [])
            )
            for response in operation["responses"].values():
                if "application/json" in response.get("content", {}):
                    assert response["content"]["application/json"]["schema"] != {}
            assert operation["responses"]["422"]["content"] == {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                }
            }

    schemes = schema["components"]["securitySchemes"]
    assert schemes["AppCredential"] == {"scheme": "bearer", "type": "http"}
    assert schemes["UserContext"]["name"] == "X-Platform-User-Context"

    upload = paths["/api/apps/v1/workloads/{installation_id}/uploads"]["post"]
    assert upload["requestBody"]["content"] == {
        "application/octet-stream": {"schema": {"format": "binary", "type": "string"}}
    }
    assert "201" in upload["responses"]
    assert {"413", "415", "422"} <= set(upload["responses"])

    submit = paths["/api/apps/v1/runs"]["post"]
    assert {"200", "201", "409", "422"} <= set(submit["responses"])
    assert next(
        parameter
        for parameter in submit["parameters"]
        if parameter["name"] == "Idempotency-Key"
    )["required"]

    download = paths["/api/apps/v1/runs/{run_id}/results/{index}/{name}"]["get"]
    assert download["responses"]["200"]["content"] == {
        "application/octet-stream": {"schema": {"format": "binary", "type": "string"}}
    }
    assert "application/json" not in download["responses"]["200"]["content"]
    assert {"404", "409", "422"} <= set(download["responses"])

    for path in ("/api/apps/v1/runs", "/api/apps/v1/files"):
        query = {
            parameter["name"]: parameter["schema"]
            for parameter in paths[path]["get"]["parameters"]
            if parameter["in"] == "query"
        }
        assert query["limit"]["minimum"] == 1
        assert query["limit"]["maximum"] == 100
        assert query["offset"]["minimum"] == 0
        assert query["offset"]["maximum"] == 100000

    models = schema["components"]["schemas"]
    workloads = models["WorkloadsResponse"]
    assert "active_release_sha256" in workloads["required"]
    entries = models["FilesResponse"]["properties"]["items"]["items"]
    assert set(entries["discriminator"]["mapping"]) == {"file", "directory"}
    assert models["DirectoryResponse"]["properties"]["size_bytes"]["type"] == "null"
    assert models["FileResponse"]["properties"]["size_bytes"]["minimum"] == 0
    input_size = models["InputResponse"]["properties"]["size_bytes"]
    assert input_size["exclusiveMinimum"] == 0
    assert "404" in paths["/api/apps/v1/inputs/{input_id}"]["delete"]["responses"]

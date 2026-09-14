"""Smoke tests for the CLI.

These exist because a runtime crash in the argument parser once passed ruff,
strict mypy, and the whole suite: `argparse._SubParsersAction[Any]` type-checks
but is not subscriptable at runtime. Static analysis cannot catch that; building
the parser can.
"""

import pytest

from cli import client, render
from cli.client import build_parser


def test_parser_builds_and_documents_every_command() -> None:
    parser = build_parser()
    help_text = parser.format_help()

    # Building the parser at all is the point: this is what crashed before.
    assert "commands and their arguments:" in help_text

    # Every command must appear with its arguments, not just its name.
    assert "submit-python-batch [--dataset-storage PATH]" in help_text
    assert "submit-batch [--entrypoint ENTRYPOINT]" in help_text
    assert "--name NAME" in help_text
    assert "script [dataset]" in help_text
    assert "download [--output OUTPUT] job_id filename" in help_text

    # Argument-less commands should not leak argparse's own flag.
    assert "health [-h]" not in help_text
    assert "\n  health\n" in help_text


def test_every_subcommand_parses_and_has_its_own_help() -> None:
    parser = build_parser()
    invocations = [
        ["health"],
        ["list"],
        ["workers"],
        ["get", "job-id"],
        ["cancel", "job-id"],
        ["artifacts", "job-id"],
        ["download", "job-id", "model.joblib"],
        ["worker-enable", "mac-primary"],
        ["worker-disable", "mac-primary"],
        [
            "worker-capacity",
            "mac-primary",
            "--cpus",
            "4",
            "--memory-mb",
            "8192",
        ],
        ["submit-sleep", "5"],
        ["submit-sleep", "5", "--worker", "windows-primary"],
        ["submit-sleep-group", "120", "4", "--name", "queue test"],
        ["submit-python-batch", "a.py", "b.csv", "--name", "run"],
        [
            "submit-python-batch",
            "a.py",
            "--name",
            "stored run",
            "--dataset-storage",
            "inputs/data.csv",
        ],
        ["submit-batch", "project", "--entrypoint", "submit.hp"],
        [
            "submit-batch",
            "project",
            "--input-url",
            "cohort=https://example.com/cohort.parquet",
            "--input-sha256",
            f"cohort={'a' * 64}",
            "--input-size-bytes",
            "cohort=123",
        ],
        [
            "submit-batch",
            "project",
            "--input-storage",
            "cohort=inputs/cohort.csv",
        ],
        [
            "submit-python-batch",
            "a.py",
            "--name",
            "remote run",
            "--dataset-url",
            "https://example.com/data.csv",
            "--dataset-sha256",
            "a" * 64,
            "--dataset-size-bytes",
            "123",
        ],
    ]
    for argv in invocations:
        parsed = parser.parse_args(argv)
        assert parsed.command == argv[0]
        assert parsed.json is False


def test_json_flag_and_url_default() -> None:
    parser = build_parser()
    assert parser.parse_args(["--json", "list"]).json is True
    assert parser.parse_args(["list"]).url.startswith("http")


def test_missing_required_argument_is_rejected() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        # --name is required for python_batch
        parser.parse_args(["submit-python-batch", "a.py", "b.csv"])


def test_named_value_parser_rejects_duplicates() -> None:
    assert client.parse_named_values(["one=a", "two=b=c"], "--input") == {
        "one": "a",
        "two": "b=c",
    }
    with pytest.raises(ValueError, match="duplicate"):
        client.parse_named_values(["one=a", "one=b"], "--input")


def test_remote_dataset_uses_the_same_python_batch_contract(
    monkeypatch, capsys
) -> None:
    submitted: dict = {}

    monkeypatch.setattr(
        client,
        "load_api_token",
        lambda **_: "secret",
    )
    monkeypatch.setattr(
        client,
        "upload_file",
        lambda url, path, *, token: {
            "upload_id": "00000000-0000-0000-0000-000000000001",
            "sha256": "b" * 64,
            "size_bytes": 10,
        },
    )

    def capture_request(method, url, *, token, body=None, extra_headers=None):
        submitted.update(body or {})
        return {"id": "job-id", "status": "QUEUED"}

    monkeypatch.setattr(client, "request", capture_request)
    monkeypatch.setattr(
        "sys.argv",
        [
            "hp",
            "--json",
            "submit-python-batch",
            "train.py",
            "--name",
            "remote",
            "--dataset-url",
            "https://example.com/data.csv",
            "--dataset-sha256",
            "a" * 64,
            "--dataset-size-bytes",
            "123",
        ],
    )

    client.main()

    assert submitted["type"] == "python_batch"
    assert submitted["parameters"]["dataset"] == {
        "url": "https://example.com/data.csv",
        "sha256": "a" * 64,
        "size_bytes": 123,
    }
    assert '"status": "QUEUED"' in capsys.readouterr().out


def test_storage_dataset_uses_the_same_python_batch_contract(
    monkeypatch, capsys
) -> None:
    submitted: dict = {}
    storage_reference = {
        "storage_id": "home-storage",
        "path": "inputs/data.csv",
        "sha256": "a" * 64,
        "size_bytes": 123,
    }
    monkeypatch.setattr(client, "load_api_token", lambda **_: "secret")
    monkeypatch.setattr(
        client,
        "upload_file",
        lambda url, path, *, token: {
            "upload_id": "00000000-0000-0000-0000-000000000001",
            "sha256": "b" * 64,
            "size_bytes": 10,
        },
    )

    def capture_request(method, url, *, token, body=None, extra_headers=None):
        if url.endswith("/storage/references"):
            assert body == {"path": "inputs/data.csv"}
            return storage_reference
        submitted.update(body or {})
        return {"id": "job-id", "status": "QUEUED"}

    monkeypatch.setattr(client, "request", capture_request)
    monkeypatch.setattr(
        "sys.argv",
        [
            "hp",
            "--json",
            "submit-python-batch",
            "train.py",
            "--name",
            "stored",
            "--dataset-storage",
            "inputs/data.csv",
        ],
    )

    client.main()

    assert submitted["parameters"]["dataset"] == storage_reference
    assert '"status": "QUEUED"' in capsys.readouterr().out


def test_incomplete_remote_dataset_fails_before_upload(monkeypatch) -> None:
    monkeypatch.setattr(client, "load_api_token", lambda **_: "secret")

    def unexpected_upload(*args, **kwargs):
        raise AssertionError("validation must run before uploading")

    monkeypatch.setattr(client, "upload_file", unexpected_upload)
    monkeypatch.setattr(
        "sys.argv",
        [
            "hp",
            "submit-python-batch",
            "train.py",
            "--name",
            "incomplete",
            "--dataset-url",
            "https://example.com/data.csv",
        ],
    )

    with pytest.raises(SystemExit):
        client.main()


def test_renderers_survive_realistic_payloads() -> None:
    assert "mac-primary" in render.workers(
        [
            {
                "id": "mac-primary",
                "enabled": True,
                "state": "ONLINE",
                "supported_types": ["sleep"],
                "last_seen": "2026-09-07T06:00:00Z",
                "metrics": {"cpu_percent": 12.0, "memory_percent": 50.0},
            }
        ]
    )
    # A worker that has never reported metrics must not crash the table.
    assert "unknown-worker" in render.workers(
        [
            {
                "id": "unknown-worker",
                "enabled": False,
                "state": "STALE",
                "supported_types": [],
                "last_seen": None,
                "metrics": None,
            }
        ]
    )
    assert "(none)" in render.jobs([])
    assert "COMPLETED" in render.job(
        {
            "id": "abc",
            "name": None,
            "type": "sleep",
            "status": "COMPLETED",
            "worker_id": "mac-primary",
            "created_at": "2026-09-07T06:00:00Z",
            "attempt": 1,
            "max_attempts": 3,
            "result": {"slept_seconds": 1, "stdout": "hello\n"},
        }
    )

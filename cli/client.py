import argparse
import hashlib
import json
import os
import sys
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from cli import render
from contracts.tokens import load_api_token

RETRY_HELP = "reuse this key to retry without creating a second job"

EPILOG = """\
detail:
  Every command above takes its own arguments. Run `%(prog)s COMMAND --help`
  for the full description of any one of them.

examples:
  %(prog)s --url https://pi.example.ts.net workers
  %(prog)s worker-enable mac-primary
  %(prog)s submit-sleep 5 --name "pipeline check"
  %(prog)s submit-python-batch train.py data.csv --name "Experiment 1"
  %(prog)s submit-batch ./experiment --entrypoint submit.hp
  %(prog)s submit-batch ./experiment \\
    --input-storage data=Shared/Datasets/dataset.csv
  %(prog)s submit-python-batch train.py --name "Large run" \\
    --dataset-url https://data.example/input.csv \\
    --dataset-sha256 <sha256> --dataset-size-bytes <bytes>
  %(prog)s get 7dcf9099-4204-42d9-928e-b31929cb0a0e
  %(prog)s cancel 7dcf9099-4204-42d9-928e-b31929cb0a0e

configuration:
  --url defaults to http://127.0.0.1:8000, which is a LOCAL control plane.
  To talk to a remote one, pass --url or set HOME_PLATFORM_API_URL.
  Connection refused usually means this was forgotten.

  The API token is read from ~/.config/home-platform/api-token, or from
  HOME_PLATFORM_API_TOKEN_FILE. It is never taken as an argument, so it
  cannot leak into shell history or the process list.

if a submitted job stays QUEUED:
  Some worker must be ONLINE, enabled, and advertise that job type -- all
  three. Check with `%(prog)s workers`. Workers register disabled, so a
  freshly started one claims nothing until you enable it.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="client",
        description=(
            "Submit and inspect jobs, and control which workers may claim them."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the raw API response instead of a readable summary",
    )
    parser.add_argument(
        "--url",
        metavar="URL",
        default=os.environ.get("HOME_PLATFORM_API_URL", "http://127.0.0.1:8000"),
        help=(
            "control-plane base URL (env: HOME_PLATFORM_API_URL) [default: %(default)s]"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    subparsers.add_parser("health", help="check the control plane is alive")
    subparsers.add_parser("list", help="list all jobs, newest first")

    submit_parser = subparsers.add_parser(
        "submit-sleep",
        help="submit a do-nothing job, useful for proving the pipeline works",
    )
    submit_parser.add_argument(
        "seconds", type=int, help="how long the worker should sleep (1-300)"
    )
    submit_parser.add_argument("--name", help="human-readable job name, not unique")
    submit_parser.add_argument(
        "--worker", help="only this registered worker may claim the job"
    )
    submit_parser.add_argument("--idempotency-key", help=RETRY_HELP)

    group_parser = subparsers.add_parser(
        "submit-sleep-group",
        help="submit one test group containing multiple independently scheduled tasks",
    )
    group_parser.add_argument(
        "seconds", type=int, help="how long every child task should sleep (1-300)"
    )
    group_parser.add_argument(
        "count", type=int, help="number of child tasks to create (1-1000)"
    )
    group_parser.add_argument("--name", required=True, help="job group name")
    group_parser.add_argument("--idempotency-key", help=RETRY_HELP)

    batch_parser = subparsers.add_parser(
        "submit-python-batch",
        help="run your own Python against one input file in an isolated container",
        description=(
            "Uploads the script and either uploads a local CSV, selects a "
            "NAS file by logical path, or records a verified HTTPS reference, "
            "then creates one canonical job. "
            "The script runs in a fixed container with no network, a "
            "read-only root and no credentials. It reads the input path from "
            "HOME_PLATFORM_DATASET, may inspect HOME_PLATFORM_INPUT_DIR, and "
            "writes results to HOME_PLATFORM_OUTPUT_DIR; "
            "whatever it leaves there is published under a directory named "
            "after the job. Only a worker "
            "that already has the container image will claim this. "
            "--cpus is a hard quota, so a fraction runs the job slowly and "
            "coolly rather than just deprioritising it; library thread pools "
            "are pinned to match, and HOME_PLATFORM_CPU_LIMIT is set so your "
            "script can size its own parallelism."
        ),
    )
    batch_parser.add_argument(
        "script", type=Path, help="path to a local .py file to run"
    )
    batch_parser.add_argument(
        "dataset",
        type=Path,
        nargs="?",
        help=(
            "path to a local .csv file; omit when using --dataset-storage or "
            "--dataset-url with its checksum and size"
        ),
    )
    batch_parser.add_argument(
        "--dataset-storage",
        metavar="PATH",
        help=(
            "logical path to an existing NAS file, as Shared/... or "
            "Home/USER_ID/...; the same vocabulary the Job Desk picker uses"
        ),
    )
    batch_parser.add_argument(
        "--dataset-url",
        help="verified HTTPS dataset URL fetched directly by the chosen worker",
    )
    batch_parser.add_argument(
        "--dataset-sha256",
        help="expected lowercase SHA-256 for --dataset-url",
    )
    batch_parser.add_argument(
        "--dataset-size-bytes",
        type=int,
        help="expected byte size for --dataset-url",
    )
    batch_parser.add_argument(
        "--name", required=True, help="human-readable job name, not unique"
    )
    batch_parser.add_argument(
        "--worker",
        help="only this registered worker may claim the job; waits if unavailable",
    )
    batch_parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=1800,
        help="kill the container after this long [default: %(default)s, max 604800]",
    )
    batch_parser.add_argument(
        "--cpus",
        type=float,
        default=2.0,
        help=(
            "CPU cores to allow, 0.1-8. Fractions throttle deliberately: 0.5 runs "
            "at half a core to stay cool [default: %(default)s]"
        ),
    )
    batch_parser.add_argument(
        "--memory-mb",
        type=int,
        default=2048,
        help=(
            "hard memory limit in MiB, 256-16384; exceeding it fails the job "
            "as MEMORY_LIMIT_EXCEEDED [default: %(default)s]"
        ),
    )
    batch_parser.add_argument("--idempotency-key", help=RETRY_HELP)

    general_batch_parser = subparsers.add_parser(
        "submit-batch",
        help="package a project and submit its numeric #HP job array",
        description=(
            "Packages a directory as one immutable project, parses the PBS-like "
            "entrypoint on the coordinator, and creates one independently "
            "schedulable child for each #HP --array index."
        ),
    )
    general_batch_parser.add_argument(
        "project", type=Path, help="project directory or an existing .zip archive"
    )
    general_batch_parser.add_argument(
        "--entrypoint",
        default="submit.hp",
        help="project-relative PBS-like Bash wrapper [default: %(default)s]",
    )
    general_batch_parser.add_argument(
        "--worker", help="override #HP --worker for every array child"
    )
    general_batch_parser.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help=(
            "bind a declared logical input to a local file; repeat for multiple "
            "inputs (20 MiB maximum each)"
        ),
    )
    general_batch_parser.add_argument(
        "--input-url",
        action="append",
        default=[],
        metavar="NAME=URL",
        help="bind a declared input to a verified HTTPS URL fetched by the worker",
    )
    general_batch_parser.add_argument(
        "--input-storage",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help=(
            "bind a declared input to a file already on the NAS, as "
            "Shared/... or Home/USER_ID/...; overrides any #HP default"
        ),
    )
    general_batch_parser.add_argument(
        "--input-sha256",
        action="append",
        default=[],
        metavar="NAME=SHA256",
        help="expected lowercase SHA-256 for the matching --input-url",
    )
    general_batch_parser.add_argument(
        "--input-size-bytes",
        action="append",
        default=[],
        metavar="NAME=BYTES",
        help="expected byte size for the matching --input-url",
    )
    general_batch_parser.add_argument("--idempotency-key", help=RETRY_HELP)

    get_parser = subparsers.add_parser(
        "get", help="show one job in full, including its result"
    )
    get_parser.add_argument("job_id", help="job UUID, as printed by `list`")

    cancel_parser = subparsers.add_parser(
        "cancel",
        help="cancel a queued job or ask its worker to stop it",
        description=(
            "Queued jobs stop immediately. Running jobs stop cooperatively when "
            "the worker receives the request on its next lease heartbeat."
        ),
    )
    cancel_parser.add_argument("job_id", help="job UUID, as printed by `list`")

    subparsers.add_parser(
        "workers", help="list registered workers, their state and capabilities"
    )

    capacity_parser = subparsers.add_parser(
        "worker-capacity",
        help="set the largest single batch job a worker may accept",
        description=(
            "This is an admission and placement ceiling, not live telemetry. "
            "Targeted jobs cannot bypass it."
        ),
    )
    capacity_parser.add_argument("worker_id", help="worker ID, as printed by `workers`")
    capacity_parser.add_argument(
        "--cpus", type=float, required=True, help="maximum CPU quota, 0.1-8"
    )
    capacity_parser.add_argument(
        "--memory-mb",
        type=int,
        required=True,
        help="maximum container memory, 256-16384 MiB",
    )

    enable_parser = subparsers.add_parser(
        "worker-enable",
        help="allow a worker to claim new jobs",
        description=(
            "Changes durable state in the control plane. It does not start the "
            "worker process."
        ),
    )
    enable_parser.add_argument("worker_id", help="worker ID, as printed by `workers`")

    disable_parser = subparsers.add_parser(
        "worker-disable",
        help="stop giving a worker new jobs; it drains gracefully",
        description=(
            "The worker keeps heartbeating and finishes any job it already holds. "
            "This is a drain, not a cancel, and it does not stop the process."
        ),
    )
    disable_parser.add_argument("worker_id", help="worker ID, as printed by `workers`")

    artifacts_parser = subparsers.add_parser(
        "artifacts", help="list the files a job produced"
    )
    artifacts_parser.add_argument("job_id", help="job UUID, as printed by `list`")

    download_parser = subparsers.add_parser(
        "download",
        help="download one of a job's files",
        description="Saves into the current directory unless --output is given.",
    )
    download_parser.add_argument("job_id", help="job UUID, as printed by `list`")
    download_parser.add_argument(
        "filename", help="file name, as printed by `artifacts`"
    )
    download_parser.add_argument(
        "--output", type=Path, help="where to write it [default: ./<filename>]"
    )

    pull_parser = subparsers.add_parser(
        "pull",
        help="resume and verify every artifact produced by a job",
        description=(
            "Downloads the complete artifact manifest into a directory. Partial "
            "files are retained as .part files and resumed on the next run."
        ),
    )
    pull_parser.add_argument("job_id", help="job UUID, as printed by `list`")
    pull_parser.add_argument(
        "--destination",
        type=Path,
        default=Path("."),
        help="directory to receive the files [default: current directory]",
    )

    delete_parser = subparsers.add_parser(
        "delete",
        help="delete a job's published files",
        description=(
            "Removes the stored files. The job record itself is kept, so its "
            "history, output and file names remain visible. Nothing expires on "
            "its own, so this is how results are removed."
        ),
    )
    delete_parser.add_argument("job_id", help="job UUID, as printed by `list`")
    delete_parser.add_argument(
        "filename",
        nargs="?",
        help="one file to delete; omit to delete all of the job's files",
    )

    parser.epilog = _signatures(subparsers) + "\n" + (parser.epilog or "")
    return parser


def _signatures(subparsers: "argparse._SubParsersAction[Any]") -> str:
    """Render every command's argument signature for the top-level help.

    argparse only shows arguments one level down, so `--help` lists command
    names with no hint that each takes parameters, or that a second level of
    help exists. Generating this from the parsers themselves means it cannot
    drift out of step with the actual arguments.
    """
    lines = ["commands and their arguments:"]
    for name, sub in subparsers.choices.items():
        usage = " ".join(sub.format_usage().replace("usage:", "").split())
        usage = usage.replace(f"{sub.prog} ", "", 1).replace("[-h]", "").strip()
        lines.append(f"  {name} {usage}".rstrip())
    return "\n".join(lines) + "\n"


def download_artifact(
    base_url: str, job_id: str, filename: str, target: Path, *, token: str | None
) -> Path:
    headers = {"X-API-Token": token} if token else {}
    with httpx.stream(
        "GET",
        f"{base_url}/jobs/{job_id}/artifacts/{filename}",
        headers=headers,
        timeout=httpx.Timeout(30, read=300),
    ) as response:
        response.raise_for_status()
        with target.open("wb") as output:
            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                output.write(chunk)
    return target


def pull_artifact(
    base_url: str,
    job_id: str,
    artifact: dict[str, Any],
    destination: Path,
    *,
    token: str | None,
) -> Path:
    filename = str(artifact["filename"])
    expected_size = int(artifact["size_bytes"])
    expected_sha256 = str(artifact["sha256"])
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / filename
    partial = destination / f".{filename}.part"
    offset = partial.stat().st_size if partial.is_file() else 0
    if offset > expected_size:
        partial.unlink()
        offset = 0
    headers = {"X-API-Token": token} if token else {}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    with httpx.stream(
        "GET",
        f"{base_url}/jobs/{job_id}/artifacts/{filename}",
        headers=headers,
        timeout=httpx.Timeout(30, read=300),
    ) as response:
        response.raise_for_status()
        resumed = offset > 0 and response.status_code == 206
        mode = "ab" if resumed else "wb"
        with partial.open(mode) as output:
            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                output.write(chunk)
    if partial.stat().st_size != expected_size:
        raise RuntimeError(
            f"downloaded size for {filename!r} does not match its manifest"
        )
    digest = hashlib.sha256()
    with partial.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise RuntimeError(f"SHA-256 verification failed for {filename!r}")
    partial.replace(target)
    return target


def request(
    method: str,
    url: str,
    *,
    token: str | None,
    body: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    headers = {"X-API-Token": token} if token else {}
    headers.update(extra_headers or {})
    response = httpx.request(method, url, headers=headers, json=body, timeout=10)
    response.raise_for_status()
    return response.json()


def upload_file(url: str, path: Path, *, token: str | None) -> Any:
    headers = {"X-API-Token": token} if token else {}
    with path.open("rb") as source:
        response = httpx.post(
            url,
            headers=headers,
            files={"file": (path.name, source)},
            timeout=30,
        )
    response.raise_for_status()
    return response.json()


def package_project(source: Path, target: Path) -> Path:
    if source.is_file():
        if source.suffix.lower() != ".zip":
            raise ValueError("project file must be a .zip archive")
        return source
    if not source.is_dir():
        raise ValueError(f"project path does not exist: {source}")
    files = sorted(path for path in source.rglob("*") if path.is_file())
    if len(files) > 1000:
        raise ValueError("project contains more than 1000 files")
    if any(path.is_symlink() for path in source.rglob("*")):
        raise ValueError("project directories containing symlinks are not supported")
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(source).as_posix())
    return target


def parse_named_values(values: list[str], option: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for binding in values:
        if "=" not in binding:
            raise ValueError(f"{option} must use NAME=VALUE")
        name, value = binding.split("=", 1)
        if not name or not value or name in parsed:
            raise ValueError(f"invalid or duplicate {option} name: {name!r}")
        parsed[name] = value
    return parsed


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    base_url = args.url.rstrip("/")
    token_file = Path(
        os.environ.get(
            "HOME_PLATFORM_API_TOKEN_FILE",
            Path.home() / ".config/home-platform/api-token",
        )
    )
    token = load_api_token(default_file=token_file)
    renderer: Callable[[Any], str] | None = None

    if args.command == "health":
        result = request("GET", f"{base_url}/health", token=token)
        renderer = render.status
    elif args.command == "list":
        result = request("GET", f"{base_url}/jobs", token=token)
        renderer = render.jobs
    elif args.command == "submit-sleep":
        result = request(
            "POST",
            f"{base_url}/jobs",
            token=token,
            body={
                "name": args.name,
                "target_worker_id": args.worker,
                "type": "sleep",
                "parameters": {"seconds": args.seconds},
            },
            extra_headers=(
                {"Idempotency-Key": args.idempotency_key}
                if args.idempotency_key
                else None
            ),
        )
        renderer = render.submitted
    elif args.command == "submit-sleep-group":
        if not 1 <= args.count <= 1000:
            parser.error("submit-sleep-group count must be between 1 and 1000")
        result = request(
            "POST",
            f"{base_url}/job-groups",
            token=token,
            body={
                "name": args.name,
                "tasks": [
                    {
                        "task_id": f"task-{index + 1:03d}",
                        "job": {
                            "name": f"{args.name} / task {index + 1}",
                            "type": "sleep",
                            "parameters": {"seconds": args.seconds},
                        },
                    }
                    for index in range(args.count)
                ],
            },
            extra_headers=(
                {"Idempotency-Key": args.idempotency_key}
                if args.idempotency_key
                else None
            ),
        )
        renderer = render.submitted
    elif args.command == "submit-python-batch":
        remote_dataset_fields = (
            args.dataset_url,
            args.dataset_sha256,
            args.dataset_size_bytes,
        )
        if args.dataset is not None:
            if args.dataset_storage is not None or any(
                value is not None for value in remote_dataset_fields
            ):
                parser.error(
                    "submit-python-batch accepts either a local dataset or "
                    "--dataset-storage or the complete verified URL fields, not "
                    "more than one source"
                )
            dataset = upload_file(
                f"{base_url}/uploads/datasets", args.dataset, token=token
            )
        elif args.dataset_storage is not None:
            if any(value is not None for value in remote_dataset_fields):
                parser.error(
                    "--dataset-storage cannot be combined with verified URL fields"
                )
            dataset = request(
                "POST",
                f"{base_url}/storage/references",
                token=token,
                body={"path": args.dataset_storage},
            )
        else:
            if any(value is None for value in remote_dataset_fields):
                parser.error(
                    "submit-python-batch requires a local dataset or all of "
                    "--dataset-url, --dataset-sha256, and --dataset-size-bytes"
                )
            dataset = {
                "url": args.dataset_url,
                "sha256": args.dataset_sha256,
                "size_bytes": args.dataset_size_bytes,
            }
        script = upload_file(f"{base_url}/uploads/scripts", args.script, token=token)
        result = request(
            "POST",
            f"{base_url}/jobs",
            token=token,
            body={
                "name": args.name,
                "target_worker_id": args.worker,
                "type": "python_batch",
                "parameters": {
                    "script": script,
                    "dataset": dataset,
                    "timeout_seconds": args.timeout_seconds,
                    "cpu_limit": args.cpus,
                    "memory_mb": args.memory_mb,
                },
            },
            extra_headers=(
                {"Idempotency-Key": args.idempotency_key}
                if args.idempotency_key
                else None
            ),
        )
        renderer = render.submitted
    elif args.command == "submit-batch":
        try:
            with tempfile.TemporaryDirectory(
                prefix="home-platform-project-"
            ) as temporary:
                archive = package_project(args.project, Path(temporary) / "project.zip")
                project = upload_file(
                    f"{base_url}/uploads/projects", archive, token=token
                )
        except ValueError as error:
            parser.error(str(error))
        try:
            local_inputs = parse_named_values(args.input, "--input")
            input_urls = parse_named_values(args.input_url, "--input-url")
            storage_inputs = parse_named_values(args.input_storage, "--input-storage")
            input_hashes = parse_named_values(args.input_sha256, "--input-sha256")
            input_sizes = parse_named_values(
                args.input_size_bytes, "--input-size-bytes"
            )
        except ValueError as error:
            parser.error(str(error))
        if set(input_urls) != set(input_hashes) or set(input_urls) != set(input_sizes):
            parser.error(
                "every --input-url name requires matching --input-sha256 and "
                "--input-size-bytes values"
            )
        overlap = (
            (set(local_inputs) & set(input_urls))
            | (set(local_inputs) & set(storage_inputs))
            | (set(input_urls) & set(storage_inputs))
        )
        if overlap:
            parser.error(
                "an input name cannot use more than one binding source: "
                + ", ".join(sorted(overlap))
            )
        inputs: dict[str, Any] = {}
        for name, raw_path in local_inputs.items():
            input_path = Path(raw_path)
            if not input_path.is_file():
                parser.error(f"input file does not exist: {input_path}")
            inputs[name] = upload_file(
                f"{base_url}/uploads/inputs", input_path, token=token
            )
        for name, url in input_urls.items():
            try:
                size_bytes = int(input_sizes[name])
            except ValueError:
                parser.error(f"--input-size-bytes for {name!r} must be an integer")
            inputs[name] = {
                "url": url,
                "sha256": input_hashes[name],
                "size_bytes": size_bytes,
            }
        for name, path in storage_inputs.items():
            inputs[name] = request(
                "POST",
                f"{base_url}/storage/references",
                token=token,
                body={"path": path},
            )
        result = request(
            "POST",
            f"{base_url}/batch-submissions",
            token=token,
            body={
                "project": project,
                "entrypoint": args.entrypoint,
                "target_worker_id": args.worker,
                "inputs": inputs,
            },
            extra_headers=(
                {"Idempotency-Key": args.idempotency_key}
                if args.idempotency_key
                else None
            ),
        )
        renderer = render.submitted
    elif args.command == "workers":
        result = request("GET", f"{base_url}/workers", token=token)
        renderer = render.workers
    elif args.command == "cancel":
        result = request("POST", f"{base_url}/jobs/{args.job_id}/cancel", token=token)
        renderer = render.cancelled
    elif args.command == "worker-capacity":
        result = request(
            "PUT",
            f"{base_url}/workers/{args.worker_id}/capacity",
            token=token,
            body={
                "max_job_cpu": args.cpus,
                "max_job_memory_mb": args.memory_mb,
            },
        )
        renderer = render.worker_updated
    elif args.command in {"worker-enable", "worker-disable"}:
        result = request(
            "PATCH",
            f"{base_url}/workers/{args.worker_id}",
            token=token,
            body={"enabled": args.command == "worker-enable"},
        )
        renderer = render.worker_updated
    elif args.command == "artifacts":
        result = request("GET", f"{base_url}/jobs/{args.job_id}/artifacts", token=token)
        renderer = render.artifacts
    elif args.command == "download":
        target = args.output or Path(args.filename)
        written = download_artifact(
            base_url, args.job_id, args.filename, target, token=token
        )
        result = {
            "filename": args.filename,
            "path": str(written.resolve()),
            "size_bytes": written.stat().st_size,
        }
        renderer = lambda payload: (  # noqa: E731
            f"{render.GREEN}Downloaded{render.RESET} {payload['path']} "
            f"({payload['size_bytes'] / 1024:.1f} KiB)"
        )
    elif args.command == "pull":
        manifest = request(
            "GET", f"{base_url}/jobs/{args.job_id}/artifacts", token=token
        )
        pulled_files = [
            pull_artifact(
                base_url,
                args.job_id,
                artifact,
                args.destination,
                token=token,
            )
            for artifact in manifest
        ]
        result = {
            "job_id": args.job_id,
            "directory": str(args.destination.resolve()),
            "files": [str(path.resolve()) for path in pulled_files],
        }
        renderer = lambda payload: (  # noqa: E731
            f"{render.GREEN}Downloaded and verified{render.RESET} "
            f"{len(payload['files'])} file(s) into {payload['directory']}"
        )
    elif args.command == "delete":
        path = f"{base_url}/jobs/{args.job_id}/artifacts"
        if args.filename:
            path = f"{path}/{args.filename}"
        result = request("DELETE", path, token=token)
        renderer = render.deleted
    else:
        result = request("GET", f"{base_url}/jobs/{args.job_id}", token=token)
        renderer = render.job

    if args.json or renderer is None:
        print(json.dumps(result, indent=2))
    else:
        print(renderer(result))


def run() -> None:
    """Entry point that reports API and network errors as messages, not tracebacks.

    A 404 or a refused connection is an ordinary outcome of using a command-line
    tool, not a bug worth a stack trace. The exit code still distinguishes
    failure so scripts can branch on it.
    """
    try:
        main()
    except httpx.HTTPStatusError as error:
        detail = ""
        try:
            detail = error.response.json().get("detail", "")
        except Exception:
            detail = error.response.text[:200]
        code = error.response.status_code
        hint = {
            401: "  check the API token file is present and readable",
            404: "  check the ID with `client list`",
            409: "  the lease or idempotency key conflicts with existing state",
            413: "  the file exceeds the configured size limit",
        }.get(code, "")
        print(f"{render.RED}error {code}{render.RESET}: {detail}", file=sys.stderr)
        if hint:
            print(f"{render.DIM}{hint}{render.RESET}", file=sys.stderr)
        raise SystemExit(1) from None
    except httpx.RequestError as error:
        print(
            f"{render.RED}cannot reach the control plane{render.RESET}: {error}",
            file=sys.stderr,
        )
        print(
            f"{render.DIM}  is --url correct? it defaults to localhost{render.RESET}",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    run()

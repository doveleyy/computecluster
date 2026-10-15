"""Record operator metadata and JSON release bytes without deploying apps."""

import argparse
import asyncio
import json
import re
import secrets
import shlex
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

import httpx

from app.app_contracts import (
    MAX_DOCUMENT_BYTES,
    ContractError,
    Registration,
    parse_document,
)
from app.app_deployments import (
    provision_legacy,
    restore_unaccepted_legacy,
    update_registration,
)
from app.app_discovery import ReleaseDiscovery
from app.app_github import GitHubPublications
from app.app_previews import PreviewService
from app.app_registry import AppRegistry
from app.app_runtime import docker_run_argv, runtime_spec
from app.app_workloads import WorkloadStore
from app.database import Database
from app.migrations import MIGRATIONS


def read_document(path: Path) -> bytes:
    with path.open("rb") as source:
        return source.read(MAX_DOCUMENT_BYTES + 1)


def schema_version(database: Database) -> int:
    with database.connect() as connection:
        version: int = connection.execute(
            "SELECT coalesce(max(version), 0) FROM schema_migrations"
        ).fetchone()[0]
    return version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser(
        "register", help="record an operator JSON definition"
    )
    register.add_argument("definition", type=Path)
    update = commands.add_parser(
        "update-registration",
        help="update operator bindings, or propose them for an installed app",
    )
    update.add_argument("definition", type=Path)
    provision = commands.add_parser(
        "provision-legacy",
        help="replace an unaccepted legacy registration with a managed one",
    )
    provision.add_argument("definition", type=Path)
    provision.add_argument("--expected-legacy-sha256", required=True)
    restore = commands.add_parser(
        "restore-legacy",
        help="restore the legacy registration before any accepted install",
    )
    restore.add_argument("definition", type=Path)
    restore.add_argument("--expected-legacy-sha256", required=True)
    restore.add_argument("--expected-managed-sha256", required=True)
    ingest = commands.add_parser(
        "ingest", help="retain exact version 1 JSON manifest bytes"
    )
    ingest.add_argument("app_id")
    ingest.add_argument("manifest", type=Path)
    install = commands.add_parser(
        "install-workload", help="install a digest-verified workload archive"
    )
    install.add_argument("app_id")
    install.add_argument("release_sha256")
    install.add_argument("workload_id")
    install.add_argument("archive", type=Path)
    commands.add_parser("list", help="list safe application metadata")
    discover = commands.add_parser(
        "check-updates", help="verify releases from registered workflows"
    )
    discover.add_argument("app_id", nargs="?")
    discover.add_argument("--token-file", type=Path)
    preview = commands.add_parser(
        "preview", help="select or retry an exact verified release"
    )
    preview.add_argument("app_id")
    preview.add_argument("release_sha256")
    status = commands.add_parser(
        "preview-status", help="reconcile an existing staging attempt"
    )
    status.add_argument("attempt_id", type=UUID)
    retire = commands.add_parser(
        "retire-preview", help="retire an attempt before replacement"
    )
    retire.add_argument("attempt_id", type=UUID)
    for command in (preview, status, retire):
        command.add_argument("--origin", required=True)
        command.add_argument("--control-token-file", type=Path, required=True)
    credential = commands.add_parser(
        "credential", help="issue an application credential file"
    )
    credential.add_argument("app_id")
    credential.add_argument("--rotate", action="store_true")
    credential.add_argument(
        "--file",
        type=Path,
        help="credential file to create (default: a fresh name under the "
        "database directory's app-credentials)",
    )
    runtime = commands.add_parser(
        "runtime", help="print a hardened Docker command without starting it"
    )
    runtime.add_argument("app_id")
    runtime.add_argument("--image", required=True)
    runtime.add_argument("--port", required=True, type=int)
    runtime.add_argument("--uid", type=int, default=1000)
    runtime.add_argument("--gid", type=int, default=1000)
    args = parser.parse_args()
    database = Database(args.database)
    try:
        if not args.database.is_file() or schema_version(database) < MIGRATIONS[-1][0]:
            print(
                "Application metadata database is missing or not migrated; "
                "start the control plane to migrate it",
                file=sys.stderr,
            )
            return 2
        registry = AppRegistry(database)
        result: object
        if args.command in {"preview", "preview-status", "retire-preview"}:

            async def request_preview() -> object:
                async with httpx.AsyncClient(trust_env=False) as client:
                    previews = PreviewService(
                        database,
                        client,
                        args.origin,
                        args.control_token_file.read_text().strip(),
                    )
                    if args.command == "preview":
                        response = await previews.preview(
                            args.app_id, args.release_sha256
                        )
                    elif args.command == "preview-status":
                        response = await previews.status(args.attempt_id)
                    else:
                        response = await previews.retire(args.attempt_id)
                    return response.model_dump(mode="json")

            result = asyncio.run(request_preview())
        elif args.command == "check-updates":

            async def check_updates() -> object:
                token = args.token_file.read_text().strip() if args.token_file else None
                async with httpx.AsyncClient(trust_env=False) as client:
                    discovery = ReleaseDiscovery(
                        database, GitHubPublications(client, token)
                    )
                    if args.app_id:
                        return asdict(await discovery.discover(args.app_id))
                    return [asdict(item) for item in await discovery.all()]

            result = asyncio.run(check_updates())
        elif args.command == "update-registration":
            result = {
                "outcome": update_registration(database, read_document(args.definition))
            }
        elif args.command == "provision-legacy":
            result = {
                "outcome": provision_legacy(
                    database,
                    parse_document(Registration, read_document(args.definition)),
                    expected_legacy_sha256=args.expected_legacy_sha256,
                )
            }
        elif args.command == "restore-legacy":
            result = {
                "outcome": restore_unaccepted_legacy(
                    database,
                    parse_document(Registration, read_document(args.definition)),
                    expected_legacy_sha256=args.expected_legacy_sha256,
                    expected_managed_sha256=args.expected_managed_sha256,
                )
            }
        elif args.command == "register":
            result = {"outcome": registry.register(read_document(args.definition))}
        elif args.command == "ingest":
            result = asdict(
                registry.ingest_release(args.app_id, read_document(args.manifest))
            )
        elif args.command == "credential":
            if registry.managed_app(args.app_id) is None:
                raise ContractError("Application is not a managed installation")
            path = args.file or (
                args.database.resolve().parent
                / "app-credentials"
                / (args.app_id + "-" + secrets.token_hex(16))
            )
            result = asdict(
                registry.issue_credential(args.app_id, path, rotate=args.rotate)
            )
        elif args.command == "install-workload":
            result = (
                WorkloadStore(database)
                .install(
                    args.app_id, args.release_sha256, args.workload_id, args.archive
                )
                .public()
            )
        elif args.command == "runtime":
            if (
                not 1 <= args.port <= 65535
                or not 1 <= args.uid <= 2**31 - 1
                or not 1 <= args.gid <= 2**31 - 1
            ):
                raise ContractError("Runtime port and non-root user IDs are invalid")
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/@-]{0,334}", args.image):
                raise ContractError("Invalid runtime image reference")
            app = registry.managed_app(args.app_id)
            active = registry.active_credential(args.app_id)
            instance = registry.instance()
            if app is None or active is None or instance is None:
                raise ContractError(
                    "Managed registration, instance and active credential are required"
                )
            if not active.file_path.is_file():
                raise ContractError(
                    "Active credential file is missing; issue an explicit rotation"
                )
            print(
                shlex.join(
                    docker_run_argv(
                        runtime_spec(app, active, instance, args.port),
                        args.image,
                        (args.uid, args.gid),
                    )
                )
            )
            return 0
        else:
            result = [asdict(item) for item in registry.summaries()]
        print(json.dumps(result, sort_keys=True))
        return 0
    except ContractError as error:
        print(str(error), file=sys.stderr)
        return 2
    except OSError:
        print("Application metadata file could not be read", file=sys.stderr)
        return 2
    except sqlite3.Error:
        print("Application metadata database operation failed", file=sys.stderr)
        return 2
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())

# Home Platform

Home Platform is the core and organizer for a private computing platform. It
brings independently published applications, shared files, and borrowed compute
under one identity system and browser entry point.

This repository owns the launcher, accounts, Job Desk, Files, scheduler, worker
software, and application integration. Independent applications own their
business logic, dependencies, releases, and private state in separate
repositories.

## The design

A small coordinator keeps durable state while workers connect outward to claim
jobs. A worker can disappear without losing the queue. Each claim carries a
renewable lease, and only its current holder may publish results.

Applications connect through a defined contract. The core routes authenticated
requests, supplies scoped platform APIs, and manages approved installation.
Each application runs in its own container with its own database, credentials,
resource limits, and backup lifecycle.

The main boundaries are deliberate.

- Private network access provides reachability. Accounts, roles, and immutable
  ownership decide who may read or change data.
- The coordinator owns job state and published results. Workers execute bounded
  jobs and verify downloaded inputs by size and digest.
- Applications use supported APIs rather than importing core internals or
  opening its database.
- Live databases stay on local disk. Shared files and verified backups can live
  on a file server with separate filesystem permissions.

See [Architecture](docs/architecture.md) for the component diagram, request
paths, and limits of these boundaries.

## Read the documentation

| Goal | Start here |
|---|---|
| Understand the system | [Architecture](docs/architecture.md) |
| Understand trust and routing | [Network](docs/network.md) and [access control](docs/access-control.md) |
| Build an independent application | [Application integration](docs/apps/README.md) |
| Submit a Python job | [Python script guide](docs/compute/python-script.md) |
| Submit a project or job array | [Batch project guide](docs/compute/batch-script.md) |
| Work with inputs and results | [File workflow](docs/storage/workflow.md) |
| Look up configuration or protocols | [Documentation index](docs/README.md) |

Author guides and machine-readable contracts are public so a developer can
write jobs or applications without a copy of the deployment configuration.
Host inventories, credentials, operational runbooks, and acceptance records
remain private.

## Repository layout

```text
app/         core API, persistence, migrations, and browser interfaces
worker/      job claiming, input cache, telemetry, and container execution
contracts/   shared wire models used by the core, worker, and client
cli/         command-line client
ops/         registration, release, installation, and backup tools
containers/  batch execution image
examples/    job projects and independent application examples
docs/        architecture, author guides, and supported contracts
tests/       automated checks
```

The worker and client import shared contracts without importing the core.
Application implementations are developed outside this tree.

## Local development

```bash
pixi run dev
pixi run check
```

The development server listens on loopback. Its interactive API reference is
at `/docs`. `/health` reports process liveness. `/ready` also checks database
access. The check task runs lint, format, type, and test checks.

`pixi run client --help` lists client commands. Worker settings and the separate
worker environment are described in [Configuration](docs/configuration.md).

The design uses one coordinator and one database writer. It does not provide
high availability. [Architecture](docs/architecture.md#failure-and-capacity)
describes recovery behavior and the remaining capacity limits.

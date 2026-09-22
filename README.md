# Home Platform

A small distributed job system for a home network. One always-on coordinator
owns job state; laptops join as compute workers when they happen to be
available, and queued work waits when none are.

Built to explore the parts of distributed systems that are easy to describe and
hard to get right: atomic work claiming, lease-based failure recovery, verifying
data you did not produce, and running untrusted code without trusting it.

## What it does

- **Durable job queue** — SQLite is the single source of truth. Workers mutate
  state only through the API.
- **Lease-based recovery** — a claimed job carries a renewable lease. If a
  worker sleeps or dies, the job is requeued automatically, bounded by an
  attempt limit so a poisonous job fails instead of looping forever.
- **Atomic claiming** — a single conditional update means exactly one worker
  wins a job, even under contention.
- **Content-addressed data plane** — large datasets go straight to the worker
  that needs them, verified by size and SHA-256 and cached by digest. They never
  travel through the coordinator or sit in job rows.
- **Isolated execution** — user-supplied Python runs in a fixed, pre-built
  container with no network, a read-only root, dropped capabilities, a non-root
  user, and CPU/memory/PID/time limits. The host agent never imports it.
- **PBS-style numeric arrays** — one bounded project contains a strict `#HP`
  Bash wrapper and its child scripts. An inclusive array range becomes one
  durable parent plus independently placed, retried, and collected children.
  Named inputs may be small uploads or verified HTTPS files downloaded directly
  and cached by each selected worker. Projects and inputs already on the NAS are
  selected by logical `Home/...` or `Shared/...` path — the same vocabulary in
  Job Desk, the CLI, and `#HP` defaults, for both job types.
- **Result publishing** — a worker uploads its output files to the coordinator
  under the same lease that authorises completion, so a revived worker cannot
  overwrite its replacement's results. The coordinator, not the script, decides
  where they land: one directory per submission, named after the job, with array
  children nested inside it. Files are then downloadable and can be previewed or
  downloaded from Job Desk, or exposed read-only to a file share.
- **Deliberate throttling** — `cpu_limit` is a hard quota, not a priority, and
  accepts fractions. A long search at 0.5 CPU runs slowly and coolly on a laptop
  you are still using. Library thread pools are pinned to the quota, without
  which a throttled job oversubscribes its own limit and runs several times
  slower for the same CPU budget.
- **Operator control** — workers register scheduling-disabled and are enabled
  deliberately, from a web dashboard or the CLI. Disabling drains gracefully
  rather than cancelling running work.
- **Single NAS storage boundary** — Synology is the sole household SMB service
  and the application backend for Home, Shared, and owner-scoped artifacts.
  Any removable storage attached to the Pi is local-only and is never
  advertised as a second file share.
- **Purposeful web navigation** — monitoring, operator controls, job history,
  and job submission have focused routes instead of one oversized dashboard or
  job page.
- **User-scoped Job Desk** — members sign in with individual credentials and
  can access only their own jobs, groups, staged uploads, and artifacts. The
  owner retains worker, host, and account controls. Members can change their
  password, while an owner reset revokes every existing member session.
- **Guarded Pi power control** — the authenticated owner dashboard can request
  reboot or shutdown only after all workers are drained and jobs are idle. A
  root-owned helper flushes writes and unmounts local removable storage before
  changing power state.
- **Cooperative cancellation** — queued work stops immediately; a running
  container receives cancellation through its lease heartbeat and is removed
  without conflating the outcome with timeout or memory exhaustion.

## Interfaces

The browser UI has five focused routes: owner Overview, owner Operations, job
history/results, Files, and Submit. They retain one terminal-inspired visual language
and one responsive 1240 px content shell without forcing monitoring,
destructive controls, forms, and history into one screen. Submission uses a
roomy two-column form on larger screens and a single-column phone layout.

### Homelab Dashboard

![Homelab Dashboard showing service health and compute workers](docs/assets/dashboard.png)

### Job Desk

![Job Desk showing batch submission and synthetic job history](docs/assets/job-desk.png)

The screenshots use synthetic host, worker, job, and timestamp values so the
public repository does not disclose details of the live deployment.

## Documentation

- [Architecture](docs/architecture.md) — design, job lifecycle, leases,
  scheduling behaviour, isolation model, trust boundaries, failure behaviour.
- [Job and API contract](docs/job-contract.md) — job types, state machine,
  worker protocol, endpoints, authentication.
- [Job types and authoring](docs/jobs/README.md) — separate guides for the live
  [Python script job](docs/jobs/python-script.md) and numeric-array
  [PBS-like batch script](docs/jobs/batch-script.md).
- [Web interfaces](docs/interfaces.md) — what the dashboard and Job Desk do
  today, and the boundary for their next redesign.
- [Configuration](docs/configuration.md) — control-plane, storage, worker, and
  execution settings.
- [Accounts and access control](docs/access-control.md) — roles, sessions,
  immutable ownership, and the pending NAS ACL boundary.

## Layout

```text
contracts/   the shared wire contract — the only code both sides import
app/         control plane: HTTP API, persistence, migrations, web interfaces
worker/      worker agent: claiming, telemetry, dataset cache, container launcher
cli/         operator client
containers/  pinned container image definition for batch execution
examples/    model-training and PBS-style numeric-array examples
tests/       test suite
```

`contracts/` exists so a worker deployment does not drag control-plane code
along with it. Nothing in `app/` imports `worker/`, and neither `worker/` nor
`cli/` imports `app/`.

## Development

```bash
pixi run dev      # API on localhost, interactive docs at /docs
pixi run check    # lint, format, strict type check, tests
```

`/health` reports process liveness; `/ready` also verifies the database is
reachable.

## Running a worker

```bash
HOME_PLATFORM_API_URL=<control-plane-url> \
HOME_PLATFORM_WORKER_ID=<worker-name> \
pixi run -e worker worker
```

The lock includes native x86-64 Linux workers. A Linux worker can run under a
systemd user service, starts with that user's login, registers
scheduling-disabled, and advertises container job types only after the approved
runtime image is actually available.

A worker advertises the batch capability only once it can see the pre-built
container image, re-checking periodically — so capability appears and
disappears on its own, without restarts.

The retired CSV Summary handler is no longer submittable or advertised. The
single-file `python_batch` remains available while the compute layer evolves
toward the general PBS-style batch design described in the
[architecture](docs/architecture.md) and its normative
[batch script standard](docs/jobs/batch-script.md).

## Client

```bash
export HOME_PLATFORM_API_URL=<control-plane-url>

pixi run client workers                       # readable table; --json to pipe
pixi run client worker-capacity <worker-name> --cpus 4 --memory-mb 4096
pixi run client worker-enable <worker-name>
pixi run client submit-python-batch script.py data.csv --name "Training run" \
  --worker <worker-name> --cpus 0.5 --timeout-seconds 86400
# Or keep a large dataset off the coordinator:
pixi run client submit-python-batch script.py --name "Large training run" \
  --dataset-url <https-url> --dataset-sha256 <sha256> \
  --dataset-size-bytes <bytes> --timeout-seconds 604800
# The same direct-input pattern works for a PBS-style project array:
pixi run client submit-batch ./experiment \
  --input-url data=<https-url> --input-sha256 data=<sha256> \
  --input-size-bytes data=<bytes>
# Or bind a file already on the NAS, by logical path:
pixi run client submit-batch ./experiment \
  --input-storage data=Shared/Datasets/dataset.csv
pixi run client submit-python-batch script.py --name "From the NAS" \
  --dataset-storage Shared/Datasets/dataset.csv
pixi run client list
pixi run client cancel <job-id>
pixi run client artifacts <job-id>            # what the job produced
pixi run client download <job-id> model.joblib
pixi run client pull <job-id> --destination ./results  # resume + verify all
pixi run client delete <job-id>               # remove its files; job record stays
```

Output is human-readable by default and raw JSON behind `--json`.
`pixi run client --help` lists every command with its arguments.

## Status and scope

A working personal system, not a product. It runs a single coordinator with a
single database writer and is deliberately not highly available.

Known gaps, in the order they will start to matter: automatic placement uses
fixed per-node capacity rather than live load, thermal, or data-locality policy;
artifact publication still passes through the coordinator in one bounded
request per file; and browser downloads do not expose the CLI's resumable
manifest workflow. Worker input caches now have a least-recently-used size
ceiling, and CLI artifact pulls resume by byte range and verify SHA-256. See
[Architecture](docs/architecture.md) for why each is currently adequate and
when it stops being so.

Deployment specifics — hosts, addresses, accounts, machine inventory, and
operational runbooks — are intentionally kept out of public version control.

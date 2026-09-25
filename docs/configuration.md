# Configuration

Every setting is an environment variable. There is no configuration file format
to learn, and no setting that can only be changed in code.

Values below are defaults. Paths are relative to the process working directory
unless absolute.

## Control plane

| Variable | Default | Purpose |
|---|---|---|
| `HOME_PLATFORM_DB_PATH` | `data/home-platform.db` | SQLite database — the source of job truth |
| `HOME_PLATFORM_API_TOKEN` | unset | Token value. Prefer the file form below |
| `HOME_PLATFORM_API_TOKEN_FILE` | unset | Path to a file containing the token. Preferred: a value in the environment is visible in the process list |
| `HOME_PLATFORM_SERVICE_IDENTITY_TOKEN_FILE` | unset | Separate token file allowing local application services to resolve linked external identities; never reuse the elevated API token |
| `HOME_PLATFORM_LEASE_SECONDS` | `15` | How long a claimed job's lease lasts before it must be renewed |
| `HOME_PLATFORM_WORKER_STALE_SECONDS` | `20` | Silence after which a worker is reported `STALE` |
| `HOME_PLATFORM_RECOVERY_INTERVAL_SECONDS` | `2` | How often expired leases are swept and requeued |
| `HOME_PLATFORM_MAX_ATTEMPTS` | `3` | Requeues before a job is failed permanently |
| `HOME_PLATFORM_POWER_REQUEST_DIR` | unset | Runtime marker directory watched by the optional root-owned Pi power units |

If no token is configured the API is unauthenticated. That is only appropriate
for local development.

The configured API token also signs browser sessions and bootstraps the
administrator login. Member passwords are stored as salted scrypt hashes in
SQLite and are created from the authenticated Dashboard; they are not
environment variables. Rotating the API token invalidates every browser session
as well as CLI and worker credentials, so it must be treated as a coordinated
credential rotation.

Power control remains disabled when `HOME_PLATFORM_POWER_REQUEST_DIR` is unset.
On the Pi it points to a volatile systemd runtime directory; it must never point
to user-controlled persistent storage. Power requests are also refused unless
authentication is configured, every worker is scheduling-disabled, and no job
is running.

`LEASE_SECONDS` is the interesting one: too short and a briefly-paused worker
loses its job; too long and a dead worker's job sits idle before recovery. It
must comfortably exceed the worker's heartbeat interval.

Database backup is an operator service rather than an API background task.
`python -m ops.backup_sqlite <database> <private-destination> --retain 14`
uses SQLite's online backup API, verifies the copy locally, publishes immutable
bytes plus a SHA-256 sidecar, and removes older sets. The production systemd
unit supplies its destination through `HOME_PLATFORM_BACKUP_DIR`; keep that
path in live-only configuration because it may contain an owner storage key.
NAS snapshots and a second off-device copy are separate layers.

## Habit Tracker

The Habit Tracker is a hosted application service using the
`services.habit_tracker` package and only the `HABIT_TRACKER_` environment
prefix. Its private pages are served under `/habits` by default. See
[Application services](services.md) for the hosting pattern.

| Variable | Default | Purpose |
|---|---|---|
| `HABIT_TRACKER_DB_PATH` | `data/habit-tracker.db` | Shared Water and Budget SQLite file |
| `HABIT_TRACKER_BASE_PATH` | `/habits` | External proxy prefix; `/` for direct local development |
| `HABIT_TRACKER_TIMEZONE` | `Asia/Singapore` | Local date boundaries |
| `HABIT_TRACKER_ALLOW_DEV_IDENTITY` | `false` | Accept `X-Habit-Tracker-Dev-User` only in explicit local development |
| `HABIT_TRACKER_IDENTITY_RESOLVER_URL` | unset | Narrow control-plane identity endpoint |
| `HABIT_TRACKER_IDENTITY_TOKEN_FILE` | unset | Resolver token file; required with resolver URL |
| `HABIT_TRACKER_STATE_DIR` | required by Compose | Private host directory mounted at `/data` |

For Compose, the host supplies `HABIT_TRACKER_IDENTITY_TOKEN_FILE` as a private
host path; Compose mounts it read-only and gives the container its internal
`/run/secrets/service-identity-token` path. Never put a token value into the
Compose file. The state and token host paths belong in private deployment
configuration. The old `WATER_TRACKER_` names and `water-dev` task are retired.

## Uploads (staged job inputs)

| Variable | Default | Purpose |
|---|---|---|
| `HOME_PLATFORM_UPLOAD_DIR` | `data/uploads` | Where submitted scripts and datasets are staged |
| `HOME_PLATFORM_MAX_UPLOAD_BYTES` | `10485760` (10 MiB) | Per-dataset upload ceiling |
| `HOME_PLATFORM_MAX_SCRIPT_UPLOAD_BYTES` | `262144` (256 KiB) | Per-script upload ceiling |
| `HOME_PLATFORM_MAX_PROJECT_UPLOAD_BYTES` | `20971520` (20 MiB) | Compressed ZIP ceiling for a batch project |
| `HOME_PLATFORM_STORAGE_DIR` | `/srv/home-platform/storage/nas` | Root exposed as logical `home-storage`; Job Desk and CLI paths must remain relative to it |
| `HOME_PLATFORM_MEMBER_STORAGE_ENABLED` | false | Enable member Home/Shared browsing |
| `HOME_PLATFORM_MEMBER_STORAGE_USER_IDS` | empty | Comma-separated stable user UUIDs allowed while the global flag stays false |
| `HOME_PLATFORM_WORKSPACE_DIR` | unset | Separate CIFS mount used by the constrained Job Desk workspace writer |
| `HOME_PLATFORM_MEMBER_WORKSPACE_ENABLED` | false | Globally enable member create/upload operations inside `Home/Workspace` only |
| `HOME_PLATFORM_MEMBER_WORKSPACE_USER_IDS` | empty | Stable UUID pilot allowlist for workspace writes while the global flag stays false |
| `HOME_PLATFORM_MAX_WORKSPACE_UPLOAD_BYTES` | `268435456` (256 MiB) | Maximum size of one browser-to-workspace upload |

The project ceiling also bounds each arbitrary named input upload in the current
batch implementation. These inputs are stored separately under generated IDs;
their original client paths and filenames are not execution paths.

Uploads are deleted automatically once the job referencing them reaches a
terminal state, unless another unfinished job still references the same upload.

Keep the dataset and project ceilings low. Uploads pass *through* the
coordinator, so this is the one data path where it sits in the byte stream;
anything large should use a linked URL instead, which goes directly to the
worker.

HomeStorage project imports and input references do not copy the original file
into ordinary upload staging. A project folder is packaged into the bounded
project ZIP; an input file remains on the share and is identified by relative
path, size, and SHA-256. The coordinator streams that input from its
authenticated Synology mount through an authenticated API response to the
worker. A future provider can resolve the same logical contract through a
direct worker-to-NAS storage path.

For members, storage is fail-closed by default. When member storage is enabled,
the browser exposes only two virtual roots: `Home` maps to that account's
stable UUID directory and `Shared` maps to the household collaboration
directory. Administrators continue to see provider-relative paths.

The flag is a rollout control, not a substitute for file-server permissions:
those still enforce the disk boundary. Enable it only after proving that one
member cannot read another member's directory over SMB, and use the UUID
allowlist to admit accounts one at a time while the global flag stays false.

Workspace mutation is a separate privilege from storage browsing. The API uses
`HOME_PLATFORM_WORKSPACE_DIR`, a second mount authenticated as a dedicated
workspace service identity. That identity receives read/write only on each
provisioned workspace subtree; it must not replace the read-oriented storage
mount or inherit artifact write privileges.

The API maps every member mutation to its immutable UUID, accepts only paths
inside that member's workspace, rejects traversal and links, refuses
overwrites, stages uploads under a hidden temporary name, and atomically
promotes a completed upload.

## Artifacts (published job results)

| Variable | Default | Purpose |
|---|---|---|
| `HOME_PLATFORM_ARTIFACT_DIR` | `data/artifacts` | Root of owner-keyed published results: `<owner-id>/<job-name>-<short-id>/` |
| `HOME_PLATFORM_MAX_ARTIFACT_BYTES` | `104857600` (100 MiB) | Per-file ceiling |
| `HOME_PLATFORM_MAX_JOB_ARTIFACT_BYTES` | `536870912` (512 MiB) | Per-job total ceiling |
| `HOME_PLATFORM_MAX_ARTIFACT_STORE_BYTES` | `53687091200` (50 GiB) | Whole-store ceiling — a backstop, not a policy |
| `HOME_PLATFORM_ARTIFACT_REQUIRE_MOUNT` | unset (false) | Refuse to write unless the artifact directory is on a different device from `/` |
| `HOME_PLATFORM_ARTIFACT_OWNER_SCOPED` | unset (false) | Place runs under a provisioned `<owner-id>/` directory; enable only with the NAS cutover |

Results never expire by age. The store ceiling only evicts
least-recently-touched runs if a runaway threatens the disk, and logs each
eviction at `WARNING`. The evicted unit is one submission, so an array's
children go together. In owner-scoped mode, the server derives the owner
directory from the immutable job record; workers cannot select it. An owner
directory must be provisioned before publication, preventing a newly created
account from inheriting an overly broad NAS ACL. The flag exists so the new
code can be deployed safely before the legacy artifact tree is migrated and the
storage path is cut over.

The run directory beneath the owner is derived from the job's name so results
are recognisable over SMB rather than a wall of UUIDs. Names are neither unique
nor path-safe, so the server reduces the name to one safe segment and appends
the first eight characters of the job's — or, for an array child, its group's —
UUID. Nothing about this is worker-supplied. Results published before this
layout are still served from their original `<job-id>/` directory, so the change
strands no completed work; migrate them with the artifact migration helper.

These limits also define the practical download system today. Each artifact is
served as a streamed file response with byte-range support and a SHA-256 value
in its listing. `pixi run client pull` resumes retained `.part` files and
verifies the final size and digest before atomically exposing the completed
file. The single-file CLI command and authenticated Job Desk still provide
ordinary streaming downloads. Job Desk only previews text-like files up to
256 KiB; that preview threshold is a browser-interface safety limit, not an
artifact storage limit.

**Set `ARTIFACT_REQUIRE_MOUNT` whenever the artifact directory lives on
removable storage.** If that disk is absent, its mount point is still a
perfectly writable directory on the system disk, so writes would succeed and
quietly fill it. The check compares device identity against the root
filesystem.

## Service health reporting

The dashboard reports credential-free liveness for the dedicated NAS. This
affects only what it displays; application mounts and ACLs remain the functional
storage boundary.

| Variable | Default | Purpose |
|---|---|---|
| `HOME_PLATFORM_SYNOLOGY_HOST` | unset | Private DNS name or address whose SMB and DSM reachability are reported |

When `HOME_PLATFORM_SYNOLOGY_HOST` is configured, the authenticated Dashboard
shows the dedicated NAS in the Synology NAS card.
SMB reachability determines its endpoint state; DSM HTTPS reachability is shown
as an additional management signal. No NAS credential is sent, and the check
does not claim that a share is mounted, writable, or authorized. Leave the
variable unset when no dedicated NAS is present. The retired Pi Samba unit and
local SSD are intentionally not part of this service-health response.

## Worker

| Variable | Default | Purpose |
|---|---|---|
| `HOME_PLATFORM_API_URL` | `http://raspberrypi.local:8000` | Control plane to poll |
| `HOME_PLATFORM_WORKER_ID` | `worker-<hostname>` | Identity in the registry. Stable across restarts |
| `HOME_PLATFORM_API_TOKEN_FILE` | `~/.config/home-platform/api-token` | Token used for every call |
| `HOME_PLATFORM_WORKER_DATA_DIR` | `~/.local/share/home-platform-worker` | Cache, staged inputs, and outputs |
| `HOME_PLATFORM_POLL_SECONDS` | `5` | How often an idle worker asks for work and refreshes liveness |
| `HOME_PLATFORM_HEARTBEAT_SECONDS` | `5` | Lease renewal interval. Must be well under `LEASE_SECONDS` |
| `HOME_PLATFORM_DATASET_ALLOWED_HOSTS` | unset | Comma-separated hosts from which this worker may fetch digest-and-size-verified `python_batch` datasets or general `batch` named inputs. Empty disables linked inputs but not coordinator-staged work. |
| `HOME_PLATFORM_MAX_DATASET_BYTES` | `10737418240` (10 GiB) | Largest linked dataset or named input this worker accepts |
| `HOME_PLATFORM_MAX_CACHE_BYTES` | `21474836480` (20 GiB) | Combined ceiling for reusable dataset, script, named-input, and project-archive caches; least-recently-used inactive entries are evicted before a new download |
| `HOME_PLATFORM_CONTAINER_IMAGE` | `home-platform-ml:0.1` | Image backing `python_batch` and `scientific-python:1` batch jobs. The worker advertises those types only while this image exists locally |

A worker registers with scheduling **disabled**; it claims nothing until enabled
through the API, CLI, or dashboard.

The native Linux deployment uses a systemd user unit and a locked `linux-64`
worker environment. It starts with the owner's login; running it before login
requires an explicit administrator decision to enable user lingering. Installing
the agent does not start Docker, build an image, or enable scheduling.

## Inside a job container

These are set *by* the worker and read *by* your script.

| Variable | Job types | Meaning |
|---|---|---|
| `HOME_PLATFORM_INPUT_DIR` | both | Read-only directory of logical named inputs |
| `HOME_PLATFORM_OUTPUT_DIR` | both | Write results here — everything left behind is published as an artifact |
| `HOME_PLATFORM_JOB_ID` | both | The job's UUID |
| `HOME_PLATFORM_JOB_NAME` | both | The submitted job name, or the UUID when unnamed |
| `HOME_PLATFORM_CPU_LIMIT` | both | The CPU quota this job was given, as a float |
| `HOME_PLATFORM_DATASET` | `python_batch` | Absolute path to the input CSV, read-only |
| `HOME_PLATFORM_PROJECT_DIR` | `batch` | Extracted project tree, read-only |
| `HOME_PLATFORM_TMP_DIR` | `batch` | Writable scratch, discarded after the attempt |
| `HOME_PLATFORM_ARRAY_INDEX` | `batch` | Numeric index for the current array child |
| `HOME_PLATFORM_WORKER_ID` | `batch` | Worker executing this attempt |
| `HOME_PLATFORM_ATTEMPT` | `batch` | One-based attempt number |
| `HOME_PLATFORM_MEMORY_MB` | `batch` | Effective memory limit |
| `HOME_PLATFORM_TIMEOUT_SECONDS` | `batch` | Effective wall-time in seconds |

Thread-pool variables (`OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`,
`MKL_NUM_THREADS`, `NUMEXPR_NUM_THREADS`, `VECLIB_MAXIMUM_THREADS`) are also
set, pinned to the CPU quota. See [Job
contract](compute/job-contract.md#resource-limits) for why that matters.

## Per-job resource limits

Set at submission rather than by environment:

| Parameter | Range | Default |
|---|---|---|
| `cpu_limit` | 0.1 – 8.0 | 2.0 |
| `memory_mb` | 256 – 16384 | 2048 |
| `timeout_seconds` | 1 – 604800 (7 days) | 1800 |

`cpu_limit` is a hard quota, not a priority. A fraction runs the job slowly and
coolly rather than merely deprioritising it, which makes long overnight training
runs practical on a machine you are also using.

Memory and timeout are hard cancellation boundaries. Crossing them leaves the
job in `FAILED`, with `failure_kind` set to `MEMORY_LIMIT_EXCEEDED` or
`TIMED_OUT` respectively.

## Worker job envelopes

Each worker has operator-managed `max_job_cpu` and `max_job_memory_mb` values.
They are durable admission ceilings for one `python_batch` job, not measurements
of current utilization. New workers cannot claim batch work until both are set.

Automatic placement filters for available workers that fit the request and
chooses the smallest memory envelope, then CPU envelope, then worker ID. Keep
the envelope below the host's physical resources to reserve space for the OS,
container runtime, and interactive use.

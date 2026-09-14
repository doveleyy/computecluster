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

## Uploads (staged job inputs)

| Variable | Default | Purpose |
|---|---|---|
| `HOME_PLATFORM_UPLOAD_DIR` | `data/uploads` | Where submitted scripts and datasets are staged |
| `HOME_PLATFORM_MAX_UPLOAD_BYTES` | `10485760` (10 MiB) | Per-dataset upload ceiling |
| `HOME_PLATFORM_MAX_SCRIPT_UPLOAD_BYTES` | `262144` (256 KiB) | Per-script upload ceiling |
| `HOME_PLATFORM_MAX_PROJECT_UPLOAD_BYTES` | `20971520` (20 MiB) | Compressed ZIP ceiling for a batch project |
| `HOME_PLATFORM_STORAGE_DIR` | `/srv/home-platform/storage/nas` | Root exposed as logical `home-storage`; Job Desk and CLI paths must remain relative to it |
| `HOME_PLATFORM_MEMBER_STORAGE_ENABLED` | false | Enables member Home/Shared browsing only after the NAS cross-user ACL denial test passes |
| `HOME_PLATFORM_MEMBER_STORAGE_USER_IDS` | empty | Comma-separated stable user UUIDs allowed during a limited, explicitly unaccepted pilot |
| `HOME_PLATFORM_WORKSPACE_DIR` | unset | Separate CIFS mount used by the constrained Job Desk workspace writer |
| `HOME_PLATFORM_MEMBER_WORKSPACE_ENABLED` | false | Globally enable member create/upload operations inside `Home/Workspace` only |
| `HOME_PLATFORM_MEMBER_WORKSPACE_USER_IDS` | empty | Stable UUID pilot allowlist for workspace writes while the global flag stays false |
| `HOME_PLATFORM_MAX_WORKSPACE_UPLOAD_BYTES` | `268435456` (256 MiB) | Maximum size of one browser-to-workspace upload |

The project ceiling also bounds each arbitrary named input upload in the current
batch implementation. These inputs are stored separately under generated IDs;
their original client paths and filenames are not execution paths.

Uploads are deleted automatically once the job referencing them reaches a
terminal state, unless another unfinished job still references the same upload.

Keep the dataset and project ceilings low. Uploads pass *through* the coordinator, so this is
the one data path where it sits in the byte stream; anything large should use a
linked URL instead, which goes directly to the worker.

HomeStorage project imports and input references do not copy the original file
into ordinary upload staging. A project folder is packaged into the bounded
project ZIP; an input file remains on the share and is identified by relative
path, size, and SHA-256. The current Pi-attached provider streams that input
through an authenticated API response to the worker. A future external NAS
provider can resolve the same logical contract through a direct storage path.

For members, storage is fail-closed by default. When member storage is enabled,
Job Desk exposes only two virtual roots: `Home` maps to that account's stable
UUID directory and `Shared` maps to the household collaboration directory.
Administrators continue to see provider-relative paths. The flag is not a
substitute for NAS ACLs: enable it only after proving that one member cannot
read another member's directory over SMB.

Before multi-user acceptance, an operator may keep the global flag false and
allow only explicitly provisioned UUIDs with
`HOME_PLATFORM_MEMBER_STORAGE_USER_IDS`. This is a rollout control, not an ACL
replacement: NAS permissions still enforce the disk boundary, and a new member
must not be added to the allowlist until their owner directories are ready.

Workspace mutation is a separate privilege from storage browsing. The API uses
`HOME_PLATFORM_WORKSPACE_DIR`, which should be a second mount authenticated as
a dedicated workspace service identity. That identity receives read/write only
on each provisioned `users/<id>/Workspace/` subtree; it must not replace the
read-oriented storage mount or inherit artifact write privileges. A NAS may
also require Read/Write at its share-level SMB gate before the mount can open;
in that case, enforce least privilege with directory ACLs, including an explicit
artifact deny, and prove the access matrix from the coordinator. The API maps
every member mutation to its immutable UUID, accepts only `Home/Workspace/...`,
rejects traversal and links, refuses overwrites, stages uploads under a hidden
temporary name, and atomically promotes a completed upload. Keep the global
flag false until multi-user acceptance; provisioned pilot UUIDs can be enabled
individually only after their mount and ACL checks pass.

## Artifacts (published job results)

| Variable | Default | Purpose |
|---|---|---|
| `HOME_PLATFORM_ARTIFACT_DIR` | `data/artifacts` | Root of owner-keyed published results: `<owner-id>/<job-name>-<short-id>/` |
| `HOME_PLATFORM_MAX_ARTIFACT_BYTES` | `104857600` (100 MiB) | Per-file ceiling |
| `HOME_PLATFORM_MAX_JOB_ARTIFACT_BYTES` | `536870912` (512 MiB) | Per-job total ceiling |
| `HOME_PLATFORM_MAX_ARTIFACT_STORE_BYTES` | `53687091200` (50 GiB) | Whole-store ceiling — a backstop, not a policy |
| `HOME_PLATFORM_ARTIFACT_REQUIRE_MOUNT` | unset (false) | Refuse to write unless the artifact directory is on a different device from `/` |
| `HOME_PLATFORM_ARTIFACT_OWNER_SCOPED` | unset (false) | Place runs under a provisioned `<owner-id>/` directory; enable only with the NAS cutover |

Results never expire by age. The store ceiling only evicts least-recently-touched
runs if a runaway threatens the disk, and logs each eviction at `WARNING`. The
evicted unit is one submission, so an array's children go together.
In owner-scoped mode, the server derives the owner directory from the immutable
job record; workers cannot select it. An owner directory must be provisioned
before publication, preventing a newly created account from inheriting an
overly broad NAS ACL. The flag exists so the new code can be deployed safely
before the legacy artifact tree is migrated and the storage path is cut over.

The run directory beneath the owner is derived from the job's name so results
are recognisable over SMB rather than a wall of UUIDs. Names are neither unique
nor path-safe, so the server reduces the name to one safe segment and appends
the first eight characters of the job's — or, for an array child, its group's —
UUID. Nothing about this is worker-supplied. Results published before this
layout are still served from their original `<job-id>/` directory, so the change
strands no completed work; migrate them with the artifact migration helper.

These limits also define the practical download system today. Each artifact is
served as a streamed file response and may be downloaded through the API, CLI,
or authenticated Job Desk. Job Desk only previews text-like files up to 256 KiB;
that preview threshold is a browser-interface safety limit, not an artifact
storage limit. Transfers are not yet resumable and have no progress contract.

**Set `ARTIFACT_REQUIRE_MOUNT` whenever the artifact directory lives on removable
storage.** If that disk is absent, its mount point is still a perfectly writable
directory on the system disk, so writes would succeed and quietly fill it. The
check compares device identity against the root filesystem.

## Service health reporting

The dashboard reports on a file-sharing service if one is present. These only
affect what it displays; nothing functional depends on them.

| Variable | Default | Purpose |
|---|---|---|
| `HOME_PLATFORM_NAS_MOUNT` | `/srv/home-platform/storage` | Mount point checked for presence and capacity |
| `HOME_PLATFORM_NAS_SERVICE` | `home-platform-nas` | systemd unit whose state is reported |
| `HOME_PLATFORM_SYNOLOGY_HOST` | unset | Private DNS name or address whose SMB and DSM reachability are reported |

The health check combines four signals — the mount point being a real mount, its
capacity, the service unit's state, and TCP reachability of the share port. It is
a liveness indication, not proof that an authenticated read or write would
succeed.

When `HOME_PLATFORM_SYNOLOGY_HOST` is configured, the authenticated Dashboard
shows the dedicated NAS as its own endpoint inside the Network Storage card.
SMB reachability determines its endpoint state; DSM HTTPS reachability is shown
as an additional management signal. No NAS credential is sent, and the check
does not claim that a share is mounted, writable, or authorized. Leave the
variable unset when no dedicated NAS is present.

## Worker

| Variable | Default | Purpose |
|---|---|---|
| `HOME_PLATFORM_API_URL` | `http://raspberrypi.local:8000` | Control plane to poll |
| `HOME_PLATFORM_WORKER_ID` | `worker-<hostname>` | Identity in the registry. Stable across restarts |
| `HOME_PLATFORM_API_TOKEN_FILE` | `~/.config/home-platform/api-token` | Token used for every call |
| `HOME_PLATFORM_WORKER_DATA_DIR` | `~/.local/share/home-platform-worker` | Cache, staged inputs, and outputs |
| `HOME_PLATFORM_POLL_SECONDS` | `2` | How often to ask for work |
| `HOME_PLATFORM_HEARTBEAT_SECONDS` | `5` | Lease renewal interval. Must be well under `LEASE_SECONDS` |
| `HOME_PLATFORM_DATASET_ALLOWED_HOSTS` | unset | Comma-separated hosts from which this worker may fetch digest-and-size-verified `python_batch` datasets or general `batch` named inputs. Empty disables linked inputs but not coordinator-staged work. |
| `HOME_PLATFORM_MAX_DATASET_BYTES` | `10737418240` (10 GiB) | Largest linked dataset or named input this worker accepts |
| `HOME_PLATFORM_CONTAINER_IMAGE` | `home-platform-ml:0.1` | Image backing `python_batch` and `scientific-python:1` batch jobs. The worker advertises those types only while this image exists locally |

A worker registers with scheduling **disabled**; it claims nothing until enabled
through the API, CLI, or dashboard.

## Inside a job container

These are set *by* the worker and read *by* your script.

| Variable | Meaning |
|---|---|
| `HOME_PLATFORM_DATASET` | Absolute path to the input CSV, read-only |
| `HOME_PLATFORM_INPUT_DIR` | Directory of logical named batch inputs, read-only |
| `HOME_PLATFORM_PROJECT_DIR` | Extracted batch project tree, read-only |
| `HOME_PLATFORM_ARRAY_INDEX` | Numeric index for the current array child |
| `HOME_PLATFORM_OUTPUT_DIR` | Write results here — everything left behind is published as an artifact |
| `HOME_PLATFORM_JOB_ID` | The job's UUID |
| `HOME_PLATFORM_CPU_LIMIT` | The CPU quota this job was given, as a float |

Thread-pool variables (`OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`,
`MKL_NUM_THREADS`, `NUMEXPR_NUM_THREADS`, `VECLIB_MAXIMUM_THREADS`) are also set,
pinned to the CPU quota. See [Job contract](job-contract.md#resource-limits) for
why that matters.

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

# Job and API Contract

The wire contract between clients, the control plane, and workers. Schemas live
in the shared models module and are enforced by Pydantic on both sides.

## Job identity and state

Every job has a server-generated UUID and an optional, **non-unique**
human-readable name. Repeated runs may deliberately share a name; identity,
leases, idempotency, and state transitions always use the UUID.

A submission may include `target_worker_id`. The target must already be a
registered worker. The job remains `QUEUED` until that exact worker is enabled,
online, idle, advertises the required job type, and fits the requested resource
limits. Omitting it selects the smallest currently available capacity that fits.
Targeting never bypasses a worker's resource ceiling.

```text
QUEUED -> RUNNING -> COMPLETED
                  -> FAILED
RUNNING --lease expires--> QUEUED, until max_attempts, then FAILED
QUEUED  --user cancels--> FAILED / CANCELLED_BY_USER
RUNNING --user cancels--> cancellation requested --> FAILED / CANCELLED_BY_USER
```

Supplying an `Idempotency-Key` header makes repeated identical submissions
return the same job. Reusing a key with a different payload returns `409`.
The target worker is part of that payload.

### Failure details

Every unsuccessful terminal job has `status: FAILED`. `failure_kind` provides a
stable additional code, while `error` provides human-readable detail:

| `failure_kind` | Meaning |
|---|---|
| `EXECUTION_ERROR` | The submitted program or handler exited unsuccessfully |
| `INFRASTRUCTURE_ERROR` | The container runtime could not launch the workload |
| `MEMORY_LIMIT_EXCEEDED` | Docker killed the container after it crossed `memory_mb` |
| `TIMED_OUT` | The worker killed the container after `timeout_seconds` |
| `WORKER_LOST` | Lease expiry exhausted the job's attempt limit |
| `CANCELLED_BY_USER` | An operator cancelled the queued or running job |

Clients should branch on `status` first and then use `failure_kind`; they should
not infer a cause by parsing `error` text.

Cancellation deliberately does not add another lifecycle state. A queued job
becomes `FAILED` immediately. A running job remains `RUNNING` with
`cancellation_requested: true` while its lease holder stops the workload, then
becomes `FAILED / CANCELLED_BY_USER`. Completion and artifact publication are
rejected after the request, so a finishing race cannot turn a cancellation into
success. If the worker disappears, lease recovery finalizes the cancellation
instead of requeueing it.

## Job types

| Type | Input | Execution | Result |
|---|---|---|---|
| `sleep` | `seconds`, 1–300 | Worker sleeps | `slept_seconds` |
| `python_batch` | Uploaded `.py` + verified dataset, timeout ≤ 7 days, 0.1–8 CPUs, 256–16384 MiB | Fixed container image, no network, read-only inputs | Exit code, truncated stdout/stderr, output filenames, artifact reference |
| `batch` | ZIP/HomeStorage project, uploaded files up to 20 MiB, verified HTTPS files, or HomeStorage regular files, `submit.hp`, numeric array, timeout ≤ 7 days, 0.1–8 CPUs, 256–16384 MiB | Approved container runtime executes Bash once per array index with read-only logical inputs | Per-child logs, flat output files, artifact reference |

The former `dataset_script/csv_summary` handler is retired. New submissions are
rejected and workers no longer advertise or execute it. Its schema remains
readable only so completed historical records do not corrupt job history.

`python_batch` remains the lightweight convenience path. The implemented first
general-batch slice provides a shell entrypoint, project bundle, uploaded,
verified-HTTPS, or HomeStorage named file inputs, a resource request, and a
numeric task array. Directory inputs and
reusable runtime selection remain planned. Machine learning is one possible workload,
not the scheduler's organizing abstraction.

Contributor-facing instructions are separated by job type in the
[authoring index](jobs/README.md). The [Python script guide](jobs/python-script.md)
describes the lightweight contract; the [batch script standard](jobs/batch-script.md)
defines the live numeric-array subset and marks later features explicitly.

## Resource limits

A `python_batch` job declares what it may consume:

| Parameter | Range | Default |
|---|---|---|
| `cpu_limit` | 0.1 – 8.0 | 2.0 |
| `memory_mb` | 256 – 16384 | 2048 |
| `timeout_seconds` | 1 – 604800 (7 days) | 1800 |

`cpu_limit` is enforced as a **hard CPU quota**, not a scheduling priority. A
fraction below 1.0 is a supported and useful case: it runs the job slowly and
coolly, which is what makes a multi-hour search practical on a machine you are
also using.

The effects deliberately differ: CPU is throttled, while memory and wall time
are cancellation boundaries. The worker inspects Docker's OOM state before
removing a stopped container, keeping memory exhaustion distinct from an
ordinary non-zero exit and from a timeout.

### Thread pools are pinned to the quota

A CPU quota caps how much CPU time a container may consume; it does **not**
change how many cores the container appears to have. Some libraries account for
this and some do not — `joblib` reads the cgroup quota, native BLAS libraries
generally do not and start one thread per *host* core.

The effect is counter-intuitive: a job limited to one core on an eight-core host
would start eight compute threads, which then contend for a single core's worth
of quota. Measured, that ran roughly **3.5× slower than the same work with one
thread**, for identical CPU budget. Throttling would cost more than the
throttle itself.

The worker therefore sets `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`,
`MKL_NUM_THREADS`, `NUMEXPR_NUM_THREADS` and `VECLIB_MAXIMUM_THREADS` to match
the quota, and exposes `HOME_PLATFORM_CPU_LIMIT` so a script can size its own
parallelism:

```python
cpu_limit = float(os.environ.get("HOME_PLATFORM_CPU_LIMIT", "1"))
n_jobs = max(1, int(cpu_limit))     # NOT n_jobs=-1
```

`n_jobs=-1` inside a container sees the host's cores, not the quota, and
recreates exactly the oversubscription above.

Measured on one Mac grid search, with identical results both times:

| Quota | Threads | Elapsed |
|---|---|---|
| 0.5 CPU | 1 | 1062 s |
| 4.0 CPU | 4 | 173 s |

6.1× faster for 8× the quota — about 77% parallel efficiency, the remainder
being serial setup and the final refit. Throttling costs roughly proportional
time and nothing else.

## Container environment

A job's script receives:

| Variable | Meaning |
|---|---|
| `HOME_PLATFORM_DATASET` | Absolute path to the input file, read-only (`python_batch`) |
| `HOME_PLATFORM_INPUT_DIR` | Read-only directory of logical named inputs |
| `HOME_PLATFORM_OUTPUT_DIR` | Write results here; everything left behind is published |
| `HOME_PLATFORM_JOB_ID` | The job's UUID |
| `HOME_PLATFORM_JOB_NAME` | The submitted job name, or the UUID when unnamed |
| `HOME_PLATFORM_CPU_LIMIT` | The CPU quota, as a float |

Both job types expose this same set, so a script can move between them without
rewriting its I/O. The batch type adds `HOME_PLATFORM_PROJECT_DIR` and
`HOME_PLATFORM_ARRAY_INDEX`; see [batch script](jobs/batch-script.md).

The container has no network, no credentials, and no container-runtime socket.
Standard output is captured but **truncated to the last 8000 characters**, so
anything worth keeping should be written to `HOME_PLATFORM_OUTPUT_DIR` and
retrieved as an artifact.

## Dataset references

A job never carries dataset bytes. It carries one of:

**Linked** — an HTTPS URL, exact `size_bytes`, and `sha256`. The worker
downloads directly from the source. The host must be in that worker's allowlist,
redirects are refused, and both size and digest are verified before use.

**Uploaded** — an `upload_id`, `sha256`, and `size_bytes` returned by a prior
upload call. The worker fetches it from the control plane over its existing
authenticated connection and verifies it again.

In both cases the worker keeps a content-addressed cache keyed by digest, so a
repeated dataset is fetched once. General `batch` named inputs use the same two
reference shapes and verification rules; they are exposed by logical name
instead of being restricted to a CSV dataset variable.

**HomeStorage** — a logical storage ID, normalized share-relative path, exact
size, and SHA-256. The coordinator resolves only regular files below its
configured storage root, rejects traversal and symbolic links, and never puts
host paths into the job. The coordinator serves the file from its authenticated
Synology mount to an authenticated worker, which verifies and caches it.
Directory references and direct worker-to-NAS resolution remain pending.

## Worker protocol

Workers poll; the control plane never pushes.

| Call | Purpose |
|---|---|
| `POST /workers/claim` | Report identity, supported types and metrics; receive a job or null |
| `POST /workers/heartbeat` | Renew a lease, refresh metrics, and receive job-control flags |
| `POST /jobs/{id}/complete` | Submit a result — requires the current lease token |
| `POST /jobs/{id}/fail` | Report failure — requires the current lease token |
| `GET /workers` | List registered workers |
| `PATCH /workers/{id}` | Enable or disable scheduling |
| `PUT /workers/{id}/capacity` | Set the largest single batch job the worker may accept |

A claim returns the oldest queued job for which the caller is the deterministic
selection, or null. Automatic selection filters enabled, online, idle, capable
workers whose configured CPU and memory envelopes fit, then chooses the lowest
memory ceiling, CPU ceiling, and worker ID in that order.
It is atomic: exactly one worker can win a given job. A worker that already
holds a live lease is handed back its existing job rather than a new one.

Completion and failure require the **current** lease token. A stale token is
rejected with `409`, so a revived worker cannot overwrite its replacement's
result.

Workers register with scheduling **disabled** and claim nothing until enabled.
They also register without a batch capacity; an operator must configure one.

A successful heartbeat returns `{"cancellation_requested": false}` or `true`.
This is a control-plane instruction, not progress telemetry. Workers normally
receive a cancellation within one heartbeat interval.

## Job endpoints

All human interfaces use this canonical flow: stage the script or project,
stage small inputs or describe large inputs by verified URL, then submit the resulting
references inside one `JobCreate`. The CLI and Job Desk are different clients
of this contract, not different execution modes. Browser-session routes are
authentication adapters and must preserve the same validation, idempotency,
scheduling, cancellation, and result semantics.

| Call | Purpose |
|---|---|
| `POST /jobs` | Submit; honours `Idempotency-Key` |
| `GET /jobs` | List |
| `GET /jobs/{id}` | Retrieve one |
| `POST /jobs/{id}/cancel` | Cancel queued work or request a running job to stop |
| `POST /uploads/datasets` | Stage a CSV, returns a verified reference |
| `POST /uploads/scripts` | Stage a Python script, returns a verified reference |
| `POST /uploads/projects` | Stage and validate a ZIP project, returns a verified reference |
| `POST /uploads/inputs` | Stage an arbitrary named-input file up to 20 MiB |
| `GET /storage` | Browse safe, non-hidden HomeStorage entries by relative directory |
| `POST /storage/references` | Hash one existing HomeStorage file into an immutable job reference |
| `POST /storage/project-uploads` | Package one HomeStorage project folder as a bounded project ZIP |
| `GET /storage/files/{path}` | Authenticated worker transfer for one referenced HomeStorage regular file |
| `POST /batch-submissions` | Parse `submit.hp` and atomically create its numeric task group |

## Job groups

A group is one user-facing submission containing multiple ordinary child jobs.
Creation is atomic: either the parent and every child are stored, or none are.
Each child retains its own UUID, task ID, queue state, worker placement, lease,
attempts, failure reason, and artifacts. Workers continue claiming child jobs
through the ordinary worker protocol; they do not claim a parent record.

The parent's `status` is derived when read: all queued is `QUEUED`, all complete
is `COMPLETED`, any active/mixed unfinished set is `RUNNING`, and an entirely
terminal set containing a failure is `FAILED`. Group names are display text and
never define membership.

| Call | Purpose |
|---|---|
| `POST /job-groups` | Atomically submit a named parent and 1–1000 child jobs; honours `Idempotency-Key` |
| `GET /job-groups` | List groups with their ordered child records |
| `GET /job-groups/{id}` | Retrieve one group and its current derived state |

Equivalent session-authenticated routes exist under `/jobs-ui/api/job-groups`.
The Job Desk hides group children from the top-level flat list and displays them
under one expandable group row. Automatic creation from a numeric
`#HP --array` range is live; group-wide cancellation remains pending.

## Probes

| Endpoint | Meaning |
|---|---|
| `GET /health` | Process liveness only |
| `GET /ready` | Readiness, including database reachability |
| `GET /version` | Deployed application identity and version |

Liveness and readiness are distinct on purpose: the process can be alive while
the database is not reachable, and that must not read as healthy.

## Authentication

CLI and worker API calls use the elevated `X-API-Token` header. The owner may
exchange that token for an administrator browser session. Members instead sign
in with an individual username and password; the password is checked against a
salted scrypt hash and is never stored in browser JavaScript.

Both login methods produce an HttpOnly, `SameSite=Strict`, signed session cookie
containing only a stable user ID and expiry. Job Desk queries are scoped by that
identity. A member receives not-found for another owner's job, group, staged
upload, or artifact even if the UUID is known. Dashboard metrics, account
management, worker controls, and power control require `ADMIN`.

Uploads are size-capped, stored under generated identifiers rather than
client-supplied filenames, and registered to the authenticated uploader.
Idempotency keys are unique within one owner rather than across all users.

## Results and artifacts

The job record holds **metadata only** — exit codes, truncated stdout and
stderr, digests, and output file names. The files themselves are published
separately.

| Call | Purpose |
|---|---|
| `POST /jobs/{id}/artifacts` | Worker publishes one output file |
| `GET /jobs/{id}/artifacts` | List a job's files, sizes, and SHA-256 digests |
| `GET /jobs/{id}/artifacts/{filename}` | Stream one, including HTTP byte ranges |
| `DELETE /jobs/{id}/artifacts` | Delete all of a job's files |
| `DELETE /jobs/{id}/artifacts/{filename}` | Delete one |

Authenticated browser sessions expose equivalent read-only routes under
`/jobs-ui/api/jobs/{id}/artifacts`. Job Desk can preview text-like files up to
256 KiB and download any accepted artifact. Binary files are download-only.

Publishing is authorised by `worker_id` **plus the current lease token**, sent as
form fields alongside the file. It carries the same authority as completing the
job, because it changes the job's output: a worker whose lease has expired
receives `409` and cannot overwrite the results of its replacement. Once a job
reaches a terminal state its lease is gone, so publishing stops working too.

File names are validated against an allow-list — a plain name, no separators, no
traversal, no leading dot — because they originate from user-supplied code and
are used to build a path. Per-file and per-job size limits are enforced while
streaming rather than trusting a declared length.

**Where results land** is decided by the server, never by the worker or the
script. Each run publishes into `<owner-id>/<job-name>-<short-id>/`; an array
child nests one level deeper, under its group's directory and keyed by array
index:

```text
artifacts/<owner-id>/
├── SVM_model-7dcf9099/         metrics.json, model.joblib
├── SVM_model-1a4be012/         a second run of the same script
└── cohort-analysis-3b2e91c4/
    ├── 1/result.txt
    └── 2/result.txt
```

The standalone job name, or the parent group name for an array, is a convenience
for browsing storage directly; the UUID remains the identity every API route
resolves. Because these names are neither unique nor path-safe, the directory
carries a short UUID suffix and the name is reduced to a safe segment, so two
runs sharing a name cannot collide and a crafted name cannot escape the owner's
root. Results published before this layout existed are still served from their
original `<job-id>/` directory.

The default ceilings are 100 MiB per file and 512 MiB across one job. Publication
uses one HTTP request per file; it is not chunked or resumable at the application
protocol level. Downloads are different: the manifest carries each file's size
and SHA-256, the response supports byte ranges, and `hp pull <job-id>` retains a
hidden `.part` file, resumes it, verifies it, and atomically renames it. There is
not yet a browser download-all workflow or transfer progress UI. Outputs beyond
the publication limits need a separate upload contract rather than a larger JSON
job result.

The `worker://` URI in the result remains as a record of which worker produced
the files. Retrieval goes through the endpoints above.

Deleting artifacts leaves the job record intact — its status, output streams and
recorded file names survive. Nothing expires by age; deletion is an explicit
action. A publish response includes `evicted_runs`, non-empty only when the
store exceeded its ceiling and older runs had to be evicted. Eviction removes a
whole submission, so an array loses all its children together rather than
leaving a partial result behind.

Staged inputs are released automatically once a job reaches a terminal state,
unless another unfinished job still references them.

## Progress reporting

Application progress is not implemented. The control plane knows lifecycle,
lease health, and whether cancellation was requested, but not epochs, batches,
percent complete, live stdout, or ETA. Progress will require a bounded,
rate-limited contract separate from heartbeats; scripts printing percentages do
not currently make them visible while a job runs.

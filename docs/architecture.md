# Architecture

A private home platform: one always-on host coordinates distributed jobs and
runs small household application services, while laptops supply compute only
when they happen to be available.

This document describes design and reasoning. It deliberately contains no
hostnames, addresses, accounts, or filesystem paths — those belong to a
particular deployment, not to the design.

## Shape of the system

```text
        clients (CLI, web)
                |
                v
    +-----------------------+
    |    control plane      |   always on, low power
    |  HTTP API + SQLite    |   owns job truth
    +-----------------------+
                ^
                | workers poll: claim, heartbeat, report
                |
    +-----------+-----------+
    |                       |
 worker A                worker B      on-demand, may disappear
    |                       |
 per-job container      per-job container

    dedicated NAS + Samba        Pi-attached SSD
    files + job artifacts        local-only storage
```

The coordinator is deliberately not a compute node. It stays responsive because
it only ever handles small messages: job contracts, heartbeats, leases, status,
and result metadata. Queued work simply waits when no eligible worker is online.

Long-running household applications use a second pattern. Each application is
an independent container with its own loopback port, health check, lifecycle,
resource limits, and persistence. A tailnet-only reverse proxy presents them
under one private HTTPS origin and dispatches requests by path. The first such
application is a water tracker; it owns a separate SQLite database and has no
access to job-control tables. See [Application services](services.md).

## Ownership boundaries

| Concern | Owner | Why |
|---|---|---|
| Job truth | SQLite on the coordinator | One writer, durable, trivially backed up |
| Job schemas | Shared contract package | Both sides must agree or nothing works |
| State transitions and leases | Control-plane service layer | Workers must not invent transitions |
| Execution | Worker host process | It observes real host resources |
| Untrusted code execution | Per-job container on the worker | The host agent must never import user code |
| Heavy compute | Workers only | The coordinator must not silently become one |

The database is the source of truth. Worker memory, dashboard state, HTTP
responses, and logs are views or transports — never competing authorities.

## Human interfaces

The browser surface is intentionally split by responsibility and task, not by
individual widget:

- **Overview** (`/dashboard`) is the owner monitoring view: control-plane and
  storage health plus worker availability and telemetry.
- **Operations** (`/dashboard/operations`) contains state-changing owner tools:
  scheduling switches, account management, and guarded Pi power.
- **Jobs** (`/jobs-ui`) is the workload history/result view: filter and sort
  jobs, inspect details, cancel work, and retrieve outputs.
- **Files** (`/jobs-ui/files`) is the member-safe file browser: each member sees
  a virtual private `Home`, common `Shared`, and owner-scoped job `Artifacts`,
  without seeing the physical account UUID used by either storage provider.
  `Home` and `Shared` are path rewrites; `Artifacts` is derived from owned job
  records, because published output is stored flat. The share layout and the
  reasoning are in [files in and out of a job](jobs/storage-workflow.md).
- **Submit** (`/jobs-ui/new`) is the focused job-creation workflow for uploaded,
  linked, Python, connectivity, and PBS-style work.
- **CLI** remains the primary automation interface and exposes the same API
  concepts without depending on browser state.

The pages use the same signed-session mechanism but now enforce different
roles. Members may use Job Desk and see only their own workload records. The
operator Dashboard requires `ADMIN`. The CLI and workers retain the separate
elevated API-token path. See [Web interfaces](interfaces.md) and
[Accounts and access control](access-control.md).

### Guarded host power

FastAPI does not run as root and is not granted general `sudo` access. An
authenticated, exactly confirmed reboot or shutdown request creates one of two
fixed marker files in a volatile runtime directory. Root-owned systemd path
units consume those markers and run the corresponding fixed operation.

The API refuses the request while any worker is scheduling-enabled or any job
is running, so an already eligible worker cannot claim new work during the
handoff. The power helper then stops the API, flushes pending writes, and
unmounts the local removable filesystem. If any stop or unmount step fails, it
aborts and restores the API. Only a successful storage detach reaches reboot or
poweroff.

### One submission contract, multiple clients

The CLI and Job Desk are not separate compute systems. They are adapters around
one canonical submission workflow:

```text
script file ------> staged script reference --+
                                               |
CSV upload -------> staged dataset reference --+--> JobCreate --> queue
         or                                    |
verified URL -----> external dataset reference-+
```

Both clients submit the same validated `JobCreate` model, use the same service
method and scheduling rules, and produce the same worker contract. Job Desk
uses an HttpOnly browser session while the CLI sends an API token, so their HTTP
adapter routes differ; authentication transport must not change job semantics.
The CLI and browser both support an uploaded CSV or a verified URL for
`python_batch`. Resource controls shown as convenient choices in the browser
map to the same numeric fields that the CLI exposes directly.

### PBS-style numeric arrays are live

The scheduler is general-purpose compute infrastructure. Its primary future
unit of work is not a model-training template but:

```text
runtime + project + shell entrypoint + logical inputs + resources + task
```

Versions `0.22.0` through `0.24.0` implement the first general slice: a CLI packages a project
directory, the coordinator safely parses its `submit.hp`, and an inclusive
numeric range creates one parent plus independently scheduled children. Each
child runs the same Bash wrapper in the existing unprivileged scientific
container with its own `HOME_PLATFORM_ARRAY_INDEX`, limits, status, logs, and
flat artifacts. Repeated CLI bindings either stage small arbitrary files outside
the project or describe an HTTPS source by URL, exact size, and SHA-256. Both
appear under the same stable logical names. Linked bytes travel source-to-worker,
and worker caches are content-addressed. Array run directories normally use
hard links to cached files instead of copying the bytes per child.

HomeStorage regular-file references are also available through Job Desk and the
CLI. Directory inputs, reusable runtime selection, user-scoped storage,
group-wide cancellation, dependencies, and nested artifact trees
remain the target. `python_batch` stays available as the convenient single-file
path.

The exact author-facing syntax and filesystem rules live in the
[batch script standard](jobs/batch-script.md). It distinguishes the live
numeric-array subset from planned extensions.

One task runs on one worker. A job array distributes independent tasks across
workers; the system does not attempt MPI or one process spanning machines.
Uploaded code never receives control-plane credentials merely to create child
jobs. Arrays and dependencies are materialized by the coordinator, while a
trusted client-side shell script may also submit multiple ordinary jobs.

An environment is stored as an immutable runtime image or reproducible build
definition and cached by compatible workers. It is not copied as an activated
virtual-environment directory with every run: compiled dependencies differ
between ARM64 and AMD64. Worker capability includes runtime and architecture,
and the scheduler assigns a task only where its runtime is available.

### Batch groups and staged files

A batch submission is persisted as one user-facing job group plus one
schedulable child job per task. Group membership uses immutable IDs, never a
shared display name. The Job Desk lists groups at the top level and expands one
group to show each child's placement, attempts, failure reason, logs, and
artifacts. Group status is a projection of child state rather than a second
state machine that can disagree with the queue.

The live content-addressed project object contains the entrypoint, source, and
configuration. It passes through the coordinator as a bounded ZIP and is
verified and safely extracted by the worker. Named file bindings are also live:
every declared name maps to either a separately verified upload or a
digest-and-size-verified HTTPS source, then to a stable read-only container path.
HTTPS bytes travel directly to the selected worker. A regular file already on
the Synology NAS is identified by a logical storage ID, safe relative path,
size, and digest; its host path never enters the job contract. The coordinator
streams the file from its authenticated NAS mount while direct worker-to-NAS
resolution and directory inputs remain later data-plane stages. Array children
reuse the same immutable
references instead of duplicating bytes in job records or, under the normal
same-filesystem layout, on disk.

## Multi-user ownership rollout

The intended end state is user-scoped arbitrary compute, not a catalog of
standardized jobs. Each member may still submit a script and its inputs. The
platform attaches an authenticated owner to every job, upload, dataset, and
artifact, and applies authorization on every operation.

| Capability | Member | Administrator |
|---|---|---|
| Submit scripts and inputs | Yes, owned by that member | Yes |
| List, inspect, or cancel jobs | Own only | All users |
| Preview or download artifacts | Own only | All users |
| Personal NAS files | Own only | All users |
| Shared NAS files | Read/write | Read/write |
| Worker controls and system health | No | Yes |
| User and credential management | No | Yes |

This is an authorization rule, not merely a UI filter. A member who knows
another job UUID must still receive no access through the API, CLI, Job Desk,
artifact URL, or SMB. List queries are scoped by owner; individual reads,
downloads, cancellation, and deletion check the same owner. UUIDs are identity,
not access control.

The data model uses a stable user ID and role. Jobs record an immutable
`owner_user_id`; uploads and artifacts inherit that owner from the job rather
than accepting an owner supplied by a worker. Human-readable usernames may
change, so filesystem placement uses a stable storage key. Existing records are
assigned to the administrator during migration. Published files use
`artifacts/<owner-id>/<job-name>-<short-id>/` for standalone jobs. Array
children are nested beneath `<group-name>-<short-group-id>/`; the API refuses to
create a missing owner root because doing so could inherit an unsafe share-level
ACL.

The run directory is derived from the standalone job name or persisted parent
group name, so an owner browsing the NAS over SMB recognises their work instead
of reading UUIDs. That name is user-supplied, neither unique nor path-safe, so
it is reduced to one safe segment and carries a short UUID suffix: two runs
sharing a name stay separate, and a crafted name cannot escape the owner's
root. The UUID remains the identity every API route resolves — the name is a
browsing affordance, never a key and never an authorization boundary. Placement
is computed by the server from immutable job and group records; a worker cannot
influence where its output lands.

```text
authenticated member
        |
        +--> own jobs/uploads/artifacts
        +--> own NAS directory
        +--> shared NAS directory

authenticated administrator
        |
        +--> every user's jobs/uploads/artifacts
        +--> every user's NAS directory
        +--> shared NAS directory
        +--> operations and user management
```

Application and SMB authentication remain separate security boundaries. The
API enforces job and artifact ownership in the database. Samba and host
filesystem permissions enforce personal and shared storage access on disk.
They may use matching stable account names for usability, but credentials are
provisioned and stored separately. Worker credentials are service identities
and never grant a worker end-user browsing rights.

Browser workspace mutation uses a separate service identity from artifact
publication. Its NAS authority is limited to provisioned
`users/<owner-id>/Workspace/` trees, while the application maps every request
from the signed session to exactly one owner tree. The NAS sees the workspace
service identity rather than the human actor, so application authorization is
still essential; the restricted NAS ACL limits the blast radius of a defect.
Members' own SMB sessions continue to use their personal NAS identities.

Schema migrations 13 and 14 implement the application boundary. They create
stable `MEMBER`/`ADMIN` identities, backfill existing jobs/groups to the
administrator, add salted password hashes and signed user sessions, make
ownership required and immutable, scope idempotency by owner, and register
staged uploads to their uploader. List/detail/cancel/artifact routes enforce the
same owner rule, and adversarial tests cover known foreign UUIDs.

The Synology storage provider is live for Home, Shared, Workspace, and
owner-scoped artifacts. Publisher and browser-workspace writes use separate SMB
identities with distinct ACLs; the accepted member remains allowlisted while a
second-member cross-user acceptance test is still pending. The retired Pi
Samba service is disabled and its SSD is local-only.

## Storage topology

The dedicated NAS is the only household SMB service and owns personal
directories, shared data, project inputs, and published artifacts. The Pi
remains the always-on control plane: API, scheduler, SQLite, authentication,
leases, and small upload staging. Its attached SSD is local-only and may be
repurposed for backup, scratch, or control-plane recovery.

```text
clients ---------------- SMB ----------------> dedicated NAS
   |                                               ^
   | HTTPS                                         | direct input/result transfer
   v                                               |
Pi control plane <----- status + metadata ---- compute workers
```

The logical storage-reference contract remains independent of the physical
provider. The Dashboard's Synology NAS card represents only Synology; it
does not treat optional Pi-local storage as a network service. An authenticated read or
write check remains stronger evidence than credential-free TCP liveness.

## Job lifecycle

```text
client submits
      |
      v
   QUEUED
      |
      | atomic claim by an eligible worker
      v
   RUNNING  + renewable lease
      |
      +--> success ------> COMPLETED
      |
      +--> handler error -> FAILED
      |
      +--> user cancel --> FAILED / CANCELLED_BY_USER
      |
      +--> lease expires -> QUEUED, bounded by max_attempts, then FAILED
```

**Leases** are what make worker loss survivable. A claimed job carries a lease
token and an expiry. The worker renews it by heartbeat. If the worker sleeps,
loses network, or dies, the lease expires and the coordinator requeues the job —
up to a bounded attempt limit, so a job that reliably kills its worker fails
instead of looping forever.

Only the worker holding the current lease token may complete or fail a job. A
stale token is rejected, so a worker that comes back from the dead cannot
overwrite a result produced by its replacement.

`FAILED` remains the single unsuccessful terminal lifecycle state. A separate
machine-readable `failure_kind` explains whether the cause was execution,
infrastructure, a memory limit, a timeout, final worker loss, or an operator
cancellation. The `error` field is human-readable diagnostic detail, not a code
clients must parse.

Queued cancellation is immediate. Running cancellation is cooperative across
the distributed boundary: the coordinator records `cancellation_requested`, a
lease heartbeat carries that instruction back to the worker, and the worker
force-removes only the named job container before acknowledging
`FAILED / CANCELLED_BY_USER`. A cancellation requested before completion wins
the race; completion and artifact publication are refused. If the worker is
lost before acknowledgement, lease recovery finalizes the cancellation rather
than requeueing it.

Heartbeat cancellation is control, not application progress. The current
system does not store epochs, percentages, live logs, or ETA. Those need a
separate bounded and rate-limited progress contract so fast loops cannot turn
the small coordinator into a telemetry write sink.

**Idempotency.** An optional client-supplied key makes repeated identical
submissions return the same job. Reusing a key with a different payload is a
conflict, not a silent overwrite.

## Scheduling: deterministic best-fit placement

Placement remains worker-pull, but claiming is decided centrally. Each worker
has a durable maximum CPU and memory envelope for a single batch job. A
submission may name a target or request automatic placement.

```sql
eligible = enabled + online + idle + capable + request fits job envelope

if target_worker_id:
    choose that worker, if eligible
else:
    choose the eligible worker with the smallest memory envelope,
    then smallest CPU envelope, then worker ID
```

The consequences are worth stating plainly, because they are easy to
misread as intelligence:

- An explicitly targeted job waits until that exact registered worker is
  enabled, online, idle, capable, and within its configured envelope.
- Targeting is a placement instruction, not permission to bypass safety limits.
- An automatic job uses deterministic **best fit**. The smallest adequate node
  wins even when a larger node polls first, preserving the larger envelope for
  work that needs it.
- A busy best-fit node is excluded, so the next job may spill to another node.
- Live CPU, memory, storage, GPU, and temperature readings are displayed but not
  used for placement. They fluctuate too quickly to be a stable policy.
- The claim is a single atomic conditional update, so exactly one worker can win
  a given job even under contention.
- A worker already holding a live lease is handed back its existing job rather
  than a new one, so each node runs at most one job at a time. This produces
  crude but real load spreading: whoever is free takes the next job.

Batch submission is rejected when no registered capacity can ever fit it,
rather than creating a permanently queued job. A newly registered worker has no
batch envelope and cannot claim batch work until an operator configures one.

The envelope limits one job because each worker runs at most one job at a time.
It is intentionally lower than total host resources so the operating system,
Docker, and interactive work retain headroom. Changing a worker's envelope
affects future claims; it does not cancel a job that already holds a lease.

## Scheduling eligibility is separate from liveness

Each registered worker has a durable enable/disable switch that lives in the
control plane, not the worker.

Disabling a worker does not stop its process, and does not change whether it is
online, busy, or stale. It atomically blocks *future* claims while heartbeats
and metrics continue. A job already holding a valid lease may finish normally —
this is graceful draining, not remote cancellation.

Workers register with that switch **off**. A newly seen worker is inert until
someone deliberately enables it, so an accidentally-started agent cannot quietly
begin consuming work.

## Data plane

Large files should not travel through the coordinator process or sit in job
rows. Three paths currently exist, with one deliberate transitional exception:

**Linked datasets.** The job carries a URL, an exact byte count, and a SHA-256.
The selected worker downloads directly from the source, verifies size and digest
before use, and keeps a content-addressed local cache keyed by digest. The
coordinator never sees the bytes.

**Uploaded datasets.** Small files submitted through the web interface are staged
by the coordinator, which records an upload ID, digest, and size. The worker
retrieves them over its existing authenticated connection and verifies them
again. Here the coordinator *is* in the byte path, which is why uploads are size
capped and linked datasets remain the route for anything large.

**NAS files.** A user first copies a project and data into the guarded share
using an ordinary file client. Job Desk or the CLI then selects a regular file
by logical path — `Home/...` for a private tree, `Shared/...` for deliberately
shared data — and the coordinator maps that onto the provider tree, records its
exact size and SHA-256, and does not copy it into upload staging. One
vocabulary covers both interfaces and both job types; the caller's identity
decides what `Home` means, so a member cannot name another member's tree and an
administrator must say whose tree it wants. Because the current disk is
attached to the Pi, workers retrieve the bytes through an authenticated API
file response and verify them into the same content-addressed cache. This makes
the workflow usable now but is not the desired high-throughput endpoint. When
storage moves to a dedicated NAS, the provider should resolve the same logical
reference directly between NAS and worker.

Verification happens on the consuming side in both cases. A declared digest that
does not match what arrived is a hard failure, not a warning.

### Publishing results

Inputs travel to the compute; results travel back. A worker that finishes a job
uploads its output files to the coordinator, which stores them in a directory it
derives from the job record on durable storage. The job record keeps only
metadata — exit code, truncated output streams, and file names.

Four details make this work rather than merely function:

**The coordinator decides where results land, not the worker or the script.**
A submission publishes into its own directory, so no run can overwrite another
even when two share a name, and a script cannot aim its output somewhere it
should not reach. Naming that directory after the job rather than its UUID is
what makes the store legible to a person browsing it over SMB.

**The upload is authorised by the same lease that authorises completion.**
Publishing results mutates a job's output, so it demands the same proof as
finishing it. A worker whose lease has expired — because it froze and the job
was requeued — cannot overwrite the results of whichever worker took over.

**The upload happens while the lease is still being renewed.** This is easy to
get wrong. If results are uploaded after the heartbeat loop stops, a transfer
slower than the lease interval causes the coordinator to declare the worker dead
and requeue the job *while it is succeeding*. Large jobs would then silently run
twice while small ones behaved perfectly.

**The coordinator refuses to write to the wrong disk.** Where durable storage is
a removable volume, an absent disk leaves an ordinary directory at the mount
point, and writes would quietly fill the system disk instead of failing. The
write path compares device identity against the root filesystem and refuses
rather than proceeding.

Results are exposed read-only to any file-sharing layer. The coordinator owns
that directory; letting a share client delete from it would create a second
writer and no way to reconcile the two.

SQLite is backed up through its online backup API, never by copying the live
database file. The verified local copy is closed before immutable bytes cross
the NAS boundary, then its destination digest is checked and recorded. This is
the recovery copy for control-plane truth; NAS snapshots and off-device backup
remain independent durability layers.

The browser and CLI download paths stream files through the control plane. They
do not first load the whole file into application memory. Artifact listings are
integrity manifests containing size and SHA-256; HTTP ranges plus the CLI's
hidden partial file make a full-job pull resumable and verified. The browser
still offers ordinary per-file downloads. Publication is nevertheless a single
request rather than a resumable transfer. Current defaults cap an
artifact at 100 MiB and all artifacts for one job at 512 MiB, so this is suitable
for models, metrics, reports, and modest result tables — not multi-gigabyte model
checkpoints or generated datasets.

At larger sizes, transfer should become its own durable lifecycle: queue the
output, copy in chunks with progress and retry, verify a digest, and only then
publish it. That lets a failed download or upload resume without rerunning the
compute job and allows direct-to-storage transfer without relaying bytes through
the coordinator process.

### Keeping storage bounded

Every job leaves data in several places: staged inputs on the coordinator, a
content-addressed cache and a per-job output directory on the worker, and the
published results. Left alone, all of them grow forever.

Three rules keep that in check, and the distinction between them matters:

- **Inputs are released when a job reaches a terminal state.** Nothing will ask
  for them again. An input still referenced by an unfinished job is kept, since
  two jobs may legitimately share one upload.
- **The worker's copy is deleted once publishing succeeds.** At that point it is
  pure duplication. A *failed* publish leaves it alone — it is then the only
  remaining copy.
- **Published results are never deleted automatically by age.** They are what
  the job was for. A total-size ceiling exists purely as a backstop against a
  runaway, evicting least-recently-touched jobs and logging loudly; removal is
  otherwise an explicit operator action.

The asymmetry is deliberate. Inputs and intermediates are reconstructible or
redundant, so they expire on a rule. Results are not, so they expire on a
decision.

## Isolating untrusted code

The system accepts user-supplied Python. The host worker agent never imports or
executes it. Instead the worker stages the script and its input into a per-job
directory and launches a **fixed, pre-built container image**, with:

- no network
- read-only root filesystem
- read-only input mount; only the output directory is writable
- non-root user, all capabilities dropped, no-new-privileges
- CPU, memory, swap, PID, and wall-clock limits
- no credentials and no container-runtime socket inside

The image is pinned and built ahead of time, not assembled per job — so a job
cannot influence its own runtime. A worker advertises the batch capability only
when it can actually see that image, and re-checks periodically, so capability
appears and disappears on its own without restarts.

This is appropriate for trusted household workloads. It is not a claim of
hostile multi-tenant isolation.

### Resource limits are for pacing, not just safety

The CPU limit exists as much to make a job *considerate* as to contain it. A
hard quota below one core lets an expensive search run for hours on a laptop
that is also being used for something else — the fans stay off and the machine
stays responsive, at the cost of proportionally longer wall-clock time.

For that to work, the container's thread pools must match the quota. A quota
caps CPU *time* but not the core count the container observes, and libraries
that size their pools from the visible core count will start far more threads
than the quota can run. They then spend the difference on context switching, so
the throttle costs more than it should. The worker pins the thread-pool
environment to the quota and advertises the quota to the job, so a script can
size its own parallelism to it rather than to the host.

## Trust boundaries

```text
public internet
      |
      | no port forwarding
      v
private overlay network        reachability only
      |
      +-- HTTPS  -> elevated API token or signed per-user session
      +-- SSH    -> key-based authentication
      +-- SMB    -> its own separate account
```

Network-level privacy and application authentication are separate concerns. The
overlay network supplies reachability; every service still authenticates
independently. Compromising one does not grant the other.

Secrets live in owner-only files outside the source tree and are never
committed. Uploaded files are stored under generated identifiers rather than
client-supplied names.

## Removable storage

A container bind-mounting a path on removable media will happily write to the
mount *point* when the media is absent — silently filling the system disk
instead of the intended volume.

The guard is to make the consumer prove the right filesystem is mounted: the
service unit verifies the expected filesystem UUID before starting, and the
mount itself is configured to let the machine boot without the disk. Absent
disk therefore means "service does not start," not "service writes to the wrong
place."

## Failure behaviour

| Failure | Result | Response |
|---|---|---|
| Worker sleeps or disconnects | Heartbeat goes stale; leased job requeued within its attempt limit | Restore the worker, or leave work queued |
| Worker administratively disabled | Keeps heartbeating, claims nothing; current job may finish | Re-enable when it should accept work |
| Coordinator stops | Submission and coordination stop; SQLite data remains durable | Inspect logs, restart |
| Overlay network down | Remote access stops; local network still works | Check the network daemon |
| Removable disk absent at boot | Machine boots; dependent service fails its mount check; API unaffected | Reconnect, mount, start the service |
| Disk pulled during a write | Processes may see I/O errors; recent data may be corrupt | Stop consumers, remount, check the filesystem |
| Database unavailable | Liveness may still pass while readiness fails | Restore the database before accepting work |

Liveness and readiness are deliberately distinct: a process can be alive and
still unable to serve.

## Decisions worth keeping

1. The coordinator coordinates; laptops compute.
2. The database owns job truth; workers mutate state only through the API.
3. Large files never travel in job rows or job payloads.
4. Network privacy is not authentication; each service authenticates anyway.
5. Host services own hardware and networking; containers isolate applications.
6. Consumers of removable storage must prove the intended filesystem is mounted.
7. Scheduling eligibility is durable control-plane state, independent of worker
   connectivity and execution state.
8. Untrusted code runs only inside a fixed image; the host agent never executes
   it directly.

## Known limits

- Placement uses fixed best-fit capacity, not benchmark scores, live load,
  thermal pressure, or data locality.
- The worker's content-addressed dataset, script, input, and project caches are
  bounded by a combined per-node LRU ceiling. This is a disk guard, not a
  data-locality scheduler: eviction may require a later job to download again.
- Published results are never evicted except by an explicit deletion or the
  size ceiling, which is intentional but means the store's growth is governed by
  operator discipline rather than by policy.
- Results pass through the coordinator rather than going directly to storage.
  Correct at this scale; resumable downloads reduce retry cost but not the data
  path. The natural fix at larger scale is direct object/storage transfer with
  pre-signed upload URLs, either of which takes the coordinator out of the byte
  path entirely.
- Telemetry is carried inside the claim and heartbeat messages rather than
  exposed separately, so monitoring a worker requires speaking the job protocol.
- Single coordinator, single database writer — this is not a highly available
  design, by choice.

# Compute

A durable job queue plus a fleet of borrowed laptops. This is the largest
application on the platform, and the one with the most moving parts.

| Page | What it covers |
|---|---|
| This page | How the engine works: lifecycle, leases, placement, data, isolation |
| [Job and API contract](job-contract.md) | The wire contract: endpoints, schemas, authentication |
| [Python script job](python-script.md) | Authoring one Python file against one input |
| [Batch script job](batch-script.md) | Authoring a PBS-like project with arrays and named inputs |

## The problem

Compute comes from laptops that are asleep, closed, or carried out of the house
without warning. A job dispatched to a machine that vanishes must not be lost,
must not run twice, and must not require anyone to notice.

That rules out assigning work to a machine. Work is *leased* instead.

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

`FAILED` is the single unsuccessful terminal state. A separate machine-readable
`failure_kind` says whether the cause was execution, infrastructure, a memory
limit, a timeout, final worker loss, or cancellation. The `error` field is
human-readable detail, not a code to parse.

### Leases

A claimed job carries a lease token and an expiry, and the worker renews it by
heartbeat. Sleep, network loss, or death stops the renewals, the lease expires,
and the coordinator requeues the job — up to an attempt limit, so a job that
reliably kills its worker eventually fails rather than looping forever.

Only the holder of the **current** lease token may complete or fail a job. A
stale token is rejected, so a worker returning from the dead cannot overwrite a
result produced by its replacement.

### Cancellation

Queued cancellation is immediate. Running cancellation is cooperative across
the network boundary: the coordinator records the request, the next heartbeat
carries it to the worker, and the worker force-removes only that job's
container before acknowledging. A cancellation requested before completion wins
the race — completion and artifact publication are refused afterwards. If the
worker is lost before acknowledging, lease recovery finalizes the cancellation
rather than requeueing it.

Heartbeat cancellation is *control*, not progress. The system deliberately
stores no epochs, percentages, live logs, or ETA; that needs a separate
bounded, rate-limited contract so fast loops cannot turn a small coordinator
into a telemetry sink.

## Claiming and placement

Workers poll; the coordinator never pushes. But the coordinator decides:

```text
eligible = enabled + online + idle + capable + request fits envelope

if target_worker_id:
    choose that worker, if eligible
else:
    choose the eligible worker with the smallest memory envelope,
    then smallest CPU envelope, then worker ID
```

The claim is a single atomic conditional update, so exactly one worker wins a
given job even under contention. A worker already holding a live lease is
handed back its existing job rather than a new one, so each machine runs at
most one job at a time.

The consequences are easy to mistake for intelligence, so state them plainly:

- A targeted job waits for that exact worker to become eligible. Targeting is a
  placement instruction, not permission to exceed a safety limit.
- An automatic job takes the *smallest adequate* machine, even when a larger
  one polls first. That keeps the large machine free for work that needs it.
- A busy machine is excluded, so the next job spills elsewhere. This produces
  crude but real load spreading: whoever is free takes the next job.
- Live CPU, memory, and temperature are displayed but never used for placement.
  They fluctuate far too quickly to be a stable policy.

A submission that no registered capacity could ever satisfy is rejected at
submission time rather than queued forever. Each worker's envelope sits below
its real resources so the operating system and interactive use keep headroom.

### Eligibility is separate from liveness

Every worker has a durable enable/disable switch that lives in the control
plane, not the worker. Disabling does not stop the process and does not change
whether it is online, busy, or stale. It atomically blocks *future* claims
while heartbeats continue, and a job already holding a lease finishes normally.
That is graceful draining, not remote cancellation.

Workers register with that switch **off**, so an accidentally-started agent
sits inert until someone deliberately enables it.

## Moving data

Large files never travel inside job rows or through the coordinator's memory.
Three paths exist:

**Linked inputs.** The job carries an HTTPS URL, an exact byte count, and a
SHA-256. The worker downloads straight from the source, verifies size and
digest before use, and caches by digest. The coordinator never sees the bytes.

**Uploaded inputs.** Small files submitted through the browser are staged by
the coordinator. Here the coordinator *is* in the byte path — which is why
uploads are size-capped and anything large should be linked instead.

**File-server inputs.** A user copies data onto the NAS with an ordinary file
client, then selects it by logical path. The coordinator records its size and
digest without copying it into staging, and streams it to the worker.

Verification always happens on the consuming side. A declared digest that does
not match what arrived is a hard failure, not a warning. See
[Storage](../storage/README.md) for the file areas themselves.

### Publishing results

A finishing worker uploads its output files to the coordinator, which stores
them on durable storage. The job record keeps only metadata — exit code,
truncated output streams, and file names. Four details make this work rather
than merely function:

**The coordinator decides where results land**, never the worker or the script.
Each submission publishes into its own directory, so no run can overwrite
another even when two share a name, and a script cannot aim output somewhere it
should not reach.

**The same lease that authorises completion authorises the upload.** Publishing
changes a job's output, so it demands the same proof as finishing it. A worker
whose lease expired cannot overwrite its replacement's results.

**The upload happens while the lease is still being renewed.** This is easy to
get wrong. If results are uploaded after the heartbeat loop stops, a transfer
slower than the lease interval makes the coordinator declare the worker dead
and requeue the job *while it is succeeding*. Large jobs would silently run
twice while small ones behaved perfectly.

**The write path refuses the wrong disk.** Where durable storage is a separate
volume, an absent disk leaves an ordinary writable directory at the mount
point, and results would quietly fill the system disk instead of failing.

### Keeping storage bounded

Every job leaves data in several places: staged inputs on the coordinator, a
cache and output directory on the worker, and the published results. Three
rules apply, and the asymmetry is deliberate:

- **Inputs are released when a job reaches a terminal state** — unless another
  unfinished job shares the same upload.
- **The worker's copy is deleted once publishing succeeds.** A *failed* publish
  leaves it alone, because it is then the only remaining copy.
- **Published results never expire by age.** They are what the job was for. A
  total-size ceiling exists only as a backstop against a runaway.

Inputs and intermediates are reconstructible, so they expire on a rule. Results
are not, so they expire on a decision.

## Isolating untrusted code

The system accepts user-supplied Python and Bash. The host worker agent never
imports or executes it. The worker stages the code and inputs into a per-job
directory and launches a **fixed, pre-built container image** with:

- no network;
- a read-only root filesystem;
- read-only inputs, with only the output directory writable;
- a non-root user, all capabilities dropped, no-new-privileges;
- CPU, memory, swap, PID, and wall-clock limits; and
- no credentials and no container-runtime socket inside.

The image is pinned and built ahead of time, never assembled per job, so a job
cannot influence its own runtime. A worker advertises the batch capability only
while it can actually see that image, re-checking periodically, so capability
appears and disappears on its own without restarts.

This suits trusted household workloads. It is not a claim of hostile
multi-tenant isolation.

### Resource limits are for pacing, not only safety

The CPU limit exists as much to make a job *considerate* as to contain it. A
hard quota below one core lets an expensive search run for hours on a laptop
someone is also using — the fans stay off and the machine stays responsive, at
the cost of proportionally longer wall-clock time.

For that to work, the container's thread pools must match the quota. A quota
caps CPU *time* but not the core count the container observes, and libraries
that size their pools from the visible core count start far more threads than
the quota can run, then spend the difference on context switching. The worker
pins the thread-pool environment to the quota and advertises the quota to the
job, so a script can size its own parallelism to it rather than to the host.

## Writing a job

Two authoring contracts. Choose the smaller one that fits.

| Job type | Use it when | Guide |
|---|---|---|
| `python_batch` | One Python file consumes one input file and writes results | [Python script job](python-script.md) |
| `batch` | A project needs Bash, several named inputs, or a numeric array of independent runs | [Batch script job](batch-script.md) |

Use a Python script job when the workload is one `.py` file with exactly one
input, the fixed scientific image has every dependency, and flat result files
are enough. Use a batch script job when the workload has several source files,
a Bash entrypoint, more than one input, or parameter sets that should run as
independently scheduled tasks.

Both choose inputs the same way and publish results the same way, and neither
lets a script pick its output destination — so the choice is about the shape of
the workload, not about how files get in or out.

# Documentation

A private homelab: one always-on host, a file server, and laptops that supply
compute when they are available. Several applications run behind a single
private entrance; a distributed compute system is the largest of them.

Start with [Architecture](architecture.md) — it is the overview of the whole
system and links to everything else.

## Start here

| If you want to… | Read |
|---|---|
| Understand the whole system | [Architecture](architecture.md) |
| Understand how anything is reached | [Network](network.md) |
| Run something on it | [Compute](compute/README.md) |
| Know where your files live | [Storage](storage/README.md) |
| Build against the API | [Job and API contract](compute/job-contract.md) |
| Know who may see or do what | [Accounts and access control](access-control.md) |
| Host another application | [Application services](services.md) |
| Deploy or configure it | [Configuration](configuration.md) |

## Sections

- **[Architecture](architecture.md)** — the overview: physical shape, the
  subsystems, trust boundaries, failure behaviour, and known limits.
- **[Network](network.md)** — the private overlay network, the reverse
  proxy that routes one HTTPS origin to several backends, why backends bind to
  loopback, and why that makes identity headers trustworthy.
- **[Compute](compute/README.md)** — the job engine: leases, atomic claiming,
  deterministic placement, the data plane, and container isolation. Its
  [wire contract](compute/job-contract.md) and the authoring guides for
  [Python scripts](compute/python-script.md) and
  [batch projects](compute/batch-script.md) sit beside it.
- **[Storage](storage/README.md)** — one file server, logical areas, ownership
  on disk and in the database, published results, and backups. See
  [files in and out of a job](storage/workflow.md) for the practical route.
- **[Accounts and access control](access-control.md)** — roles, sessions,
  immutable ownership, and linking a network login to a platform account.
- **[Application services](services.md)** — the standard for hosting
  long-running household applications beside the compute system.
- **[Configuration](configuration.md)** — every environment variable and which
  ones matter.

## The four ideas

**Jobs are leased, not assigned.** A worker claims work and renews a lease by
heartbeat. Lose the worker and the lease expires and the job is requeued, so
worker loss is survivable without a supervisor watching each machine.

**Compute is borrowed.** Workers register scheduling-disabled and advertise
what they can run. Placement is deterministic best-fit against fixed capacity
envelopes, not live load.

**Files are addressed logically.** Members see `Home`, `Shared` and
`Artifacts`, never a mount point or an account identifier. The server resolves
each against the signed-in session, so a typed path cannot reach another
account.

**Ownership is enforced server-side, everywhere.** Every job, upload and
artifact carries an immutable owner. Lists, reads, downloads, cancellation and
deletion each check it. A UUID is identity, never authorization.

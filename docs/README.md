# Documentation

A private home compute platform: an always-on coordinator schedules jobs onto
laptops that supply compute only while they are available, with files kept on a
household file server rather than passed through browser uploads.

## Start here

| If you want to… | Read |
|---|---|
| Understand how the system works | [Architecture](architecture.md) |
| Run something on it | [Job types](jobs/README.md) |
| Know where your files live | [Files in and out of a job](jobs/storage-workflow.md) |
| Build against the API | [Job and API contract](job-contract.md) |
| Deploy or configure it | [Configuration](configuration.md) |

## The four ideas

**Jobs are leased, not assigned.** A worker claims work and renews a lease by
heartbeat. Lose the worker and the lease expires and the job is requeued, so
worker loss is survivable without a supervisor watching each machine.

**Compute is borrowed.** Workers register scheduling-disabled and advertise what
they can run. Placement is deterministic best-fit against fixed capacity
envelopes, not live load.

**Files are addressed logically.** Members see `Home`, `Shared` and `Artifacts`,
never a mount point or an account identifier. The server resolves each against
the signed-in session, so a typed path cannot reach another account.

**Ownership is enforced server-side, everywhere.** Every job, upload and
artifact carries an immutable owner. Lists, reads, downloads, cancellation and
deletion each check it. A UUID is identity, never authorization.

## Reference

- [Architecture](architecture.md) — design, job lifecycle, leases, scheduling,
  isolation, trust boundaries, failure behaviour.
- [Job and API contract](job-contract.md) — job types, state machine, worker
  protocol, endpoints, authentication, resource limits.
- [Job types](jobs/README.md) — pick a contract, then follow
  [Python script](jobs/python-script.md) or
  [PBS-like batch script](jobs/batch-script.md).
- [Files in and out of a job](jobs/storage-workflow.md) — the `Home`, `Shared`
  and `Artifacts` areas, how each resolves, and how files reach a job and come
  back.
- [Configuration](configuration.md) — every environment variable and which ones
  matter.
- [Web interfaces](interfaces.md) — dashboard and Job Desk behaviour and scope.
- [Accounts and access control](access-control.md) — roles, sessions, immutable
  ownership, storage boundaries.

## Scope of these documents

Design and reasoning only. No deployment identifiers, addresses, accounts, or
machine paths; generic application defaults appear only where they are part of
the public configuration contract. Live deployment detail stays out of version
control.

## Conventions

- Describe behaviour as **implemented**, **deployed**, **live-verified**, or
  **pending**. These are not interchangeable, and planned architecture must not
  be written as though it already runs.
- Never commit credentials, addresses, hostnames, hardware identifiers, or
  machine inventory.

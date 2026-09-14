# Documentation

- [Architecture](architecture.md) — system design, job lifecycle, leases,
  scheduling behaviour, isolation model, trust boundaries, failure behaviour.
- [Job and API contract](job-contract.md) — job types, state machine, worker
  protocol, endpoints, authentication, resource limits.
- [Job types and authoring](jobs/README.md) — choose a submission contract, then
  follow its dedicated [Python script](jobs/python-script.md) or
  [PBS-like batch script](jobs/batch-script.md) standard. Both choose inputs by
  logical `Home/...` and `Shared/...` path and publish into a per-run directory
  named after the job; see
  [getting files into a job](jobs/storage-workflow.md).
- [Configuration](configuration.md) — every environment variable, what it does,
  and which ones matter.
- [Web interfaces](interfaces.md) — current dashboard and Job Desk behaviour,
  responsive design goals, and the next UI scope.
- [Accounts and access control](access-control.md) — member/admin roles,
  sessions, immutable ownership, the accepted pilot Workspace boundary, and
  remaining multi-user NAS acceptance.

These documents describe design and reasoning only. They intentionally contain
no private deployment identifiers, addresses, accounts, or machine paths.
Generic application defaults may appear where they are part of the public
configuration contract; live deployment details stay outside version control.

## Conventions

- Describe behaviour as **implemented**, **deployed**, **live-verified**, or
  **pending**. These are not interchangeable, and planned architecture must not
  be written as though it already runs.
- Never commit credentials, addresses, hostnames, hardware identifiers, or
  machine inventory.

# Architecture

Home Platform is a private homelab: one always-on host, a file server, and a
few laptops that supply compute when they happen to be awake. It hosts several
applications behind a single private entrance — a distributed compute system is
the largest of them, but not the point of the design.

This page is the overview. Each subsystem has its own section with the detail:

| Subsystem | What it owns | Detail |
|---|---|---|
| Network | Private reachability and request routing | [network.md](network.md) |
| Compute | Job queue, leases, workers, isolation | [compute/](compute/README.md) |
| Storage | Household files and published results | [storage/](storage/README.md) |
| Identity | Accounts, roles, ownership | [access-control.md](access-control.md) |
| Services | Hosting long-running applications | [services.md](services.md) |
| Configuration | Every environment variable | [configuration.md](configuration.md) |

This document contains no hostnames, addresses, accounts, or filesystem paths.
Those belong to a deployment, not to a design.

## The constraint that shapes everything

The hardware is one small always-on machine, a NAS, and laptops that come and
go. Most of the design follows from that:

- the always-on machine must stay responsive, so it coordinates and never
  computes;
- a laptop can vanish mid-job without warning, so work is *leased* rather than
  assigned; and
- one machine is the only writer of truth, so everything else mutates state
  through its API.

## Physical shape

```text
                    authorized devices
                  (phone, laptop, tablet)
                            |
                            | private overlay network, HTTPS
                            v
    +-------------------------------------------------------+
    |                    always-on host                     |
    |                                                       |
    |   reverse proxy  --+-->  control plane (jobs, web UI)  |
    |                    +-->  habit tracker container       |
    |                    +-->  ...future applications        |
    |                                                       |
    |   SQLite (job truth) · systemd lifecycle · backups     |
    +-------------------------------------------------------+
           |                                    |
           | workers poll: claim,               | SMB / mounted shares
           | heartbeat, report                  |
           v                                    v
    +-------------+  +-------------+      +--------------+
    |  laptop A   |  |  laptop B   |      |     NAS      |
    | per-job     |  | per-job     |      | household    |
    | container   |  | container   |      | files +      |
    +-------------+  +-------------+      | artifacts    |
                                          +--------------+
```

Three kinds of machine, with sharply different roles. The **host** is always on
and low power; it routes, coordinates, stores truth, and runs applications, but
never does heavy work. The **workers** are ordinary laptops that appear and
disappear. The **NAS** is the only file server and the durable home for
household data and job results.

## Getting in: the network

Nothing is exposed to the public internet. No router port is forwarded, and no
public tunnel is enabled. Every device joins a private overlay network
(Tailscale), which supplies *reachability only* — each service still
authenticates independently.

Inside that network, a single reverse proxy is the front door for everything
the host serves. It terminates HTTPS once and dispatches by path:

```text
    /          ->  127.0.0.1:8000     control plane: jobs, dashboard, API
    /habits    ->  127.0.0.1:8100     habit tracker
```

Every backend binds to loopback only, so the proxy is the sole route in, and a
proxy-supplied identity header is therefore trustworthy. Those two facts are
one decision. Workers are *not* reached through the proxy — they poll the
control plane outward — so nothing needs to connect back into a laptop.

This is the part most worth reading in full: [Network](network.md).

## Who you are: identity

One stable user ID identifies a person across every application, file, and job.
Network login and application identity are deliberately separate: the overlay
network tells a service which network account is calling, and a user links that
to their platform account once, explicitly, while signed in.

Every job, upload, and artifact carries an immutable owner, and authorization
is a server-side rule rather than a UI filter — knowing another person's UUID
grants nothing. Applications resolve identity through a narrow internal call
with its own least-privilege token; none of them opens the control-plane
database or holds the elevated API token.

Detail: [Accounts and access control](access-control.md).

## Where things live: storage

The NAS is the only file server. Members address it through three logical
areas — a private area, a shared area, and their own job artifacts — and never
through its real location. The server resolves each against the signed-in
session, so a typed path cannot reach another account, while filesystem
permissions enforce the same boundary independently for anyone connecting over
SMB.

Published job results are owned by the coordinator and exposed read-only to the
file share, because letting a share client delete them would create a second
writer with no way to reconcile the two. Control-plane truth is backed up
through SQLite's online backup API, verified, and copied to the NAS.

Detail: [Storage](storage/README.md).

## The main application: compute

The compute subsystem is a durable job queue plus a fleet of borrowed laptops.
A client submits a job; the coordinator records it in SQLite; an eligible
worker claims it with a single atomic update, runs it inside a locked-down
container, publishes its output files, and reports the result.

The ideas that make it survive unreliable hardware:

- **Leases.** A claimed job carries a renewable lease. If a worker sleeps or
  dies, the lease expires and the job is requeued, bounded by an attempt limit.
- **Atomic claiming.** Exactly one worker can win a given job, even under
  contention.
- **Deterministic placement.** The smallest adequate machine wins, so a large
  one stays free for work that needs it. Live load never influences placement.
- **Isolation.** User code runs only inside a fixed, pre-built image with no
  network, a read-only root, dropped capabilities, and hard resource limits.
  The host agent never imports or executes it.
- **A separate data plane.** Large files travel source-to-worker and are
  verified by size and SHA-256; they never sit in job rows.

Detail: [Compute](compute/README.md), its
[wire contract](compute/job-contract.md), and the authoring guides for
[Python scripts](compute/python-script.md) and
[batch projects](compute/batch-script.md).

## Everything else: application services

Long-running household applications are the second workload pattern, and the
reason the platform is not simply a job queue. Each is an independent container
with its own port, health check, lifecycle, resource limits, and database. They
know nothing about jobs and cannot read job-control tables; they share only the
host, the proxy, and the identity system.

Adding one is a deliberate, bounded exercise: a source directory, an image, a
loopback port, a systemd unit, one proxy route, explicit limits, and a tested
backup. The current example is a habit tracker.

Detail: [Application services](services.md).

## Human interfaces

The browser surface is split by responsibility rather than by widget:

- **Overview** — owner monitoring: host and storage health, worker telemetry.
- **Operations** — the state-changing owner tools: scheduling switches, account
  management, guarded host power.
- **Operator Jobs** — all-user workload history, ownership, cancellation, and
  results; deliberately no submission form.
- **Operator Files** — complete read access, constrained management of member
  Workspace trees, and deliberate artifact deletion.
- **Member Jobs and Files** — owner-scoped history plus the safe browser over
  Home, Shared, and Artifacts.
- **Member Submit** — the focused job-creation workflow.
- **CLI** — the primary automation surface, independent of browser state.

All of them speak to one API over the same signed-session mechanism and enforce
different roles: members see only their own records and may submit; the
operator dashboard sees every workload but has no browser submission path; and
the CLI and workers use a separate elevated token. Three rules keep them
honest:

**One vocabulary for files.** The same logical roots appear in the browser
picker, the CLI, and job headers, resolved per caller by the server.

**No output destination is offered.** A script choosing one could overwrite
another run, so the platform publishes each submission into its own directory.

**An unguessable UUID is never authorization.** Every list, read, download,
cancellation, and deletion re-checks the authenticated owner's scope.

## Operations

Every long-running piece is a systemd unit on the host, so the machine boots
into a working system without anyone logging in. Services expose `/health` for
liveness and `/ready` for readiness — deliberately distinct, because a process
can be alive and unable to serve.

Host power is guarded rather than exposed as a button: a confirmed request
writes a fixed marker file that a root-owned unit consumes, and it is refused
while any worker can still claim work or any job is running. The API itself
holds no privilege escalation.

## Trust boundaries

```text
public internet
      |
      | no port forwarding, no public tunnel
      v
private overlay network        reachability only
      |
      +-- HTTPS      -> elevated API token or signed per-user session
      +-- SSH        -> key-based authentication
      +-- file share -> its own separate account
```

Network privacy is not authentication. Compromising the overlay network does
not grant application access, and each service authenticates anyway. Secrets
live in owner-only files outside the source tree, and uploaded files are stored
under generated identifiers rather than client-supplied names.

## Failure behaviour

| Failure | Result | Response |
|---|---|---|
| Worker sleeps or disconnects | Heartbeat goes stale; leased job requeued within its attempt limit | Restore the worker, or leave work queued |
| Worker administratively disabled | Keeps heartbeating, claims nothing; current job may finish | Re-enable when it should accept work |
| Host stops | Submission and coordination stop; SQLite data remains durable | Inspect logs, restart |
| Overlay network down | Remote access stops; local network still works | Check the network daemon |
| Storage volume absent at boot | Machine boots; dependent service fails its mount check | Reconnect, mount, start the service |
| Database unavailable | Liveness may pass while readiness fails | Restore the database before accepting work |
| One application crashes | Only that container; jobs and other applications continue | Restart that unit |

## Decisions worth keeping

1. The host coordinates and serves; laptops compute.
2. The database owns job truth; everything else mutates state through the API.
3. Large files never travel in job rows or job payloads.
4. Network privacy is not authentication; each service authenticates anyway.
5. A proxied backend binds to loopback, because that is what makes its identity
   headers trustworthy.
6. One stable user ID is the owner everywhere; a network login is only an
   authentication method.
7. Applications share the host, the proxy, and identity — never a database.
8. Scheduling eligibility is durable control-plane state, independent of worker
   connectivity.
9. Untrusted code runs only inside a fixed image; the host agent never executes
   it directly.

## Known limits

- One host, one database writer. This is deliberately not a highly available
  design, and the host is a common power, disk, and network failure domain for
  every application on it.
- Placement uses fixed best-fit capacity, not live load, thermal pressure, or
  data locality.
- Job results pass through the coordinator rather than going directly to
  storage. Correct at this scale; the fix later is direct storage transfer.
- Published results never expire by age, so growth is governed by operator
  discipline rather than policy.
- There is no application progress protocol — only lifecycle, lease health, and
  cancellation state.

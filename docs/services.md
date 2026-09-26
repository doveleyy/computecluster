# Application services

The platform hosts small, long-running household applications beside the
compute system. Each is an independent service, not a new router inside the job
API.

## Why not just add it to the control plane

Adding a household app as another router inside the job API would share its
process, its database, its deploy cycle, and its failure modes. A dependency
upgrade for one becomes a risk for the other, and a crash in a habit tracker
takes the job queue with it.

Independence costs one container and one proxy route. That is cheap enough that
it is the default.

## The service contract

Every service has:

- a source directory under `services/`;
- its own container image and Compose definition;
- its own process, loopback-only port, health check, and durable state;
- a systemd lifecycle on the always-on host;
- one private HTTPS route through the reverse proxy;
- explicit CPU, memory, PID, filesystem, and privilege limits; and
- an online backup and tested restore procedure for its database.

The request path is the platform's standard one — private HTTPS to the reverse
proxy, then plain HTTP to a loopback port with authenticated identity headers.
Because the container port is published only on `127.0.0.1`, no other machine
can bypass the proxy and forge those headers, and the service refuses user data
routes when proxy identity is absent. See [Network](network.md).

Ownership uses the platform's stable user ID rather than the network login. A
user links the two once while signed in, and the service resolves the network
subject through a narrow internal call protected by its own least-privilege
token. A service never opens the control-plane database and never receives the
elevated job API token.

## When to create another service

Create one when a capability has its own lifecycle, data, or failure boundary.
Do **not** split one small application into several services merely because it
has several screens — that multiplies containers, backups, and routes while
adding no isolation anyone benefits from.

A future calendar synchronizer owning OAuth credentials and a schedule would be
a separate service. Another page of an existing app would not.

The host is a single machine, not a cluster. Independent containers stop
dependency and process failures from being shared, but power, disk, network,
and the container runtime remain a common failure domain.

## Current examples

One application with three pages — an Overview, Water for logging drinks, and
Budget for daily spending and a sinking fund. They are pages of one cohesive
app, not separate services, so they share a single container, identity
boundary, SQLite database, backup lifecycle, and tab navigation. It reads and
writes nothing in the job-control database.

It is served at `/habits`. Source and a development recipe are in
[`services/habit_tracker/`](../services/habit_tracker/README.md).

**Wishlist** tracks what a thing costs over time. It is a separate service
rather than another Habit Tracker page, because reading prices from other
people's shops is a different failure boundary: a shop can hang, rate-limit, or
change its response without notice, and none of that should reach an
application for logging daily habits. It is served at `/wishlist` with its own
container and database, and shares only the identity mechanism.

**Transport** keeps a deliberately small personal view of public-transport
data. Its train card downloads a GTFS timetable only on explicit refresh and
answers today's last-train lookup locally. Its bus card stores a short list of
stop/service pairs and requests live arrival estimates only when that card is
refreshed. Grouping saved services by stop avoids duplicate upstream calls.
Transport owns its cache, saved selections, credential, and upstream failure
boundary, so it is independent of both the control plane and other apps.

These services show the rule in practice — several screens of one idea stay
together; a capability that fails for unrelated reasons gets its own service.

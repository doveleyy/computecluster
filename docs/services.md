# Application services

The Home Platform can host small, long-running household applications alongside
the batch control plane. Each application is an independent service, not a new
router inside the job API.

## Service contract

Every service should have:

- a source directory under `services/`;
- its own container image and Compose definition;
- its own process, loopback-only port, health check, and durable state;
- a systemd lifecycle on the always-on host;
- one tailnet-only HTTPS route through the reverse proxy;
- explicit CPU, memory, PID, filesystem, and privilege limits; and
- an online backup and tested restore procedure for each local SQLite database.

The first implementation is `services/water_tracker`. It records owner-scoped
drink entries and settings in a dedicated SQLite database. Every entry stores a
stable classification code and temperature as well as amount and time. Tea and
coffee may also carry a constrained sweetness marker. Today and history APIs
return category breakdowns for later analysis, while raw totals remain literal
beverage volume rather than an inferred hydration score. It does not read or
write the job-control database.

## Request path

```text
authorized device
    |
    | HTTPS /water/... on the private tailnet
    v
Tailscale Serve reverse proxy
    |
    | HTTP to a loopback-only port + authenticated tailnet identity headers
    v
water-tracker container
    |
    v
water-tracker SQLite database
```

The container port is published only on `127.0.0.1`, so another machine cannot
bypass the proxy and forge its identity headers. The service refuses user data
routes when the proxy identity is absent. Its development-only identity header
works only after an explicit local opt-in and is disabled by the production
Compose definition.

Tailscale identifies the network caller, but durable application ownership uses
the existing Home Platform user UUID. A user explicitly links those identities
while signed into Job Desk through the private HTTPS proxy. The service then
resolves the Tailscale subject through a narrow internal API and stores the
returned Home Platform UUID. It does not open the control-plane database or
reuse the elevated job-system API token.

The reverse proxy is routing infrastructure, not the application. It terminates
private HTTPS and chooses a backend from the request path; the application still
owns validation, authorization, persistence, and its UI.

## When to create another service

Create a service when a capability has its own lifecycle, data, or failure
boundary. Do not split one small application into services merely because it
has several screens. The water tracker therefore keeps its UI, API, settings,
and history together. A future calendar synchronizer that owns OAuth credentials
and a schedule would be a separate service.

The Pi remains a single host, not a high-availability cluster. Independent
containers prevent dependency and process failures from being shared, but the
Pi is still a common power, disk, network, and Docker failure domain.

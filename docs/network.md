# Network

The network design provides private reachability and one HTTPS entry point.
Application authorization remains a separate responsibility.

## Private ingress

Authorized devices join a private overlay network. The platform does not need
router port forwarding or a public tunnel. A reverse proxy terminates HTTPS
and forwards requests to the core on loopback.

The core serves its own pages and APIs. Registered application paths pass
through the managed gateway to their corresponding containers. Adding an app
changes the registry rather than adding another external listener.

The [architecture diagram](architecture.md#components-and-ownership) shows the
request dependencies. Hostnames and routing configuration belong to each
deployment.

## Reachability and authorization

Overlay membership decides which devices can exchange packets. It does not
grant permission to read a job, open an application, or perform an
administrator action.

The core authenticates API clients and browser sessions. Where ingress supplies
an external identity, the core resolves it to an enabled, linked platform
account. An unlinked network identity does not become an application account.
Role and ownership checks still apply after authentication. See
[Accounts and access control](access-control.md).

## The host is a trust boundary

Backends publish on `127.0.0.1` rather than `0.0.0.0`. This blocks remote
clients from connecting directly and bypassing the ingress controls.

Local host processes can connect to loopback. Binding there does not prove
that a request came from the proxy. The host and configured ingress are
trusted, and the core's handling of forwarded identity depends on that
boundary. An untrusted process on the same host can violate its assumptions.

The ingress must replace client-supplied identity headers with authenticated
values. Development identity shortcuts must stay disabled outside local test
instances. A backend exposed off-host needs an authentication design that does
not trust caller-supplied headers.

## Applications verify signed context

Independent apps do not authenticate users from raw network identity headers.
The core gateway supplies signed, short-lived context bound to the app and
environment. The app verifies that context through the core API using its own
credential. Missing, invalid, or wrong-app context fails closed.

The app then checks ownership and permissions within its own domain. Routing
a request to an app does not authorize a database read or a file operation.
See the [runtime contract](apps/runtime-contract.md) for the full request and
verification rules.

Apps sharing the core's browser origin are trusted code. A separate container,
process account, or API credential does not protect the browser from malicious
scripts on that same origin.

## Workers initiate connections

Workers poll the core to claim work, renew leases, report status, and publish
results. The coordinator does not need to open a connection back to a worker.
Workers therefore need no inbound application listener or stable address.

Polling bounds the delay before work starts. Leases handle sleep, disconnection,
and worker loss without making network reachability a promise that a job is
still running. See [Compute](compute/README.md).

## Other protocols keep their own credentials

File-server access and host administration use separate authentication from
the web application. A platform password does not grant file-server or shell
access. Matching account names are a convenience, not a shared credential.

Direct file access also needs filesystem permissions that enforce the intended
ownership boundary. The core's path checks cannot protect a file-server client
that bypasses the core. See [Storage](storage/README.md).

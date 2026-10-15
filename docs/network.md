# Network

The network design provides private reachability and one HTTPS entry point.
Application authorization remains a separate responsibility.

## Private ingress

Authorized devices join a private overlay network. The platform does not need
router port forwarding or a public tunnel. Tailscale Serve runs on the
coordinator host, terminates HTTPS, and forwards requests to the host-native
core through loopback.

The core serves its own pages and APIs. Registered application paths pass
through the managed gateway to their corresponding containers. Adding an app
changes the registry rather than adding another external listener.

The [machine diagrams](architecture.md#machines-and-network) show the reverse
proxy, core gateway, application containers, worker agents, and NAS. Hostnames
and routing configuration belong to each deployment.

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

Managed app containers publish host ports on `127.0.0.1`. This prevents remote
clients from connecting directly to an app backend. The gateway inside the
core uses those loopback ports to forward requests.

The core has a different requirement. App containers call its scoped API
through the Docker host gateway, so its listener is reachable from that
network. Serve using a loopback upstream does not mean the core listens only
on loopback. The core authenticates callbacks with the app credential and
signed user context, independently of their network source. A direct request
does not gain a trusted browser identity by supplying forwarded headers.

Local host processes can connect to loopback. Binding there does not prove
that a request came from the proxy. The host and configured ingress are
trusted, and the core's handling of forwarded identity depends on that
boundary. An untrusted process on the same host can violate its assumptions.

The ingress must replace client-supplied identity headers with authenticated
values. Development identity shortcuts must stay disabled outside local test
instances. A core listener reachable off-host must preserve that proxy trust
boundary and enforce endpoint authentication. Its listen address alone
provides neither identity nor authorization.

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

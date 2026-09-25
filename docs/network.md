# Network

How a request reaches the right process, and why that is safe without exposing
anything to the public internet.

## Nothing is public

No router port is forwarded. No public tunnel is enabled. There is no
internet-facing hostname, so the usual homelab attack surface — an open port
scanned within minutes of appearing — simply does not exist.

Instead every authorized device joins a private overlay network (Tailscale).
Devices reach each other over encrypted peer-to-peer links, addressed by stable
names that do not change when a laptop moves between home Wi-Fi and a phone
hotspot. A device that is not on the overlay network cannot see the platform at
all.

The overlay network supplies **reachability only**. It answers "can these two
machines exchange packets", never "is this person allowed to do this". Every
service still authenticates independently, so a stolen laptop still faces a
login, and compromising the network does not grant application access.

## One front door

The host runs several HTTP services on different local ports. Exposing them as
`host:8000`, `host:8100`, `host:8200` would mean a certificate problem, a port
to memorise per application, and a new decision each time something is added.

Instead a single reverse proxy fronts all of them. It terminates HTTPS once,
then chooses a backend by looking at the leading path segment:

```text
        device on the overlay network
                     |
                     |  HTTPS to one private name
                     v
    +--------------------------------------+
    |     reverse proxy (Tailscale Serve)  |
    |  terminates TLS                      |
    |  authenticates the network caller    |
    |  attaches identity headers           |
    +--------------------------------------+
         |                        |
         | /                      | /habits
         v                        v
    127.0.0.1:8000           127.0.0.1:8100
    control plane            habit tracker
    jobs, web UI, API        Overview, Water, Budget
```

Adding an application means adding one route and one loopback port. Nothing
about the certificate, the hostname, or the firewall changes.

## Why backends bind to loopback

Each service publishes its port on `127.0.0.1`, never `0.0.0.0`. The
distinction carries most of the security of this design.

Bound to `0.0.0.0`, a container's port is reachable from any machine on the
network, and the proxy becomes one caller among many — an optional politeness
rather than a boundary. Bound to loopback, only processes on the host itself
can connect, and the proxy is the only route in from anywhere else.

That is what makes the next part work.

## Identity headers, and why they can be trusted here

The proxy knows which network account is calling, because the overlay network
authenticated it. It passes that along as request headers describing the
caller.

An application trusting such a header is normally a serious vulnerability.
Headers are trivially forged: anyone who can open a socket to the port can
claim to be anyone. Countless breaches begin with an internal service believing
`X-Forwarded-User`.

It is safe here for exactly one reason — **nobody can open that socket except
the proxy.** The loopback binding removes the forgery path, so the header means
what it says. Remove the binding and the whole scheme collapses silently, with
no error and no visible change until someone notices they can impersonate
anyone. Treat the two as a single decision that must not be separated.

Two habits keep it honest:

- A service **refuses user data routes when proxy identity is absent**, so if
  it is ever exposed by accident it fails closed rather than serving anonymous
  data.
- Any development-only identity header is off by default and disabled outright
  in the production configuration.

## The proxy is not the application

The proxy decides *where* a request goes. That is all. It does not validate
input, enforce ownership, or know what a job or a drink entry is. The
application behind it still owns validation, authorization, persistence, and
its own UI.

This matters when reading the rest of these documents: "the request reached the
habit tracker" and "the caller may read this data" are separate claims, settled
by different components. Path routing is never an authorization boundary.

## Workers connect outward

Compute workers are laptops on home networks, behind NAT, asleep half the time.
Nothing connects *to* them.

Instead each worker polls the control plane — claiming work, renewing leases,
reporting results — over the same private network, authenticating with its own
token. A worker needs no inbound port, no port forward, and no stable address,
and can join or vanish without any routing change. The cost is latency bounded
by the poll interval, which is irrelevant for jobs measured in minutes or
hours.

So the proxy fronts only what the host serves. The compute fleet is reached the
other way round.

## Other protocols on the same network

HTTPS is not the only thing crossing the overlay network, and each protocol
keeps its own authentication:

| Protocol | Purpose | Authenticates with |
|---|---|---|
| HTTPS | Web interfaces, API, applications | Session cookie or API token |
| SSH | Host administration | Key-based authentication |
| SMB | Household file access from Finder or Explorer | Its own file-server account |

Matching account names across these is a usability choice, never a shared
credential.

## Rules

- Do not forward router ports, and do not enable a public tunnel as a
  substitute for authorization.
- Do not publish a backend port on `0.0.0.0`.
- Do not trust an identity header on any service that is reachable off-host.
- Remove lost devices from the overlay network, and review its device list
  periodically.

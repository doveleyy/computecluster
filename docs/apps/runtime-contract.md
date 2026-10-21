# Application runtime contract

Contract version 1 defines the boundary between a managed app container and
the core. An app implements HTTP, uses the supplied configuration, and owns
its state. The core supplies user identity and checks permitted platform
operations. No core-source dependency is part of this contract.

## Container configuration

The platform supplies these environment variables.

| Variable | Meaning |
|---|---|
| `APP_ID` | Stable application identifier from its registration. |
| `APP_BASE_PATH` | Registered external route prefix, without a trailing slash. |
| `PLATFORM_API_URL` | Reachable URL for the versioned app API, including `/api/apps/v1`. |
| `APP_CREDENTIAL_FILE` | Read-only file containing this app's environment-specific credential. |
| `APP_STATE_DIR` | App-owned writable state directory. The container default is `/data`. |
| `TZ` | Operator-configured timezone. Portable examples use `UTC`. |

Read configuration at startup and refuse invalid required settings. Treat
`APP_BASE_PATH` as a URL prefix, not as a filesystem path. Keep credentials
out of logs, HTML, browser scripts, error messages, and published artifacts.
Read a credential from its file; do not expect the credential in the image.

The credential mounts at `/run/secrets/app-credential`. Additional secret files
mount at `/run/secrets/<name>`, so `app-credential` is a reserved binding name.
All credential and secret mounts are read-only. Credential rotation selects a
new host file; restart the container with that active file to use the new token.

Additional application settings are registered non-secret configuration.
Additional secret bindings supply read-only files through
`APP_SECRET_<NAME>_FILE`, with the binding name normalized to uppercase and
underscores. Binding names must normalize uniquely. App configuration cannot
replace platform-owned variables or choose secret host paths.

A registered integration binding supplies one operator-owned directory. It
mounts at `/integrations/<name>` and is named by
`APP_INTEGRATION_<NAME>_DIR`, normalized like a secret variable. The
registration requests the binding by name only. The operator's installation
policy chooses the host directory and whether it is read-only, so the release
can neither choose a host path nor reach anything the operator did not bind.
The release manifest has no binding field. Rehearsal containers receive
neither the mount nor its variable, so an app must start and pass readiness
without its integrations.

Each environment has separate app credentials, state, and identity authority.
A staging credential or assertion is invalid in production. The app cannot
select an environment by supplying a request parameter.

The authenticated private proxy must be the only source whose network peer
the core trusts for forwarded headers. App-to-core API connections must arrive
from an untrusted peer. Never trust every peer or relay container requests
through the trusted proxy address. A Docker Desktop callback to a host-native
core can appear as loopback and violate this boundary. Use an isolated Linux
network with separate proxy and app peers for managed identity acceptance.
Verify this distinction on the deployment target before enabling managed apps.

## HTTP and external routes

Listen on the container port declared in the release. Bind to all container
interfaces so the platform can reach that port. The platform keeps the backend
private and owns the published route.

The gateway strips the external prefix. With `APP_BASE_PATH=/apps/example`,
external `/apps/example/api/items?limit=10` reaches internal
`/api/items?limit=10`. Preserve methods, query strings, request bodies, and
response streaming. Generate browser links and asset URLs with the supplied
prefix. Relative redirects must resolve beneath that prefix; do not construct
URLs from caller-supplied proxy headers.

App cookies stay within the app's registered route. Every app cookie must have
an explicit `Path` equal to the route or beneath it. A cookie value may be a
token or a double-quoted token as RFC 6265 defines. `Domain`, reserved
`home_platform*` cookie names, and `Clear-Site-Data` are refused. Responses
that violate these rules fail with 502 before their bodies reach the browser.
The gateway removes `Service-Worker-Allowed` from responses. Apps share one
browser origin, so a service worker cannot widen its scope beyond its script's
directory under the app route.

The gateway removes platform cookies and incoming Tailscale, platform-context,
forwarded, real-IP, and hop-by-hop headers, then supplies its verified context.
It preserves request body framing. A request with `Content-Length` reaches the
app with that `Content-Length` and exactly that many body bytes. A request the
client sent with chunked transfer encoding reaches the app chunked, with no
`Content-Length`, including when the client sent both headers. A request
without a body reaches the app with no body. A body that disagrees with its
declared `Content-Length`, or a `Content-Length` that is not a single decimal
number, is refused with 400 before any body bytes reach the app. Apps may
therefore require `Content-Length` on uploads.
It retains the app's own cookies. It never shares a cookie jar between callers.
The bare app route redirects with 308 to its trailing-slash form. An upstream
redirect to the backend's own origin is rewritten beneath the external prefix.
WebSocket connections are unsupported and close with code 1008.

All browser requests through the gateway require verified user context,
including health paths. The gateway returns 503 without forwarding when the
core cannot read the identity records it needs. Routine health checks reach
the private backend
directly on loopback. Health endpoints must expose no user data.

The optional shared navigation document is `/platform/bar?app=<APP_ID>` on the
same external origin. It is navigation, not authorization. The core decides
which installed apps the viewer can access, and shows those the viewer pinned
from the launcher. Every installed app can frame the document, pinned or not;
an unpinned app's page shows the navigation without marking a current app.

## User identity

The gateway supplies an opaque `X-Platform-User-Context` assertion. An app sends
it to the core together with its own credential to resolve the current user.

```http
GET <PLATFORM_API_URL>/user
Authorization: Bearer <credential read from APP_CREDENTIAL_FILE>
X-Platform-User-Context: <assertion from the incoming gateway request>
```

The successful identity response has this shape.

```json
{
  "id": "11111111-1111-4111-8111-111111111111",
  "display_name": "Example user"
}
```

The UUID is stable across apps. Store it as the owner of app records.
Display names can change and are not ownership keys. An application's
existing namespaced ownership format can remain unchanged when its database
already uses the stable UUID.

Treat the assertion as opaque. The app does not mint it, select its subject,
trust unsigned decoded claims, or possess its signing key. Validate user
actions through the core's authorized API or the supported client before
reading or changing user data. A missing or forged context never activates a
fallback development user on a managed deployment.

The assertion binds the app ID, environment, linked user and provider subject,
issuance, and expiry. It expires after two minutes. The core checks current
account status and identity linking on API calls, as well as the app credential
and registration. Assertions can support several calls within their lifetime.
They are not single-use credentials. Do not retain them for background work.

The signing key stays in the core. A managed instance requires configured
administrator authentication and a separate context-key file. Each database
belongs to one environment; a different environment cannot reuse it at startup.

An administrator-restricted app requires an additional valid administrator
session at the gateway. That authority is distinct from the linked household
user. The assertion binds the administrator account and session version when
that authority is required. The core rechecks it on app API calls. The app
still accesses only the linked user's permitted, app-scoped jobs and files.

The browser supplies its session to the gateway, not to the application.
Normal linked-user apps need no separate dashboard login for permitted app
operations. Administrative installation and approval always require the
administrator session.

## Platform capabilities

The release requests capabilities and the administrator approves them.

| Capability | Permitted connection |
|---|---|
| `identity` | Resolve the gateway's current linked user. Required for every managed app. |
| `jobs` | Upload own inputs, submit installed workloads, inspect/cancel own app runs, and download their results. |
| `files` | Browse/select provisioned logical input files and obtain authorized input references. |

Capabilities grant operations, not ownership of other users' resources.
`files` does not permit general Workspace mutations, direct NAS mounts, or
reading another app's state. `jobs` does not permit arbitrary code submission,
runtime changes, worker administration, or result publication as a worker.

The app API namespace is `/api/apps/v1`. The API groups are current user,
inputs, file selection/references, runs, cancellation, and result downloads.
Run creation selects a registered workload and authorized inputs. The core
assigns ownership; request bodies cannot supply an owner UUID, host path, or
executable project. Detailed job/file wire schemas belong to their API
reference, not to the container startup configuration.

The core checks both app and user ownership on app-created uploads, runs, and
results. Logical input references retain current user/file authorization.
An administrator session does not disable these checks.

Submit keys are scoped to app and user and bound to the workload version and
payload. Retrying the same submission returns the same run; a different
payload with the same key conflicts. Authorization happens at submission.
The gateway assertion is not passed to the worker or stored in its project.

Read result manifests for filename, byte size, and SHA-256. Verify downloads
against those values. A successful upload or an available partial artifact
does not establish a completed run.

## Application state and runtime limits

The app owns `APP_STATE_DIR` exclusively. The core owns neither its business
schema nor its application migrations. Other apps cannot mount that state.
Core databases and platform secret directories are never app volumes.

The container runs as a non-root user with a read-only root filesystem,
dropped capabilities, no new privileges, a bounded temporary directory, and
operator-controlled CPU, memory, and PID limits. Actual limit enforcement is
an installation check, not a fact inferred from a Compose declaration.

Apps cannot request privileged mode, host networking, a Docker socket,
arbitrary host mounts, or host shell commands. Registered integration bindings
can grant a specific existing resource without granting arbitrary mount access.
Network access follows the installation policy; an app cannot assume either
unrestricted egress or complete network isolation from its manifest alone.

## Health and scheduled work

The release declares health, readiness, and version endpoints. Health reports
process liveness without accessing providers or shared storage. Readiness may
check the app's database and explicitly required dependencies. Routine
readiness polling is opt-in because dependencies can have a cost.

The declared health and readiness endpoints accept `GET`. A healthy process
returns 200 from health. Readiness returns 200 when the app can serve requests
using its required state and dependencies. It returns 503 while startup or
required dependencies prevent service. Their response bodies are informational
and the platform does not parse them.

The version endpoint accepts `GET` and returns 200 with a JSON object containing
`service` and `version`, both non-empty strings. `service` equals `APP_ID`.
`version` is an informational label supplied by the image, not an installation
identity. The platform verifies the installed OCI digest against the approved
release separately. Additional version-response fields are permitted.

Scheduled commands run inside the app container as its configured non-root
user, against only its own state and approved bindings. They do not run on the
host. A request assertion cannot authorize a future scheduled job after expiry.
Version 1 does not grant unattended submission on behalf of arbitrary users.

## Failure behavior

Use these HTTP meanings at the platform boundary.

| Status | Meaning |
|---|---|
| `400` | Malformed request or unsupported contract fields. |
| `401` | Missing, invalid, or expired app credential or user context. |
| `403` | Disabled/unlinked identity, unavailable authority, or denied capability. |
| `404` | Missing resource or a resource outside the caller's ownership. |
| `409` | Conflicting submission key or input changed after selection. |
| `413` | Input exceeds its documented size limit. |
| `502` | App upstream failed or returned a forbidden cookie or data-clearing response. |
| `504` | App upstream timed out. |
| `503` | Identity/core dependency unavailable, required storage not provisioned, or the app not yet installed or mid-switch on a managed host. |

Refuse actions when identity verification is unavailable. Keep a useful retry
message and do not substitute an anonymous user. Polling, retries, and token
refresh must not produce duplicate user actions.

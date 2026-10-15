# Integration sandbox

The sandbox is an operator-built container image that runs the real core and
the real worker with synthetic accounts. An app author starts it next to their
app container and runs integration tests against actual authentication,
gateway routing, workload execution, and verified result downloads. The local
fixture approximates the platform; the sandbox is the platform, minus the
operator's data.

It is a test tool. The image is labelled test-only, every account in it is
fictional, and its state is disposable. Never attach real storage, a
production database, or production credentials to it. A sandbox run proves
contract behaviour. It does not approve a release, verify an image digest, or
stand in for the preview guest.

## Prerequisites

Building and starting the sandbox needs Docker Engine with the buildx and
Compose v2 plugins. Docker Desktop includes both. Ubuntu's `docker.io` package
includes neither, so also install `docker-buildx` and `docker-compose-v2`, or
install Docker Engine from Docker's official repository. `build.sh` stops with
that advice when buildx is missing.

## Build the image

An operator builds the image from the core checkout and publishes it where
the app authors can pull it.

```sh
ops/sandbox/build.sh home-platform-sandbox:local
```

The image contains the core packages, their pinned dependencies, and the
Docker command-line client. It runs as a non-root user. Its entrypoint,
`python -m ops.sandbox`, seeds synthetic state and then serves the core. The
image also carries the launch recipe at `/sandbox/compose.yaml`.

## Start it next to an app

Copy `ops/sandbox/compose.yaml` into the app repository, or extract it from
the image, and run one command from that directory.

```sh
HP_SANDBOX_TOKEN=$(openssl rand -hex 32) docker compose up --wait
```

| Variable | Meaning |
| --- | --- |
| `HP_SANDBOX_TOKEN` | Required. At least 32 characters. Authenticates the test driver to the ingress. |
| `HP_SANDBOX_APP_IMAGE` | The app image under test. |
| `HP_SANDBOX_APP_ID` | The manifest `app_id`, supplied to the app as `APP_ID`. |
| `HP_SANDBOX_APP_ROUTE` | External route, supplied to the app as `APP_BASE_PATH`. |
| `HP_SANDBOX_MANIFEST` | `manifest.json` or its template. Unresolved commit, image, and digest markers receive synthetic values. |
| `HP_SANDBOX_ARCHIVES` | Directory holding the declared workload archives. |
| `HP_SANDBOX_IMAGE` | Sandbox image reference. Defaults to the local build tag. |
| `HP_SANDBOX_RUNNER_IMAGE` | Image that runs workload containers. Defaults to the sandbox image, which offers the standard library only. |
| `HP_SANDBOX_WORKER_DIR` | Host directory for worker job workspaces. |
| `HP_SANDBOX_PORT`, `HP_SANDBOX_API_PORT` | Loopback ports for the ingress and the core API. |

Put the variables other than the token in a `.env` file next to
`compose.yaml`. Compose reads it for every command, so `logs` and `down` work
in a new shell. Only `up` needs the token. When it is missing or short, `up`
reports that the core exited and `docker compose logs core` names the cause.
The same log names a missing workload archive, and a manifest or archive the
sandbox user (uid 10001) cannot read. Make both world-readable, for example by
cloning with the default umask 022. Compose refuses a manifest or archive
directory that does not exist.

The recipe starts three containers on one private network. `core` seeds and
serves the platform, `worker` executes workloads, and `app` is the author's
image with the platform variables from the
[runtime contract](runtime-contract.md). `--wait` returns after the core has
seeded its state, issued the app credential, and admitted the worker. The
app reads its credential from `/run/secrets/app-credential`, which the core
writes into a volume the app mounts read-only.

Both ports bind to `127.0.0.1` on the host. The ingress port is the browser's
path into the app. The core API port exists so a test can prove that an
untrusted peer gains nothing from forged proxy headers.

## Synthetic identities

The seed creates three linked accounts. The test driver selects one by
sending the proxy identity header through the ingress together with the token.

| Login | Account | Result through the gateway |
| --- | --- | --- |
| `alex@sandbox.test` | `alex`, enabled | Normal user |
| `sam@sandbox.test` | `sam`, enabled | Second normal user |
| `dana@sandbox.test` | `dana`, disabled | 403 |

```http
GET /apps/example/ HTTP/1.1
Host: 127.0.0.1:18001
X-Preview-Bridge: <HP_SANDBOX_TOKEN>
Tailscale-User-Login: alex@sandbox.test
```

The sandbox reuses the preview guest ingress, so the token header carries that
name. Each user owns `Home/Inputs/sample.csv` in synthetic member storage,
and `Shared/Inputs/shared.csv` is visible to both. The app API reports the
account name as the display name.

## How authentication stays real

The sandbox fakes nothing on the identity path. It changes only who the peers
are.

- The ingress runs inside the core container and forwards over loopback with
  `X-Forwarded-Proto: https`. The core trusts forwarded headers from loopback
  only, exactly as it trusts the deployed private proxy. The app container
  and the host are ordinary peers. Their forwarded headers are ignored, so
  the same request sent to the core API port returns 401.
- The gateway strips incoming platform headers and issues a signed assertion
  bound to the app, the `sandbox` environment, the linked user, and an expiry.
  A fixture value such as `test-alex` in `X-Platform-User-Context` is refused
  by the gateway when no proxy identity accompanies it and replaced when one
  does. The core API rejects it with 401.
- The signing key, administrator token, and app credential are generated at
  first start inside the state volume. The credential is issued by the real
  registry and verified on every API call. The database is pinned to the
  `sandbox` environment, so it cannot be reused by another environment.
- The core and worker read only `HP_SANDBOX_*` variables. The entrypoint
  refuses to start when any `HOME_PLATFORM_*`, `HP_FIXTURE_*`, `PREVIEW_*`,
  `APP_ALLOW_DEV_IDENTITY`, or `HOME_PLATFORM_DEV_USER` variable is present,
  so a sandbox cannot be pointed at real paths or fictional users by
  configuration.

Restarting the stack converges. Secrets are created once, the registration
and release are retained when unchanged, a changed manifest becomes a new
release and installation, and a missing credential file is rotated.

## The disposable worker

The worker container runs the unchanged worker program. It needs the host
Docker socket to start workload containers, and it bind-mounts
`HP_SANDBOX_WORKER_DIR` at the same path inside and outside so the paths it
hands to the host daemon exist there. Driving the host daemon is already
host-equivalent authority, so the container runs as root. Treat the machine
running a sandbox as a development or CI host, never as a server that holds
real data.

Workload containers run with the usual isolation flags and the runner image
from `HP_SANDBOX_RUNNER_IMAGE`. Build the scientific Python worker image from
`containers/ml-runner` and name it there when a workload needs more than the
standard library.

## Write the opt-in test

Skip the test unless `HP_SANDBOX_TOKEN` is set, so an ordinary checkout and
CI job stay local. Cover at least these checks.

1. Two users see their own name and only their own records.
2. An upload and submission complete, and the downloaded result matches the
   workload's expected output byte for byte. This proves the archive ran on
   the worker and the client verified size and digest.
3. One user cannot download the other's result.
4. Missing identity, the disabled account, a missing token, and a fixture
   context header are refused.
5. Proxy headers sent to the core API port are refused.

The starter repository's sandbox test follows this list.

## Limits

Template markers are resolved with synthetic values, so the sandbox does not
check the image digest or commit of a release. Administrator-restricted apps
need an administrator session, which the sandbox does not provide. There is
no TLS; the ingress sets the forwarded protocol itself. Capacity is one
worker with one CPU and 512 MiB per job.

Stop and remove everything with `docker compose down -v`, then delete the
worker directory. It defaults to `.hp-sandbox-worker` in the directory where
you ran `docker compose`. On Linux the worker container runs as root, so the
job workspaces it creates there are root-owned and an ordinary user cannot
delete them. From the same directory, empty the directory through a throwaway
root container, then remove it.

```sh
docker compose down -v
dir=${HP_SANDBOX_WORKER_DIR:-$PWD/.hp-sandbox-worker}
docker run --rm -v "$dir":/worker alpine find /worker -mindepth 1 -delete
rmdir "$dir"
```

The container cannot remove its own mount point, so `rmdir` removes the empty
directory. That needs write access only to its parent.

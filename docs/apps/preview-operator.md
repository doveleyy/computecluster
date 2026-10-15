# Run a preview guest

Provision a dedicated Linux guest with Docker and a private HTTPS reverse proxy.
Use a separate enrollment identity and synthetic storage. Do not mount
production
storage or copy production credentials into the guest.

Install the platform's Python dependencies and source in the guest. Build a
fixed core image containing the source, Python dependencies, and Docker CLI.
Preload the approved scientific Python worker image. Record both local image
IDs with `docker image inspect`. The guest configuration accepts immutable
`sha256:` image IDs for these two operator-controlled images.

Create a separate staging-control token file with at least 32 random characters.
Configure `python -m ops.preview_guest --config guest.json` with these fields.

| Field | Meaning |
|---|---|
| `root_directory` | Absolute directory for the guest journal and generated state. |
| `public_origin` | Private HTTPS origin without a path or trailing slash. |
| `control_token_file` | Guest-local staging-control credential file. |
| `core_image` | Fixed local core image ID, including `sha256:`. |
| `worker_image` | Fixed local scientific Python image ID, including `sha256:`. |
| `fixture_login` | Proxy identity mapped to the generated synthetic member. |
| `apps` | Map from application ID to synthetic bindings. |
| `port` | Loopback control listener, default 8090. |

Each `apps` entry has `secret_files`, a map from declared secret names to
synthetic files, and `settings`, a map of application environment variables.
An optional `integrations` map names synthetic directories for integration
bindings. Each attempt registers those names and mounts a fresh copy of each
directory taken into its own state, so one attempt never sees another's
changes or alters the fixture. A missing fixture directory refuses the attempt
before Docker runs.
State directories, application credentials, and platform variables are generated
by the guest. The application never receives the staging-control token.

Run the guest process as a non-root Docker-group member. The process binds only
to loopback. Configure the private HTTPS proxy to replace user identity headers
and forward to that listener. The core contacts the configured HTTPS origin
with certificate verification and refuses redirects.

Use `python -m ops.apps --database DATABASE check-updates APP` to discover
releases. Add `--token-file FILE` for a private repository. The result lists
releases still waiting for verification under `waiting` and releases that
exhausted their verification attempts under `abandoned`. Use `preview APP
MANIFEST_SHA256 --origin HTTPS_ORIGIN --control-token-file FILE` to select one
verified release. `preview-status ATTEMPT_ID` reconciles the existing attempt.
`retire-preview ATTEMPT_ID` removes its containers before permitting
replacement.
Both commands require the same origin and control-token options.

The returned browser URL stays on the configured staging origin. It uses
`/previews/ATTEMPT_ID/APP/`. The generated member accesses that application
through the guest core's existing gateway and app API. The generated worker
uses separate credentials and accepts jobs only from its synthetic core.

App, core and worker containers share one internal bridge per attempt, each
with its own network namespace. They publish no host ports. The guest resolves
owned container addresses for readiness checks and browser forwarding. It
rejects changed ownership, external or extra network attachments, missing
addresses and unexpected published ports. Browser requests go through the
core's token-protected ingress, which forwards through core-local loopback.
The app cannot use forwarded headers to impersonate that trusted proxy.

The fixed v1 budget admits at most two installing or ready previews, with only
one installation in progress. Each preview has 256 MiB limits for its core,
worker daemon and application, plus one worker job capped at 512 MiB and one
CPU. The two previews therefore have a 2.5 GiB container budget, leaving
1.5 GiB in a 4 GiB guest for the operating system, Docker and installer. Each
core and worker daemon has a one-CPU quota; each application has a half-CPU
quota. These quotas
share the guest's two vCPUs and do not promise dedicated CPU capacity.
A transient operator-owned identity probe has a 64 MiB limit and a tenth-CPU
quota. It runs during installation or sequential restart verification.
Retirement frees a resource slot only after cleanup succeeds. Uncertain cleanup
keeps its installation slot; restarting the guest adopts verified ready
previews.

The guest retains completed attempt records and synthetic state for
review. Its bounded journal refuses additional installs when retention is full.
An operator can replace the disposable guest after exporting required evidence.

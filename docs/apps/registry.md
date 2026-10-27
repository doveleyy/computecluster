# Application registry

The registry keeps operator configuration separate from app-published releases.
The launcher, shared navigation, and optional application monitoring read the
same installed registrations. Home, Job Desk, and Dashboard remain built in.
Navigation visibility does not authorize access to an application's routes.

The launcher is the full library: it shows every installed app the viewer's
role can open. The shared navigation shows the builtins and only the
registered apps that the viewer pinned from the launcher. A pin is a
preference, not a grant. An app that is not installed, including an app in
retirement, or that the viewer's role cannot open, stays out of the navigation
even when pinned. Retirement keeps every pin, so a restored app returns to the
navigation of the users who pinned it.
A newly registered app starts unpinned for everyone, so it appears on the
launcher at once and in a user's navigation only after that user pins it.

## Registration

For a new managed app, use [Register a published application](onboarding.md).
That command builds one registration for both the core and root host policy,
then prepares its account, directories and credential. The schema below also
supports operators who manage those resources separately.

An operator definition has these fields.

| Field | Meaning |
|---|---|
| `app_id` | Stable lowercase app identifier. Builtin identifiers are reserved. |
| `route` | Canonical lowercase external prefix, without a trailing slash. |
| `source` | A published-source policy or a platform-checkout source. |
| `access` | `linked-user` or `administrator`. |
| `capabilities` | Unique grants from `identity`, `jobs`, and `files`. |
| `presentation` | Labels, accent, position, and optional descriptive text. |
| `bindings` | Operator-owned configuration and secret-file references. |
| `installation` | A legacy or managed installation observation, or absent. |

A published source has `kind: published`, a repository identity such as
`example/notes`, a publish branch, a workflow path, and an image repository.
A platform-checkout source has `kind: legacy` and
`owner: current-platform-checkout`. It does not assert an independent
repository or an immutable image release.

Presentation contains `label`, `health_name`, `position`, and `accent`.
`kicker` and `description` are optional. Position determines order alongside
the builtins; the app ID breaks ties. The shared navigation lists apps that
every role can open before role-specific apps, and keeps position order within
each group. Pins choose which apps appear, never their order. The accent is a
six-digit hex color.

Bindings contain named `secret_files`, non-secret `settings`, optional state
and backup directory references, named `integration_bindings`, and declared
resource policy. A secret binding has `name` and `file_path`, never a credential
value. Settings have `name` and `value`. Platform-owned environment names and
normalized secret-file and integration variables cannot be overridden. An
integration binding is a name only; the host directory it stands for lives in
the root installation policy. Binding references do not appear in public
navigation or monitoring responses.

Resource policy has `cpu_limit`, `memory_bytes`, `pids_limit`, and
`maintenance_timeout_seconds`. These are declared operator bounds; a
registration alone does not establish runtime enforcement.

An installation observation has `owner: legacy-scripts`, an operator-selected
loopback `host_port`, and an optional `probe`. It records a service operated
outside the registry. Recording this observation neither starts a container
nor configures its route. An absent installation stays out of navigation and
routine monitoring. A release record does not change installation ownership.

A managed observation has `owner: platform`, an allocated loopback `host_port`,
and a required `v1-status` probe. It requires an absolute app state directory.
The core gateway serves its registered route and verifies its caller. Recording
this observation still does not start a container or approve a release. Where
an installer policy is configured, the gateway serves a managed app only from
an accepted installation, so the app joins the launcher and system health
monitoring, and can be pinned to the shared navigation, with its first accepted
install rather than showing a link the gateway refuses or a probe of a
container that does not exist yet. A retirement reverses that. While an app's
latest accepted generation is a retirement, it leaves the launcher, the shared
navigation, system health and routine monitoring, and the gateway answers its
route with 503 and a detail that names the retirement. See
[Retirement and restore](installation.md#retirement-and-restore).
Previews have no installer policy and list their app at once.
Existing script-managed installations retain their separate hosting path.

State and backup directories are absolute operator paths without traversal or
mount-syntax separators. Managed secret-file paths follow the same rule. The
reserved `app-credential` binding cannot replace the platform credential mount.

Routes cannot overlap another registration or a core route tree. The check
compares path segments, so `/notes` conflicts with `/notes/admin`, while
`/notes-extra` is distinct. Ambiguous routes, unknown fields, and incompatible
types are rejected before insertion. An identical registration is idempotent;
a changed definition for the same app ID is a conflict.

## Release records

The registry validates a release against the
[release contract](release-contract.md) and the operator's registered app ID,
image repository, capability grants, secret names, and maintenance bounds.
Only a published-source registration accepts release records.

Each record retains the exact input bytes and their SHA-256. Repeating the same
bytes returns the same record. Different bytes create a distinct record even
when the parsed manifest has the same meaning. Updates and deletions of release
rows are refused. Recording a release establishes neither CI provenance nor
preview success, administrator approval, or production installation.

## Operator command interface

The local operator command requires an explicit database and JSON documents.
The database must already exist at the current schema version. The command
does not create or migrate a database; the control plane migrates it at
startup. A missing or older database returns a nonzero exit status.

JSON is a valid YAML subset; this command does not parse general YAML syntax.
Unknown fields are rejected recursively. Input size and parsing are bounded;
duplicate keys and non-finite numbers are invalid. A validation error names
each failing field and does not repeat the input value.

```sh
python -m ops.apps --database <database> register <registration.json>
python -m ops.apps --database <database> ingest <app-id> <manifest.json>
python -m ops.apps --database <database> list
python -m ops.apps --database <database> credential <app-id>
python -m ops.apps --database <database> credential <app-id> --rotate
python -m ops.apps --database <database> runtime <app-id> --image <image> --port <container-port>
```

Write commands return `recorded` or `unchanged`. Invalid input and conflicts
return a nonzero exit status. The list contains safe metadata, without private
bindings. The command provides no deployment, approval, or replacement action.

`credential` writes a private file and records its verifier, environment, and
history. It never prints the token. A second issue requires explicit rotation.
Rotation uses a fresh file so failure leaves the old active credential usable.
Credential files stay outside app-owned writable state.

`runtime` prints a hardened Docker command from the registration, active
credential, and pinned instance configuration. It starts nothing. The command
includes read-only credential and secret mounts, private writable state,
platform environment variables, loopback publication, and resource bounds.
Use `--uid` and `--gid` to select the non-root container identity; both default
to `1000`. State and secret-file permissions must admit that identity.

## Routine monitoring

An installed registration opts in by declaring a probe with `service`,
`ready_path`, `version_path`, and `readiness`. Paths default to `/ready` and
`/version`. The host is fixed to loopback; the operator supplies the port.
At most four probes run concurrently, with bounded response sizes and socket
timeouts. Redirects are not followed. An empty monitored set returns no probes.

`legacy-json` readiness requires the existing JSON `status: ready` response.
`v1-status` uses HTTP 200 and treats the body as informational. The version
response must identify the expected service and a nonempty version. Existing
service aliases can be recorded without changing the navigation identifier.

Readiness that touches shared storage can stay out of routine monitoring.
Neither the launcher nor the shared navigation document initiates probes.

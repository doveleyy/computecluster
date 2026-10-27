# Application release contract

Contract version 1 separates three records. The registration belongs to the
operator. The release belongs to the app repository. An approval selects an
exact release and its effective installation configuration.

## Operator registration

Register an app once with its stable ID, trusted repository and publish branch,
configured CI workflow, permitted image repository, external route, access
policy, and named environment bindings. The access policy is `linked-user` or
`administrator`. It is not an app-defined permissions language.

The operator supplies private addresses, host port allocation, resource bounds,
secret-file locations, state locations, backup destinations, and staging
connections. App release metadata cannot assign these values. Changed bindings
or capability grants require approval before affecting production.

Home, Job Desk, and Dashboard are core destinations. Managed app registrations
provide navigation and optional health monitoring. Navigation visibility does
not replace server-side access checks.

## Release manifest

An app publishes its built image and a final `manifest.json` release asset.
Source
templates can contain build placeholders, but the published manifest contains
resolved commit and digest values. Publish it only after required artifacts
exist.

The manifest uses these fields. Unknown fields are errors in version 1,
including fields inside every nested object and list entry.

The [release JSON Schema](release-schema.json) describes version 1's public
shape. Its `x-home-platform-constraints` and `x-home-platform-document`
metadata describe parser rules that schema consumers must also enforce.
The [conformance corpus](release-conformance.json) contains complete accepted
and rejected manifests checked against the core parser. Schema validation
alone cannot prove name uniqueness after normalization or strict JSON integer
representation. The schema is generated with
`python -m ops.export_app_contract`; `--check` refuses stale reference bytes.

| Field | Required | Meaning |
|---|---|---|
| `contract_version` | Yes | Integer `1`. |
| `app_id` | Yes | Stable lowercase identifier matching the registration. |
| `source_commit` | Yes | Full commit SHA of the successful configured build. |
| `image` | Yes | Approved OCI image reference pinned by `sha256` digest. |
| `runtime` | Yes | Container port and health/readiness/version endpoint paths. |
| `capabilities` | Yes | Unique names from `identity`, `jobs`, and `files`; includes `identity`. |
| `state` | No | Object containing the app-owned SQLite filename for the backup strategy. |
| `secrets` | No | Unique requested binding names, never values or host paths. |
| `maintenance` | No | Named in-container commands and requested cadence/timeout. |
| `workloads` | No | Named, immutable compute-project assets for the `jobs` capability. |

An app ID uses lowercase letters, digits, and single hyphens, starts with a
letter, and has at most 63 characters. Container ports are integers from 1 to
65535. Health endpoint paths are absolute app-internal paths without query
strings or fragments. They are not URLs to remote hosts.

`runtime` contains `port`, `health_path`, `ready_path`, and `version_path`.
Health endpoints follow the [runtime response contract](runtime-contract.md).
When present, `state` contains the required string field `sqlite_file` and no
other fields. A SQLite filename is a basename
relative to `APP_STATE_DIR`, not a path with directories, traversal, or
symlinks. Apps without `state` receive a private writable state directory but
do not claim the SQLite backup strategy.

Each maintenance entry has a unique `name`, an argument list `argv`,
`interval_hours`, and `timeout_seconds`. Its interval is a positive whole
number of hours that the host policy restricts to the calendar forms listed in
[installation](installation.md), and its timeout is positive and bounded by
operator policy.
The installer passes arguments directly to a command inside the fixed app
container; it never evaluates them as host shell text. Effective schedules
belong to registration and appear in the approval. An operator can retain an
existing calendar schedule instead of changing its timing to a relative timer.
Scheduled work is disabled in a preview unless explicitly enabled for a check.

Each workload entry has a unique `id`, a release `asset` basename, a `sha256`,
an `entrypoint` relative to the archive root, and `runtime` equal to
`scientific-python:1`. The archive uses the existing
[batch project contract](../compute/batch-script.md). Its script declares fixed
inputs and bounded resources. App users cannot replace its executable,
runtime, or resource policy in a run request. Workloads require `jobs`.

Named secret bindings must be registered for the app and normalize uniquely
to their runtime variable names. Requests for unknown bindings fail validation.
Resource grants and filesystem access come from platform policy, not arbitrary
Docker options or an app-authored Compose file.

See the [identity-only example](../../examples/apps/identity/app.yaml) and
[compute example](../../examples/apps/csv-report/app.yaml). Their image and
archive digests are illustrative values, not downloadable artifacts.

## Build and publish

The configured publish branch runs tests and contract checks, builds supported
container architectures, and publishes the final manifest and workload assets.
The workflow also records manifest, image, and asset digests with its source
commit. A developer's local checkout does not need to reach the deployment
host or contain its credentials.

The core fetches releases through outbound requests. Eligibility requires the
registered repository, configured successful workflow, publish branch, source
commit, and matching workflow-produced artifact metadata. A release cannot
prove this merely by claiming that its checks passed.

Pin deployable artifacts by digest, not by a mutable tag or version label.
Bound manifest/archive sizes before parsing or extraction. Reject unsafe
archive entries with the existing project validator. Download only registered
release sources and image repositories; release text cannot select a new host
for credential-bearing requests.

## Preview and approval

A preview uses the same release artifacts as production, with separate accounts,
credentials, signing authority, state, hostname, and synthetic file storage.
Its worker is a disposable test worker. No production NAS mount, worker token,
or app credential belongs in the preview.

The core requests and reads preview operations through an authenticated
private control connection. The result identifies the app, release fingerprint,
preview attempt, actual image digest, and checks. It also carries the version
string the app reported at its version path, when that string is short
printable text; the value is for display only. Staging has no production
deployment authority.

An administrator approves the release manifest's exact bytes, image/workload
digests, effective installation bindings and capabilities, and a successful
preview attempt. New release bytes or changed security-relevant bindings
invalidate that approval. A later push cannot replace an approved selection.
Members and publisher credentials cannot approve production installation.

The visible workflow remains a pending update, its preview and checks, and
Approve and deploy, Reject, Rollback, or Retire. An app in retirement returns
through the same preview and checks with Approve and restore. Durable operation
records distinguish pending work from successful installation without requiring
users to manage internal phases.

## Installation and recovery

The web process requests a bounded operation. A privileged host helper checks
the approved app/release/configuration before changing its fixed installation.
The web process holds no Docker socket or general root command authority.

Installation verifies images, backs up SQLite state, switches the image,
checks routing/health/authentication, and records the actual running digest.
Operations are serialized per app/environment and reconcile actual state after
interruption rather than starting duplicate installations.

Version 1 requires backward-compatible database migrations. Verify that the
previous image can use disposable state after the new image migrates it.
Reject ordinary approval if that test fails. Code rollback restores a previous
image against the current compatible database. It never silently restores an
older database or discards newer writes.

A destructive schema change needs a separate data migration and recovery
decision. Backup restoration is a distinct operation and must be exercised on
disposable state. An image's readiness alone proves neither safe ownership
nor recoverable data.

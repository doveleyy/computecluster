# App compute API

The app compute API runs operator-installed workloads on the native job
scheduler. Every request requires the app credential and a current signed
user context from the [runtime contract](runtime-contract.md). Ownership is
the exact linked user ID, including requests with an administrator binding.
All responses use `Cache-Control: no-store`.

## Workload installations

An installation binds an app to one immutable release and workload. The
operator installs a declared ZIP through this command.

```sh
python -m ops.apps --database DATABASE install-workload APP_ID RELEASE_SHA WORKLOAD_ID ARCHIVE
```

The command checks the release's ZIP digest, entrypoint, runtime, archive
safety, and batch directives. A retry returns the same installation ID.
Ingesting a release alone does not make its workloads available.

`GET /api/apps/v1/workloads` returns the installations available to the app.

```json
{"active_release_sha256":"64 lowercase hex characters or null","items":[{"id":"installation UUID","release_sha256":"64 lowercase hex characters","workload_id":"report","inputs":["data"]}]}
```

The list retains installations from earlier releases. The active digest
comes from the latest accepted installation in the current environment, not
the newest published candidate. It is `null` before an accepted installation
exists. Select an installation by workload ID and release digest; keep its ID
when saving an intent for replay. The [HTTP reference](api-reference.md)
shows the complete request sequence and links to the generated OpenAPI file.

The ZIP fixes executable code, runtime, resources, environment, array, and
input names. The app supplies data bindings only. Worker selection, executable
uploads, owner selection, raw filesystem paths, and execution overrides are
absent from this API.

## Input creation

`POST /api/apps/v1/workloads/{installation_id}/uploads` accepts a nonempty raw
`application/octet-stream` body up to 20 MiB. A successful upload returns 201.

```json
{"id":"input UUID","size_bytes":123,"sha256":"64 lowercase hex characters"}
```

The input belongs to the calling app, linked user, and installation. It cannot
be reused with another installation, even if both installations use identical
code. Canonical input bytes survive native run cleanup. Each run receives
fresh execution copies.

Retained uploads are limited to 200 MiB and 100 uploads per app and user,
with a 1 GiB total bound across all apps and users. An upload reserves 20 MiB
atomically before reading its body, then replaces that reservation with the
actual size. A failed request releases its reservation. Logical handles copy
no bytes and do not count toward the byte limits. Each app and user can hold
at most 100 live handles. Only live inputs count toward these limits.
The single core process reconciles interrupted uploads before accepting
requests after startup. It removes uncommitted and deleted input files and
reservations, while preserving every live input object. Concurrent core
processes sharing one database and input directory are outside this
lifecycle contract. Automatic retention expiry is outside this contract.

`DELETE /api/apps/v1/inputs/{id}` returns 204 and deletes one of the caller's
inputs. Only the owning app and user can delete it. For an upload, the core
removes the retained bytes. The input's provenance record remains with a
deletion time, and the deletion cannot be undone. A deleted input cannot be
used in a new submission, and it no longer counts toward any quota.
Repeating the request returns 204. Submission copies each input into the
run's own staging, so deletion never affects a submitted run, its replay, or
its results.

`GET /api/apps/v1/files?path=Home&limit=100&offset=0` lists logical entries.
`path` starts with `Home` or `Shared`. `Home` resolves to the linked user.
The response has an `items` array with `name`, logical `path`, `kind`, and
`size_bytes`. Directories have a null size. Symlinks are excluded.

`POST /api/apps/v1/workloads/{installation_id}/file-handles` accepts a logical
file selection and returns the same 201 input shape as an upload.

```json
{"path":"Home/Inputs/data.csv"}
```

File operations require the current `files` grant and member-storage
entitlement. Creating a handle also requires the installed release's `files`
grant. Submission rechecks entitlement and copies through a confined file
descriptor. A changed fingerprint or digest rejects the handle. Changes after
successful submission do not alter the run's snapshot.

## Run submission and status

`POST /api/apps/v1/runs` requires an `Idempotency-Key` header of 1 to 128
characters from letters, digits, `.`, `_`, `:`, and `-`. The JSON body contains
exactly the installation and declared input bindings.

```json
{"installation_id":"installation UUID","inputs":{"data":"input UUID"}}
```

A new run returns 201. Repeating the same app, user, key, and body returns
200 with the existing run. Changing the body under that key returns 409.
Replay survives restart and input cleanup, and does not require available
worker capacity. Current authentication and grants still apply.

At most four app run groups can be staging, queued, or running globally.
Each app and user can hold at most two of those slots. Admission reserves a
slot before copying files. New requests return 409 when all slots, or the
caller's two slots, are occupied. Equal-key replays still return the existing
run.
Cancellation or completion frees a slot once every native child is terminal.
Each run may stage at most 200 MiB of inputs, counting repeated input IDs
once per input name, plus a project archive of at most 20 MiB. Four groups
therefore retain at most 880 MiB of native staging, apart from cleanup in
flight. Retained canonical inputs have their separate quota. Startup removes
interrupted staging attempts and preserves committed runs and canonical inputs.

```json
{"id":"run UUID","installation_id":"installation UUID","status":"QUEUED","tasks":[{"index":0,"status":"QUEUED"}]}
```

`GET /api/apps/v1/runs?limit=100&offset=0` returns an `items` array of that
shape. `GET /api/apps/v1/runs/{id}` returns one run. Pagination accepts limits
from 1 to 100 and offsets from 0 to 100000.

Statuses are `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, and `CANCELLED`.
Task indices are zero-based positions in the native group. The workload's
batch array index remains an execution detail. A completed run reports the
native lifecycle; result publication is checked separately.

`POST /api/apps/v1/runs/{id}/cancel` returns 200 and the current run. Queued
tasks cancel immediately. Running tasks receive native cancellation requests.
Repeated requests preserve completed tasks and converge on cancellation of
unfinished tasks. A terminal failure takes precedence over cancelled tasks
in the aggregate status.

## Complete results

`GET /api/apps/v1/runs/{id}/results` returns results only when every child
completed, every completion identifies the installed project digest, and the
actual output names exactly match each completion's declared names.

```json
{"outputs":[{"index":0,"name":"report.csv","size_bytes":123,"sha256":"64 lowercase hex characters","download_url":"/api/apps/v1/runs/run-UUID/results/0/report.csv?sha256=actual-digest"}]}
```

The digest describes the actual published bytes when the core first read the
complete results. The core records that output list once, and later result
requests return the same record. The unchanged native completion protocol
does not attest an original output digest. Downloads require the same app and
user authentication. A download hashes only the requested output through a
confined open descriptor and compares it with the recorded and requested
digests before streaming. Changed or incomplete results return 409. Failed or
partially published runs expose no downloads.
Status and run listing do not hash artifacts.

## Errors

Unknown JSON fields are rejected. Error bodies contain a `detail` string.

| Status | Meaning |
| --- | --- |
| 401 | Missing, invalid, or expired authentication. |
| 403 | Current app grant, linked account, or storage entitlement is denied. |
| 404 | Missing or foreign installation, input, run, or result, or a deleted input in a submission. |
| 409 | Changed idempotency payload, stale input, unavailable workload or staging capacity, run input staging quota exceeded, or unavailable results. |
| 413 | Input size or retained-input quota exceeded. |
| 415 | Upload media type is not `application/octet-stream`. |
| 422 | Invalid request shape, input names, path, or pagination. |
| 503 | Compute storage or database is unavailable. |

Worker IDs, provider paths, leases, execution logs, and native DTOs are absent
from these responses. Current registration grants and the installed release's
requested grants constrain workload operations.

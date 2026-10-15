# App API v1 HTTP reference

The [OpenAPI document](api-reference.json) describes the exact v1 paths,
request and response shapes, status codes, and pagination bounds. Core
generates it from `create_app_router()` with
`python -m ops.export_app_api`. Run `python -m ops.export_app_api --check`
to detect a stale export. This page shows how an independent app calls the
API. The examples use fictional IDs and values.

`PLATFORM_API_URL` includes `/api/apps/v1`. Every call, including a result
download, sends both `Authorization: Bearer <app credential>` and the current
`X-Platform-User-Context` assertion. The latter belongs to one incoming
user request. The gateway supplies it; the app forwards it without parsing
or retaining it. The two OpenAPI security entries apply together. Responses
carry `Cache-Control: no-store`.

## Find the installed workload

```http
GET /api/apps/v1/workloads HTTP/1.1
Authorization: Bearer APP_CREDENTIAL
X-Platform-User-Context: CURRENT_USER_ASSERTION
```

```json
{
  "active_release_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "items": [
    {
      "id": "11111111-1111-4111-8111-111111111111",
      "release_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "workload_id": "report",
      "inputs": ["data"]
    }
  ]
}
```

The listing retains installations from older releases. The active digest
comes from the latest accepted installation in the current environment, not
from the newest published candidate. It is `null` before an accepted
installation exists. Match both `workload_id` and the chosen release digest;
if that pair has no unique installation, require the caller to select one.
Save the installation ID with any submission intent so a replay uses the
same code after an upgrade.

## Create an input and submit a run

Upload raw bytes as `application/octet-stream`, with no JSON wrapper or
multipart form. The body must be nonempty and at most 20 MiB.

```http
POST /api/apps/v1/workloads/11111111-1111-4111-8111-111111111111/uploads HTTP/1.1
Authorization: Bearer APP_CREDENTIAL
X-Platform-User-Context: CURRENT_USER_ASSERTION
Content-Type: application/octet-stream

<raw file bytes>
```

The 201 response contains the input's `id`, `size_bytes`, and `sha256`.
For a user-authorized logical file, list
`GET /files?path=Home&limit=100&offset=0`
and send `{"path":"Home/Inputs/data.csv"}` to
`POST /workloads/{installation_id}/file-handles`. That call also returns 201
and the same input shape. File listings return an `items` array. Each entry
has `name`, logical `path`, `kind` (`file` or `directory`), and `size_bytes`;
directory sizes are `null`. File access needs the `files` grant and storage
entitlement. A deleted input cannot be used for a new run.

```http
POST /api/apps/v1/runs HTTP/1.1
Authorization: Bearer APP_CREDENTIAL
X-Platform-User-Context: CURRENT_USER_ASSERTION
Idempotency-Key: action-123
Content-Type: application/json

{"installation_id":"11111111-1111-4111-8111-111111111111","inputs":{"data":"22222222-2222-4222-8222-222222222222"}}
```

The first successful submission returns 201 and a run with `id`,
`installation_id`, `status`, and zero-based `tasks`. Repeating the same
app, user, key, and body returns the same run with 200. Changing the body
under that key returns 409. After an uncertain response, replay the exact
saved key and body. The key accepts 1 to 128 ASCII letters, digits, `.`,
`_`, `:`, or `-`.

Use `GET /runs/{run_id}` for a run, `GET /runs?limit=100&offset=0` for a
page of runs, and `POST /runs/{run_id}/cancel` to request cancellation.
List limits are 1 to 100 and offsets are 0 to 100000. Status values are
`QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, and `CANCELLED`. Completion of
the run does not itself establish that its results are available.

## Verify a result download

`GET /runs/{run_id}/results` returns 200 only after the complete result
manifest has been checked. An output has `index`, `name`, `size_bytes`,
`sha256`, and a relative `download_url` such as:

```text
/api/apps/v1/runs/33333333-3333-4333-8333-333333333333/results/0/report.csv?sha256=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
```

Send both authentication headers to that exact URL. The 200 body is raw
`application/octet-stream`. Check its byte count and SHA-256 against the
manifest before presenting it as a verified file. A changed or incomplete
result returns 409. The server checks the requested file again before it
streams it.

## Errors

Errors are JSON objects with one string field, `detail`. The text is
human-readable, not a stable machine code. Invalid shapes, paths, keys,
and pagination return 422. Missing or invalid authentication returns 401;
revoked authority or denied grants return 403. A missing or foreign resource
returns 404. Conflicting replay, stale inputs, unavailable workloads,
capacity limits, and incomplete results return 409. Uploads return 413
for size or quota limits and 415 for the wrong media type. Storage or
database unavailability returns 503. See the
[compute API](compute-api.md) for quota and lifecycle details.

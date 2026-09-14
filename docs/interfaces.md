# Web Interfaces

The system has four responsive routes grouped into two purposeful browser
areas. They share the same control-plane API and authenticated session and stay
deliberately small: plain HTML, CSS, and JavaScript, with no front-end build
chain.

## Current interface boundary

### Owner dashboard

The owner area is restricted to an administrator and has two views:

- **Overview** at `/dashboard` shows Pi health, Jobs and Network Storage service
  state, and worker telemetry. Its worker cards are observational.
- **Operations** at `/dashboard/operations` contains worker scheduling controls,
  member creation/enable/disable, and guarded Pi reboot or shutdown.

This separation keeps routine monitoring away from destructive or
state-changing controls without creating a page for every service card.

![Homelab Dashboard with sanitized demonstration data](assets/dashboard.png)

The sanitized image records the established visual language; the current
release adds the Overview / Operations navigation described above.

It refreshes every 15 seconds. That is intentionally much slower than the
worker's 5-second lease heartbeat: browser freshness is a usability choice;
lease renewal is a correctness mechanism.

Pi power control is deliberately stricter than an ordinary reboot button. It
requires the authenticated owner session, refuses the request while any worker
can claim work or any job is running, and sends only a fixed action marker to a
root-owned system service. That service stops the API and Samba, flushes writes,
and unmounts removable storage first. An unmount failure aborts the power action
and brings the services back.

The Network Storage card contains two endpoint panels with different roles.
**Pi SSD Samba** is the current live `home-storage` backend and artifact store.
**Synology NAS** is monitored as the intended primary storage system, but an
online badge does not mean the application has migrated to it. The combined
card is `ONLINE` only when both endpoints are online and `DEGRADED` when only
one is. The Synology check is credential-free TCP liveness only; capacity and
share authorization will be added through a dedicated storage adapter rather
than guessed from the network.

### Job Desk

Job Desk also has two views:

- **Jobs** at `/jobs-ui` is the queue, history, detail, cancellation, and
  artifact surface. It polls jobs every 10 seconds and no longer fetches worker
  placement data.
- **Submit** at `/jobs-ui/new` is the focused creation workflow. It fetches
  worker choices and refreshes them every 30 seconds, but does not fetch the
  complete job history.

Together they support:

- named jobs and sortable job-table columns;
- one expandable parent row for a job group, with child task state and placement;
- deterministic best-fit placement or explicit targeting of one registered
  worker, with each worker's job ceiling shown in the selector;
- small CSV and Python uploads, or linked verified datasets;
- PBS-style array submission from a ZIP or HomeStorage project folder, with
  named HomeStorage file bindings;
- batch resource limits;
- status, structured failure reason, and result inspection;
- cancellation of queued or running work, with a visible pending acknowledgement; and
- artifact listing, small text preview, and per-file download.

Members authenticate with their own username and password. Every list and
mutation is owner-scoped on the server, including cancellation and artifacts;
the page is not relying on client-side filtering. Administrators may also enter
the existing owner token and can see all workload records. Members currently
use uploads or self-contained project ZIPs because HomeStorage selection stays
disabled until the provisioned NAS ACLs pass the cross-user denial test and the
storage cutover is accepted.

An authenticated member can change their own application password from the
Account action. The owner can reset a lost member password from Operations.
Both actions revoke every existing browser session for that account and require
a new login; neither changes the separate DSM/SMB credential.

![Job Desk with sanitized demonstration job history](assets/job-desk.png)

The sanitized image records the established history presentation; submission
now lives on its own route rather than beside that table.

All four routes use the same 1240 px maximum content shell, safe-area-aware
outer spacing, panel geometry, and Overview / Operations / Jobs / Submit
navigation order. Their header frame also keeps the brand, context line,
connection status, and right-aligned logout action in fixed positions while
the page identity changes. The Submit view uses the available shell width
instead of a narrow centered column: related fields form two columns on tablet
and desktop, then collapse to one column below 640 px without horizontal
scrolling.

Current uploads are intentionally small because they pass through the
coordinator. Current artifacts are streamed individually, with default ceilings
of 100 MiB per file and 512 MiB per job.

Both screenshots use synthetic identifiers and history. They demonstrate the
interface without publishing live deployment details.

### CLI

The CLI is the stable automation surface. It supports worker inspection and
scheduling control, job submission, listing and cancellation, and artifact
listing, download, and deletion. `python_batch` accepts either a local CSV to
upload or a verified URL plus its SHA-256 and byte size, matching Job Desk's two
dataset choices. `submit-batch` packages a project directory or accepts a ZIP,
uploads repeated `--input NAME=PATH` bindings or attaches verified HTTPS files
with matching `--input-url`, `--input-sha256`, and `--input-size-bytes`
bindings, then submits the numeric array declared by its PBS-like entrypoint. The
session-authenticated project and batch endpoints use the same backend contract.
Job Desk accepts a ZIP or a visually selected Home/Shared project folder. For a
storage project it previews the parsed name, runtime, resource request, array,
and inputs. `#HP --input NAME=Home/...` and `Shared/...` defaults resolve on the
server; the advanced binding control remains only for missing values and
per-run overrides. A disabled-by-default workspace capability adds folder
creation and bounded, non-overwriting uploads only under
`Home/Workspace/...`. Its separately permissioned NAS mount is active for the
provisioned pilot account; the global flag remains false while browser
acceptance and multi-user isolation are pending.

The submission form opens with *where your files go*, stated plainly rather than
behind a disclosure: an explanation nobody reads is not an explanation. **Code**
follows, meaning the same thing for every task type and sharing a shape, so
switching task swaps controls rather than restructuring the page.

Below that the form follows the job type instead of forcing a common skeleton. A
`python_batch` job chooses one input from a source and sets its own run time,
CPU and memory, so it has **Input** and **Limits** sections. A `batch` project
declares all of that in `submit.hp`: it has no Limits section, because showing
uneditable values would imply a choice that does not exist, and its declared
name, run time, CPU, memory, array range and runtime appear beside the project
as a property of the selected code. Its inputs are a single **Input files** card
holding both the resolved declarations and the control for supplying one that
has no default — one card for one concern, not a section plus a nested
disclosure.

Both interfaces and both job types now use one vocabulary for choosing files.
`Home/...` and `Shared/...` are the logical roots everywhere — the Job Desk
picker, `#HP` defaults, `--input-storage`, and `--dataset-storage`. The server
resolves them: a member session's `Home` is its own tree, while the API token
reaches every tree and so must name the account as `Home/USER_ID/...`. Physical
share-relative paths remain accepted from the token API only, as a transitional
form for the Pi share's `projects/` and `inputs/` directories.

Results are symmetric. Neither interface offers an output destination, because
a script choosing one could overwrite another run. The platform publishes each
submission into its own directory named after the job, which Job Desk surfaces
through the artifact list and an owner sees directly over SMB.

## Remaining refinements

The next UI pass is a refinement of these boundaries, not a new control plane.
It should preserve the existing routes and progressively improve presentation.

Priorities, in order:

1. Preserve the shared 1240 px shell, navigation order, black terminal-inspired
   visual system, and Overview / Operations / Jobs / Submit purposes.
2. Make mobile the constraining layout. Important state must fit an iPhone
   without horizontal table scrolling; dense tables may become cards or
   disclosure rows at narrow widths.
3. Make freshness explicit. Show when telemetry was sampled and distinguish
   ONLINE, STALE, disabled, idle, and busy without relying on colour alone.
4. Give Job Desk a clearer submission flow, useful empty/loading/error states,
   accessible sorting indicators, and a focused job-detail view.
5. Improve artifact retrieval with download-all, visible size/limit guidance,
   and progress/error feedback. Native browser downloads cannot choose an
   arbitrary destination on another device; a true server-initiated transfer is
   a separate backend feature.
6. Improve first-login and disabled-account feedback. Password change/reset is
   live; keep the existing server-enforced owner scope and never replace it
   with row hiding or other browser-only authorization.
7. Add application progress only after defining a bounded update frequency,
   monotonic progress semantics, and behavior across retries. Worker liveness is
   already represented by leases and must not be presented as task progress.
8. Complete group controls. Grouped rendering, derived aggregate state, and
   numeric `#HP --array` expansion are live; add group-wide cancellation.
9. Add separate project and named-input staging for the general batch form.
   Reusing an immutable upload reference across tasks must not duplicate bytes;
   large verified inputs continue to bypass the coordinator.

## Design constraints

- Support current Safari and Chromium layouts on phone, tablet, and desktop.
- Preserve keyboard navigation, visible focus, semantic buttons, and readable
  contrast.
- Do not expose tokens to browser JavaScript; keep the HttpOnly session flow.
- Do not make polling more frequent merely to make the page feel live.
- Do not imply that live telemetry drives scheduling. Placement uses fixed
  capacity envelopes; CPU percentage and temperature are informational.
- Never treat an unguessable job or artifact UUID as authorization. Every list,
  detail, cancellation, preview, download, and deletion route must enforce the
  authenticated owner's scope.
- Prefer small enhancements over a framework migration until interface
  complexity actually requires one.

# Storage

The core presents shared files and job results through logical areas while
enforcing ownership in the application and on disk.

| Page | Purpose |
|---|---|
| This page | Storage ownership, result publication, and backups |
| [Files in and out of a job](workflow.md) | Prepare inputs, submit jobs, and collect results |

## Separate coordination from file storage

The core owns the API, scheduler, accounts, leases, database, and bounded upload
staging. A file server can hold members' files, larger inputs, published
results, and verified backup copies.

Direct file access and browser access serve different needs. A file client can
transfer large inputs without sending them through the core. The Files view
adds authenticated browsing, downloads, and constrained workspace edits.
Neither route replaces the other's authorization checks.

Live SQLite databases stay on local disk. Network file shares may not provide
the locking guarantees SQLite requires. The core and each independent app own
separate databases, and only verified backup copies travel to shared storage.

## Logical areas

Members address three areas, resolved against their authenticated account.

| Area | Contents | Read access |
|---|---|---|
| `Home` | Private input and workspace files | The owning member |
| `Shared` | Files deliberately shared with members | Members |
| `Artifacts` | Published job results | The owner of the corresponding job |

`Home` and `Shared` resolve to configured storage locations. The session
determines the private root. `Artifacts` resolves through immutable job
ownership rather than a path chosen by the browser.

A logical `Home/notes.csv` can therefore resolve to a different file for each
member. Resource identifiers and filenames do not grant access to another
member's tree. The [file workflow](workflow.md) uses these same logical names
in Job Desk, the command-line client, and job definitions.

## Two authorization boundaries

The application checks the authenticated account on every browse, read,
download, and mutation. It rejects paths outside the permitted area, traversal,
and links that could escape that boundary.

Filesystem permissions separately protect direct file-server access. Platform
passwords and file-server credentials are independent. A browser check cannot
compensate for a file-server account that can read another member's directory.
See [Accounts and access control](../access-control.md).

Browser workspace writes use a separate storage identity with access limited
to the provisioned workspace trees. They refuse overwrites and promote staged
uploads atomically. That identity does not inherit permission to publish job
results.

## Published results have one writer

Workers upload output under the current job lease. The coordinator chooses an
owner-scoped result directory and records the published files. A job name
becomes a safe directory segment with an identifier suffix, so duplicate names
stay separate and cannot escape the owner's root.

File-share clients see results read-only. Deletion goes through the core so it
can enforce ownership, reject unsafe deletion during active work, and preserve
the durable job record. Removing output does not erase job history.

Results do not expire by age. A configured whole-store ceiling can evict
least-recently-touched submissions to protect storage capacity. An array's
children remain together as one eviction unit.

Downloads carry size and digest information. The command-line pull workflow
can resume partial transfers and verifies bytes before exposing a completed
file. See the [job contract](../compute/job-contract.md) and
[configuration reference](../configuration.md).

## Verified backups

Database backup uses SQLite's online backup API to obtain a consistent copy.
Copying a database file during writes does not provide that guarantee. The
backup tool verifies the copy and publishes it with a SHA-256 sidecar before
removing older retained sets.

Each application owns its state and backup lifecycle. File-server snapshots
and off-device copies provide separate durability layers. Integrity checks do
not replace a recovery procedure that includes credentials, permissions, and
the other state required to start the service.

A file-server recycle bin can retain bytes after application deletion. Storage
policy must account for that behavior when deletion or capacity guarantees
depend on removing data from a managed result store.

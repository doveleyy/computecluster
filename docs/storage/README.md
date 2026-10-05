# Storage

Where household files and job results live, and how one physical tree is
presented safely to several people.

| Page | What it covers |
|---|---|
| This page | The storage model: one file server, logical areas, ownership, backups |
| [Files in and out of a job](workflow.md) | The practical route: prepare files, submit, collect results |

## One file server

A NAS is the only file server. It holds personal directories, shared data, job
inputs, and published results, and it is reachable two ways: over SMB with
Finder, Explorer, or a phone file app for large transfers, and through the web
Files view for browsing, downloads, and bounded edits.

Keeping this to exactly one service is a deliberate simplification. A second
half-configured share is how households end up with two copies of everything
and no idea which is current. Any disk attached directly to the always-on host
is local-only scratch or backup space, never advertised as a file service.

The host remains the control plane — API, scheduler, SQLite, authentication,
leases, and small upload staging — not a file server.

The file server holds these areas:

| Area | Holds | Written by |
|---|---|---|
| Members' trees | `Home` and `Shared` files | Members over SMB; the application only within a workspace subtree |
| Artifacts | Published job results | The coordinator only |
| Backups | Verified database copies | The host's backup jobs, into an owner-only area |
| Media | The household media library | People over SMB; the media server reads it only |
| Personal | The owner's own library, including an unsorted downloads inbox and its organized tree | The owner over SMB; the file sorter, by renaming within the share |

The media share is deliberately separate: its read-only account can reach
nothing else, and platform accounts cannot reach it. See [Media](../media.md).

The personal share is likewise separate. The file sorter reaches it through
its own file-server account, which can read and write that share and nothing
else; members' platform storage never includes it. Sorting is a rename within
one share, so no file passes through the host. See
[Application services](../services.md).

## Databases stay on local disk

Every live SQLite database — control-plane truth and each application's own —
is on the host's local disk, never on the file server. SQLite depends on file
locking that network file shares do not implement reliably, and a database
corrupted by a lock that silently did nothing is far worse than one that is
merely unavailable. The file server receives only verified backups.

## Logical areas, not paths

A member never addresses storage by its real location. Three logical areas are
presented instead, each resolved against the signed-in account:

| Area | Contents | Who can read it |
|---|---|---|
| `Home` | That person's private files | Only them |
| `Shared` | Deliberately shared household data | Every member |
| `Artifacts` | Published output of jobs they own | Only them |

`Home` and `Shared` are path rewrites: one logical path maps to one stored
path, chosen from the session rather than from anything the browser sent.
`Artifacts` is assembled from the job records a person owns, because published
output is authorized by job ownership rather than by where it sits on disk.

The benefit is that a typed path cannot reach another account. `Home/notes.csv`
means a different physical file for each person, and there is no path a member
can type that resolves outside their own tree. Listings and download URLs never
contain the underlying account identifier, so it cannot be copied, guessed, or
shared by accident.

## Two boundaries, enforced separately

Application authorization and filesystem permissions are independent, and both
are real:

- **In the application**, every browse, download, and delete re-checks the
  authenticated owner against the immutable record. Filtering a path in the
  browser is never treated as protection.
- **On disk**, file-server permissions enforce the same boundary for anyone
  connecting over SMB, where the application is not involved at all.

They may use matching account names for usability, but the credentials are
provisioned and stored separately. An application password is not a file-server
password. See [Accounts and access control](../access-control.md).

Write access is deliberately narrow. Browser-originated writes are confined to
a workspace subtree of a person's private area and use a separate service
identity from the one that publishes job results, so a defect in one path
cannot reach the other.

## Published results

Job output is owned by the coordinator, not by the script that produced it.
Each submission publishes into its own directory, named after the job so the
store is legible to someone browsing it rather than a wall of UUIDs:

```text
artifacts/<owner>/
├── svm-model-7dcf9099/          metrics.json, model.joblib
├── svm-model-1a4be012/          a second run of the same script
└── cohort-analysis-3b2e91c4/    an array submission
    ├── 1/result.txt
    └── 2/result.txt
```

Job names are neither unique nor path-safe, so the server reduces the name to
one safe segment and appends a short UUID suffix. Two runs sharing a name stay
separate, and a crafted name cannot escape its owner's root. The UUID remains
the identity every route resolves; the name is a browsing affordance, never a
key and never an authorization boundary.

Results are exposed **read-only** to the file share. The coordinator owns that
directory, and letting a share client delete from it would create a second
writer with no way to reconcile the two. Deletion happens through the
application, which refuses it while one of that member's jobs is running, and
removing files never removes the durable job record.

Nothing expires by age. A total-size ceiling exists as a backstop against a
runaway, evicting least-recently-touched runs and logging loudly; otherwise
removal is an explicit decision.

## Backups

Control-plane truth is backed up through SQLite's online backup API, never by
copying the live database file — a copy taken mid-write is a corrupt copy. The
resulting file is verified locally, closed, then published to a private
owner-scoped tree on the NAS with a recorded digest, keeping a bounded number
of sets.

That is the recovery copy for job truth. NAS snapshots and an off-device copy
are independent durability layers, and each hosted application backs up its own
database on the same pattern.

## Recycle bins

A file server's recycle bin keeps deleted files inside the same share, in a
hidden folder at its root. That is useful for people's own files, but it
quietly changes what "delete" means for anything the platform manages: while
the bin is on, a result deleted through the application is only moved, and
still occupies space. Shares the platform writes to should have the recycle
bin disabled or restricted to administrators, so the application's service
accounts neither fill nor see it.

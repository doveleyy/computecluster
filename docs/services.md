# Application services

The platform hosts small, long-running household applications beside the
compute system. Each is an independent service, not a new router inside the job
API.

## Why not just add it to the control plane

Adding a household app as another router inside the job API would share its
process, its database, its deploy cycle, and its failure modes. A dependency
upgrade for one becomes a risk for the other, and a crash in a habit tracker
takes the job queue with it.

Independence costs one container and one proxy route. That is cheap enough that
it is the default.

## The service contract

Every service has:

- a source directory under `services/`;
- its own container image and Compose definition;
- its own process, loopback-only port, health check, and durable state;
- a systemd lifecycle on the always-on host;
- one private HTTPS route through the reverse proxy;
- explicit CPU, memory, PID, filesystem, and privilege limits; and
- an online backup and tested restore procedure for its database.

While an administrator has the Overview open, the operator dashboard checks the
private `/ready` and `/version` endpoints of the habit tracker, wishlist, and
transport dashboard over loopback every 30 seconds. It leaves the file sorter
out on purpose. The sorter's readiness check reads its library on the NAS, and a
probe every 30 seconds would keep the NAS disks from sleeping. A ready response
means the application and its database can answer a lightweight check. An
unready response is shown as degraded; no response is shown as offline. These
checks are read-only and use fixed local endpoints, so the control plane needs
no Docker socket or restart privileges. They do not write database rows or
contact external providers. They report application readiness, not container CPU
usage or logs.

The request path is the platform's standard one — private HTTPS to the reverse
proxy, then plain HTTP to a loopback port with authenticated identity headers.
Because the container port is published only on `127.0.0.1`, no other machine
can bypass the proxy and forge those headers, and the service refuses user data
routes when proxy identity is absent. See [Network](network.md).

Ownership uses the platform's stable user ID rather than the network login. A
user links the two once while signed in, and the service resolves the network
subject through a narrow internal call protected by its own least-privilege
token. A service never opens the control-plane database and never receives the
elevated job API token.

## When to create another service

Create one when a capability has its own lifecycle, data, or failure boundary.
Do **not** split one small application into several services merely because it
has several screens — that multiplies containers, backups, and routes while
adding no isolation anyone benefits from.

A future calendar synchronizer owning OAuth credentials and a schedule would be
a separate service. Another page of an existing app would not.

The host is a single machine, not a cluster. Independent containers stop
dependency and process failures from being shared, but power, disk, network,
and the container runtime remain a common failure domain.

## Current examples

**Habit Tracker** is one application with four pages — an Overview, Water for
logging drinks, Budget for daily spending and a sinking fund, and Study for
focus sessions. They are pages of one cohesive app, not separate services, so
they share a single container, identity boundary, SQLite database, backup
lifecycle, and tab navigation. It reads and writes nothing in the job-control
database.

It is served at `/habits`. Source and a development recipe are in
[`services/habit_tracker/`](../services/habit_tracker/README.md).

**Wishlist** tracks what a thing costs over time. It is a separate service
rather than another Habit Tracker page, because reading prices from other
people's shops is a different failure boundary: a shop can hang, rate-limit, or
change its response without notice, and none of that should reach an
application for logging daily habits. It is served at `/wishlist` with its own
container and database, and shares only the identity mechanism.

**Transport** keeps a deliberately small personal view of public-transport
data. Its train card downloads a GTFS timetable only on explicit refresh and
answers today's last-train lookup locally. Its bus card stores a short list of
stop/service pairs and requests live arrival estimates only when that card is
refreshed. Grouping saved services by stop avoids duplicate upstream calls.
Transport owns its cache, saved selections, credential, and upstream failure
boundary, so it is independent of both the control plane and other apps.

**File Sorter** turns an unsorted folder of downloads into an organized
library, one decision at a time. It previews the next entry, the owner chooses
a destination folder, and the entry is renamed into place on the file server;
it can also be skipped or moved to a discard folder, and the last decision can
be undone. It is its own service because it is the only application that
writes household files, so it alone holds a file-server account limited to
one owner's share.

Its web boundary is stricter than the other applications'. A tailnet identity
proves only that a request came through the private route. The sorter also
requires the platform administrator's dashboard session. It asks the control
plane to validate that session and then trusts the answer for 60 seconds, so a
page that fetches many previews does not call the control plane for each one.
Signing out removes the cookie from the browser at once. A server-side change,
such as disabling the account, reaches the sorter within 60 seconds. A refused
or failed validation is never remembered. The sorter never holds the
session-signing secret. Members are refused even when their tailnet identity is
linked. The operator dashboard links to it, but the link is navigation, not
access control.

The sorter works on *projects*: each pairs one dump folder with one tree
folder, both inside a single library on one file-server mount. Several dumps
may feed the same tree. They then share its folders, reorganizations and label
log, so a future classifier learns one consistent taxonomy, while separate
trees stay independent. A dump is queued either by its top-level entries,
where a subfolder moves whole, or by every file inside it, where emptied
subfolders are tidied away. Folders cannot overlap between projects: one
project's dump can never appear as a destination in another's tree.

Because everything lives on one mount, a move is a server-side rename:
no bytes cross the network and a move cannot be left half done. The service
refuses symlinks, path escapes, existing destination names, and entries that
changed since they were previewed. Previews never execute document code. A
saved web page renders in a sandbox that allows no scripts and no network
access, enforced both by the frame and by a response policy that still
applies if the file is opened directly. SVG renders only as an image. Office
and other formats are reduced to bounded text, tables, or a preview image the
file already carries. Large files are not fetched until asked, and long
previews load more only on request, so the queue stays quick over the private
network.

Each decision is also a label. A file placed in `school/economics/EC2101`
records three routing choices — root to `school`, `school` to `economics`,
`economics` to `EC2101` — in an append-only log that exports as JSON Lines.
That log is training data for a hierarchical classifier that may later
propose destinations; the service itself contains no model.

A wrongly placed file can be selected from the tree and reclassified. Its
correction is a new decision linked to the earlier one, retaining a stable
document identity and the original dump context. The current label manifest
includes the file's current library-relative path and recorded content hash,
so an extraction step can verify the file before pairing its text with the
label. Missing files are flagged; an outside filesystem move never silently
changes a logged label. A separate log export retains the full decision and
folder-move history, including corrections and undo records. Undoing a
correction restores the file's previous tree location and prior label.

Sorted review begins with duplicate groups inside the tree, then keeps a
searchable file list beside its preview and destination choices. Browsing
does not create a label. Confirming the current placement or correcting it
appends a decision and advances to the next file, so reviewing a mostly
correct library takes one action per file. Confirmations retain document
identity and original context; Undo retracts a confirmation without moving
the file. Manual sorting and corrections already count as reviewed; the
review queue is for discovered content and future machine classifications.
An all-files view supports optional review of a directory, with one toggle
to include its subdirectories. Training still requires content verification
against the manifest.

Duplicate discovery uses full SHA-256 hashes from a persistent local index
of a dump and its shared tree, including copies that predate the log.
Background reconciliation checks metadata and reads only new or changed
content; duplicate resolution freshly verifies content and group membership.
Sorted review uses tree-only scope so pending dump copies stay outside that
review queue.
The owner chooses one keeper, its location and filename; other copies move
to the recoverable discard area. The log links each redundant document to
the keeper and exports it as ineligible for routing training, so a duplicate
discard does not become a conflicting content label. A durable operation
journal precedes the renames; all decisions commit together, and Undo
restores the entire group. Interrupted choices block further writes until
the journal restores their prior state with content verification.

Document identity, location and content are separate facts. Recorded sorter
moves preserve identity. Unchanged content at the same path also preserves
identity after metadata changes; changed content or an uncertain external
move becomes a new document requiring review. The earlier decisions remain
in history. The current manifest cannot silently attach an old document's
label to a replacement at the same path. Existing manual decisions without
historical hashes establish a current baseline, rather than asserting that
the original document bytes have been verified.

The folder tree is expected to change as the library grows: a new level can
be inserted above existing folders, and folders can be moved or renamed.
Rewriting old decisions would destroy the history of what was chosen, so a
reorganization is appended to its own log instead. Reading a decision replays
every later move over its path. Labels therefore always match the current
tree, the original choice remains available, and undoing a sort still finds
the file in its new place.

These services show the rule in practice — several screens of one idea stay
together; a capability that fails for unrelated reasons gets its own service.

## What is not a platform service

The media server follows several of the same habits — its own container,
loopback binding, private HTTPS, and a supervised lifecycle — but it is not a
platform service. It runs on the workstation rather than the host, is
published through that machine's own proxy, has its own accounts instead of
platform identity, and stores nothing on the host. See [Media](media.md).

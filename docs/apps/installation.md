# Approval and managed installation

The administrator's Apps page selects an immutable published release. Preview
checks and a disposable migration rehearsal authorize that exact release.
Approval records the publication archive, preview, registration, host policy,
credential generation, administrator session version and installed generation.
A later publication cannot replace the selected image. Rejection records the
selected candidate. Rollback selects the previous verified installation and
uses current credentials and current application data.

## Authority and host effects

The core writes a durable operation before publishing its operation UUID to a
request directory. Requests contain no executable commands or host paths. The
root helper resolves current authority through a read-only SQLite connection
in a child running as the core account with no new privileges. The helper
rechecks the archive and compares the registration with an independently
maintained root-owned host policy before running Docker. Every root-side
release verification takes its expected source, image repository and
application ID from that policy entry, so a self-consistent archive that names
an image outside the policy's repository is refused before any pull.

Root cannot verify a release's provenance itself; only the core checks GitHub.
A compromised core could therefore forge any manifest field, so root acts on
the manifest only through a narrowed view: the image reference, which the
verification binds to the policy's repository, and the runtime port and
readiness and version paths, which steer probes at the app's own container.
Every other input has a root-owned authority:

| Root input | Authority |
|---|---|
| Source, image repository, application ID, route, host port | policy entry |
| Application UID/GID, state and backup roots, credential file, secret files, settings, resource limits | policy entry |
| Compatibility command | policy entry `compatibility_argv` |
| Maintenance commands, intervals and timeouts | policy entry `maintenance` |
| SQLite state file for backups and rehearsal | policy entry `state` |
| Integration binding directories | policy entry `mounts` |
| Image reference, runtime port, ready and version paths | verified archive, repo-bound |
| Current installation (generation, image, operation) | root journal `<app>.current.json` |
| Operation, selection, administrator approval, credential verifier | core approval, checked against policy and journal |

The host policy fixes the source, image repository, route, host port, separate
application UID/GID, storage roots, credentials, settings, secret bindings,
resource limits, compatibility command, maintenance commands and SQLite state
file. Each policy app entry carries `maintenance` and `state` in the same shape
the release manifest uses. A release whose manifest declares different
maintenance commands or a different state file is refused before any host
effect with the receipt error `policy_refused`. When an app release changes
either, the operator edits that app's policy entry to match before approving
it. Editing the entry changes the policy fingerprint approvals and the
installed generation are bound to, so the installed app keeps running but needs
the new approved deployment before its next managed restart. A previous
installation that root restores after a failed switch runs under the current
policy entry's lifecycle, including its maintenance commands and intervals,
not the ones its own release declared. Recovery verifies the blocked
candidate's release against the current policy entry before any host effect,
so a blocked candidate whose maintenance or state no longer matches the policy
needs the entry edited to match before `recover` can run, even when recovery
will only restore the previous installation. State, backups, credentials, core
state and generated lifecycle directories cannot overlap. The immediate core
state and credential directories belong to the core account; their ancestors
belong to root. Application state belongs to its separate app account.

Each policy `mounts` entry has `name`, `source` and `read_only`, which defaults
to false. The mount names must equal the registration's `integration_bindings`
exactly, with no duplicates, so a registration can request a binding but only
root decides what it means. Sources join the same no-overlap check as state,
backups, credentials and core state. An entry without mounts fingerprints
exactly as it did before mounts existed, so adding the field leaves earlier
approvals valid; adding, removing or moving a mount, or flipping its
`read_only`, changes the fingerprint and needs a new approved deployment. The
generated app unit requires each source's file system with
`RequiresMountsFor`. Before any live container start, and before a deployment
closes admission, root checks that each source exists, is a directory, belongs
to the app account and resolves without a symlink. An unmounted network share
leaves a root-owned mount point behind, so the check refuses to start the app
against that empty directory. The installer holds its lock while it checks and
mounts a source, so the operator should mount the share with soft or timeout
semantics; a hung hard mount would stall every other installer operation. The
periodic installer pass never inspects mount sources, so routine runs do not
wake a sleeping share. Rehearsal receives neither the mount nor its variable.

Images in a private registry need a login that only root holds. The optional
policy field `registry_credentials` names a root-owned Docker configuration
directory with mode `0700` whose `config.json` has mode `0600`; the operator
creates it as root, for example with
`docker --config /etc/home-platform/registry login ghcr.io`. The helper passes
that directory to `docker --config` for pulls only, after checking that both the
directory and the file belong to root and are closed to the group and others; a
readable login refuses the pull before Docker runs. The login never reaches the
core account, an unprivileged child, a container, or a receipt, and adding or
rotating it does not change any app's policy fingerprint. Without the field,
pulls are anonymous.

The helper's interpreter, installed packages and import locations belong to
root and are not writable by the web account. It runs with Python isolated
mode. The web account has neither Docker access nor unrestricted sudo. Each
container receives only its own credential snapshot through a read-only mount.
The snapshot belongs to root and is readable by the app group inside the
container; its host directory remains private to root. Rotation requires a
new approved credential path and corresponding host policy. Secret snapshots
are named `<app>--<secret>--<digest>`, so pruning one app's superseded
snapshots cannot remove another app's.

The core and helper each have one writer for their own records. Immutable
spool files use exclusive temporary files, fsync and atomic rename while a
directory lock serializes publication. A committed record has one hard link,
including when a publisher exits before directory fsync.
Reads require bounded regular files with the expected ownership and modes.
Only a validated success receipt advances installed history. A missing request
is republished from the core ledger. A helper restart republishes completed
receipts or adopts an exact owned container after checking its image and
operation label. Contradictory ownership blocks route admission for operator
recovery.

## Migration compatibility

The root policy's `compatibility_argv` is a read-only command inside either
application image. Its stdout must be nonempty, deterministic and at most
64 KiB. It describes all persisted user records whose preservation matters.
Exclude implementation details such as schema versions, timestamps generated
by the probe, and binary versions. An empty record collection can produce
`[]`. The command must fail when it cannot read or validate the database.

For an upgrade the helper compares three outputs. The previous image reads
the online backup before migration. The candidate starts against a disposable
copy and passes its readiness endpoint. Both the candidate and the previous
image then read that migrated copy. All three semantic outputs must match.
This makes a readable but empty database insufficient evidence. When the
outputs differ, the receipt error is `records_changed` and the Apps card
reports that this release would change stored records. A probe that
omits records cannot establish their preservation, so the operator must review
the command's coverage when admitting an app. A first installation has no
previous image, so its rehearsal records the previous-image read as not
applicable rather than passed; the core accepts a rehearsal only when every
applicable check passed and no inapplicable check claims to have run.

Because two different images print the outputs being compared, the probe must
describe records, not schema. Project each table onto a declared list of the
columns this version reads, in a stable key order, and leave out schema text,
column order and internal tables. Then an additive migration, such as a new
nullable column or a new table, leaves the output unchanged, while a dropped
or renamed column, a changed value, or a missing row changes it. A probe that
hashes the whole schema refuses every migration, including safe ones.

The output format is part of the app's contract with its own past releases.
Every release compares its output with the installed release's output, so a
release that changes how the probe prints records is refused even when no
data changed. Choose the format before the first managed installation and
keep it. A later release may declare a new nullable column or a new table,
provided the probe omits null values and empty tables, so that older and newer
probes still agree on the same records. Keep a column added with a non-null
default out of the declared list. Otherwise the new release reports values
that the previous release does not.

Rehearsal has no network or production secrets. Normal startup receives a
synthetic credential and unreachable platform API address. Read-only probes
receive a read-only state mount. The helper enters only the disposable
container's network namespace to check its local readiness endpoint with its
own trusted interpreter; it does not require a particular app language.

Every readiness check the helper makes follows the `readiness` mode of the
probe in the policy entry, not the release. This applies in rehearsal, after a
switch, at boot and during recovery. Managed installations require
`v1-status`. Readiness passes when the endpoint returns HTTP 200 with a body of
at most 64 KiB, and the helper does not parse the body. After a switch, at boot
and during recovery, the response must also be unencoded and the version
endpoint must return a JSON object whose `service` equals the application ID.

The app account's child performs consistent SQLite online backup and integrity
checks. A second disposable restore must reproduce the SQL content digest.
The rehearsal directory belongs to root and the app group with mode 0710, so
that check can reach the state copy under the app account while the database
copies beside it stay private to root. The helper repeats compatibility using
a fresh online backup immediately before switching code. Ordinary installation
requires enforced memory, swap accounting, CPU and PID limits, checked before
any host effect. Destructive migrations require a separate operator procedure.

## Failure and lifecycle

Queued deployments keep the installed route open during image pull, backup
and disposable rehearsal. The helper publishes a switch intent through the
receipt spool. Core commits the operation's `switching` state and closes route
admission. The helper reads that committed state through its unprivileged
child before host effects. A timeout leaves the request pending without
stopping the installed app. The helper removes its own journal marker when it
commits the operation's receipt, and removes the spool switch intent on a later
run once the core has consumed that receipt and withdrawn its request, so a
core that lost its last commit still finds the intent and a finished operation
leaves only its receipt behind.

The helper rereads the root policy and verifies current authority, stops owned
maintenance timers, switches code and verifies readiness, identity denial and
runtime limits. Containers run with a swap limit equal to their memory limit,
and the probe confirms both, so the declared memory is the total. Database
copies are written private to root and the policy's backup directory must be
private to root. The scratch root belongs to root with mode 0711, because the
app account's backup child traverses it to reach its private copy. Each root
run first removes stale `rehearsal-`, `input-` and `backup-` scratch
directories left by an interrupted run. Root's journal keeps one
current-installation record per app, `<app>.current.json`, written only from a
success receipt. The generation root installs, the installation it backs up
and restores, and the previous image a rehearsal compares against all come
from that record. A core-supplied installed snapshot or selection generation
that differs from it is refused as changed authority, so neither a rolled-back
core database nor a forged generation can move root's backups or restore
target. Successful routed identity still requires acceptance through
the core gateway; direct missing or forged identity denial alone does not
establish that check. It generates an owned systemd lifecycle and fixed
maintenance commands. Daily backups retain fourteen routine copies. The
pre-switch backup is taken after core closes admission and before old code
stops, so it contains every acknowledged write. It is named by the target
generation and operation; a retried attempt at the same generation replaces the
earlier copy, and a successful switch prunes copies older than the previous
installation. Rehearsal input copies live in scratch and are removed.

The authorization commit point is the final successful authority check after
runtime and lifecycle verification. A root success receipt attests that check.
Root writes the receipt to its journal first, then the current-installation
record it implies, then the spool copy the core consumes. A run interrupted
between any two of these replays from the journal receipt on the next request,
so the record and the core's history always follow the receipt.
Core records that completed effect even if the administrator session or app
credential is revoked before receipt consumption. Recording history does not
reactivate a credential. Current credential checks continue to govern app API
and gateway access. A helper restart with unresolved switch effects and stale
authority reports `blocked`, preserving closed admission.

`blocked` has one exit. The administrator requests a `recover` operation for
the blocked candidate from the Apps page; the operation is recorded, admitted
and executed like any other, and the recorded policy, registration and current
credential govern it. The helper inspects the owned container: if the blocked
candidate is running with its exact image and operation label, the same
selection still holds and readiness passes, it is adopted and installed history
advances. Otherwise the previous installation is adopted or restarted against
current data, or, for a failed first install, the candidate container and
units are removed. Only a success receipt releases the block; unknown
container ownership leaves the block in place and the container untouched.

App, maintenance and backup units occupy separate names. Installation and
recovery reconcile the exact owned unit set, stopping the previous supervisor
and obsolete timers before a container is replaced, and enabling the selected
generation's units. The app unit is not started during the switch: a boot
while core still records the operation as switching or blocked exits with
status 75, which the unit lists in `SuccessExitStatus`, so systemd records a
clean stop with no restart. The helper run that follows core's acknowledgement
of the receipt (the request file disappears and the path unit fires, with the
five-minute timer as the fallback) starts the app unit once for a succeeded or
recovered operation; a blocked operation leaves it stopped until recovery.
Maintenance timers are calendar timers with `Persistent=true`. The policy
accepts `interval_hours` of 1, 2, 3, 4, 6, 8, 12 (an hour step through the
day), 24 (daily) or 168 (weekly) and refuses any other value. Each task fires
at a fixed wall-clock offset inside its period, derived from a hash of the
application ID and task name, so tasks of different apps usually fire at
different times. A task can still share a minute with another task or with the
midnight backup; the online backup is safe while the app writes. The schedule
does not move on
`daemon-reload`, and a reinstall within one boot re-enables a timer whose
systemd stamp still records its last run, so the next run is the next calendar
point and no extra run follows the install. After a reboot, or when a timer was
stopped across a calendar point, `Persistent=true` runs the task once at
startup to catch up, as the daily backup timer already does. Docker restart is
disabled for managed containers. The systemd app unit runs the helper's
installed-generation
reconciliation and supervises the resulting container. Every boot or restart
checks accepted core history, the root journal, actual image and operation
labels, current policy, credential and resource support. Resource
limits are checked before the installed image is verified locally and pulled
only when absent, and nothing stops until it is present, so a restart during a
registry outage keeps the app running. An exact
running container that passes readiness is adopted; otherwise it is recreated
with the approved runtime settings. Each deployment therefore starts its
container once. The policy comparison covers only the entries that govern the
app, so editing another app's entry or the spool and scratch roots does not
stop it from restarting. Legacy application restart policies are unaffected.

Failed compatible upgrades restart the previous image against the current
database. They never restore an older live database automatically. The previous
operation label is restored with its image, so later operations can reconcile
ownership. If current authority is revoked or recovery fails, the route stays
closed. Reject, failure and rollback retain application data.

Every Apps route requires a current administrator session. Mutations also
require an exact configured origin and session-bound CSRF token. Browser
responses expose release links, permission names, checks and bounded error
codes, rather than host paths, credential values or subprocess output.

## Process interfaces

`python -I -m ops.managed_installer --policy POLICY` processes pending operation
hints once under an exclusive host lock. Each run also writes
`hp-managed-installer.service`, `.path` and `.timer` into the units directory
when their content changed and enables the path and timer. A pending-reload
marker is written before any unit file is replaced and removed after
`systemctl daemon-reload` succeeds, so a run interrupted between the two still
reloads systemd on its next run. The path unit runs
the service when the request directory changes; the timer runs it two minutes
after boot and every five minutes, so a request left pending by a helper timeout
or a core acknowledgement that arrived late is retried without a manual step.
The operator runs the command once by hand after installing the policy to
create the trigger. `--backup APP_ID` uses the same lock for an installed
app's routine
backup. Generated app services invoke `--run APP_ID`, which reconciles the
installed generation under the lock and then supervises its exact container
ID after releasing the lock. Stopping or restarting the unit signals only that
helper (`KillMode=mixed`), which stops the container with `docker stop` and
exits 0, so the next boot creates a new container. `TimeoutStopSec=30` leaves
room for Docker's 10-second stop grace. Only an installation, recovery or
rollback writes app units, so a core upgrade leaves installed units unchanged
until one of those runs. The interpreter must have the core `app`, `ops`
and `contracts` packages installed outside the web checkout, along with their
dependencies.
The host also supplies Docker, systemd and `nsenter`.

The core reads the policy named by `HOME_PLATFORM_INSTALLER_POLICY` to discover
the spool and policy fingerprint. `HOME_PLATFORM_APPS_ORIGIN` fixes the browser
origin allowed for mutations. Existing preview configuration supplies the
separate preview controller. Operator registration updates preserve omitted
settings and invalidate earlier approval bindings. For an application with an
accepted installation an update becomes a pending settings change instead, as
the next section describes. Each app's card names the running and pending
release in plain words: the version the app reported during its preview, the
first line of the commit message, and the commit time and commit. Discovery
records the commit message and time from the verified workflow run; a release
verified before that record existed shows only its commit. Before a release has
a preview, its short commit stands in for the version. One status line states
the next step from the preview and rehearsal records and offers one primary
action. A release that adds a capability or secret binding lists those additions
next to the status. Full fingerprints, the image, all capabilities and bindings,
the per-check results and recent operations stay under the card's technical
details. Approval still sends the full release fingerprint. Each app's card is
built on its own: an app whose records or registration cannot be read shows an
error card with a bounded message while the other apps render. Reading the page
never reconciles receipts; the background loop does, and it isolates each
operation so one undecodable record leaves the others reconciling. An operation
the core cannot reconcile shows its last reconciliation error and time on the
card.
Actions for one app run one at a time in the web process, so a double-click
cannot start two staging previews.

## Settings changes to an installed app

The installed registration is part of the helper's boot authority, so an
operator update to an installed app never changes it in place. The update is
stored as the app's one pending settings change. It replaces any earlier
unapproved change and leaves the installed snapshot, the gateway and navigation
as they were. Proposing the installed registration again withdraws it. A change
may alter operator bindings, meaning resources, settings, secret files, the
backup directory and integration binding names, and presentation. It may not
alter the application ID, route, source, installation, access, capability grants
or state directory, and it must still admit the installed release. No change is
accepted while an operation is pending.

A change belongs to the installation it was proposed against. Every selection
made while it is pending carries it, so its registration fingerprint is bound
into the rehearsal and the approval exactly as a release is. With no newer
release the card offers the installed release again with the change listed in
plain words, and approval redeploys that same digest with the new registration.
Each listed item names the old and new value: resource limits, setting values,
secret file paths, the backup directory and integration names. Setting values
are not secrets, so they appear in full up to 40 characters and shortened past
that. The card's technical details list every proposed binding.
The compatibility rehearsal still runs, because each approval needs one for its
exact selection. When a release update is also pending, one approval applies
both and Reject discards both. Check compatibility, Approve and Reject each send
the fingerprint of the change the card showed, or none when it showed no change.
The core refuses the action when that differs from the pending change, so a
change replaced after the page loaded is never rehearsed, approved or rejected
unseen. A retried request ID must name the same change.

Root still requires the approved registration to equal its policy entry, so the
operator edits the entry to match before approving. Until then the card's
status line asks for that edit and its primary action is disabled, since root
would refuse the attempt with `policy_refused`. Equality includes the order of
settings, secret files and integration bindings, which the registration keeps
as stored, so an entry that lists the same bindings in another order also
differs; the card then says that only the order needs fixing. As with any
policy edit, the running app keeps running but cannot restart until the change
is deployed, and once the entry is edited the card says so. When a deploy
carrying the change is accepted, its snapshot becomes the installed
registration in the same transaction. Any accepted install retires a pending
change. A rollback restores the previous snapshot's registration together with
its release, so it needs the policy entry returned to that registration first.
Until it is, the card disables Rollback and says what to change.

When a switch fails, root restarts the generation it restores with the
registration that root's journal holds for that generation, not with the edited
policy entry. That registration supplies the resource limits, settings, secret
file paths, backup directory and integration names. Everything else comes from
the current policy entry and host: the mount source of each integration, the
contents of each secret file at its journaled path, and the maintenance
commands, SQLite state file and timers. A failed settings deploy therefore
brings back the old limits and settings, and a failed rollback brings back the
current ones. When the change added or removed a binding, the restored
generation can name an integration the current entry no longer mounts, or a
secret file that no longer exists. The restore then fails and the operation is
left `blocked`.

A recovery always binds the installed registration, and root refuses it while
the policy entry's registration differs. To recover a blocked settings deploy
or rollback, the operator reverts the entry's registration to the installed
one, then chooses Recover. Only the registration is reverted. The entry's
mounts still name exactly its registered integrations, so they follow when a
binding was added or removed; maintenance, state and schedules stay as they
are. Until the registration is reverted, the card disables Recover and says
what to change. Root restores the installed generation; it adopts the blocked
operation's container only when that operation did not change the
registration. A restored generation keeps the policy fingerprint it was
installed with, and root starts the app at boot only while the whole policy
entry still matches that fingerprint. So if the blocked release also changed
the entry's maintenance commands, state file or schedules, revert those after
Recover succeeds, or the next host restart refuses to start the app. The
pending change survives recovery, so it can then be deployed again, replaced
or withdrawn.

# File sorter

A private, single-administrator labeller for messy folders. It shows one
entry at a time from an unsorted *dump* folder; the owner picks a destination
in an organized *tree*, and the entry is renamed into place. Every choice
is also logged as hierarchical routing labels: training data for a
classifier that may later suggest destinations. The service contains no
model.

## How it behaves

- **Projects.** A project pairs one dump with one tree, both folders inside
  the library root. Any number of projects can exist; several may share a
  tree, and then share its folders, reorganizations and labels. Dumps and
  trees may not overlap across projects. Archiving a project frees its dump
  and keeps its labels.
- **One entry at a time.** In `top` mode the dump's top-level files and
  folders are queued alphabetically, and a dump folder moves whole as one
  entry. In `files` mode every file at any depth is queued by its path, and
  subfolders emptied by sorting are tidied away. The queue is cached briefly
  and updated by the sorter's own moves. Returning to the window with the
  queue empty looks again; the ⋯ menu's **Look for new files now** forces it.
  Hidden files and partial downloads are never queued. Skip sends an entry
  to the back; Discard moves it to `_discarded/` in the tree; Undo reverses
  the project's most recent sort or discard. Nothing is ever deleted.
- **Two tabs.** **Sort** empties the dump; **Review** (or `R`) works on
  files already in the tree. The Review tab's badge counts duplicate groups
  in the tree. Review is one searchable file list beside the preview and
  destination tree, with a banner of duplicate groups above it when any
  exist. Search names and paths across all folders, choose a directory or
  show only files needing review. A directory includes direct files by
  default; the **Subfolders** chip unfolds its entire subtree. The list
  refreshes itself on entry, after actions, when an index scan completes and
  when the window regains focus, and loads more as it is scrolled. Manual
  sorts and corrections already count as reviewed, but remain available in
  the all-files view. The list is read from the file index, not by walking
  the NAS; only a tree no scan has covered yet is walked. `J` goes back and
  `K` forward without deciding. Enter
  confirms the current placement or saves a correction and advances. Every
  confirmation and correction appends to the log; Undo restores the prior
  label and, for corrections, location. An Undo that returns a file to a
  tree location shows it in Review, since the Sort tab only shows the dump.
  Pre-existing files can be reviewed; discarded files and folders sorted
  whole are outside this file review.
- **Moves are renames.** The whole library is one filesystem, so
  each move is a single no-replace rename: no bytes are copied, a move is
  never half done, and an existing destination is never overwritten.
  Entries that changed since they were previewed are refused.
- **Disk and log never part.** Every rename is followed by its log write.
  If that write fails, the rename is reversed: classify, discard and folder
  moves move the entry back. If even the reversal fails, the error says
  where the entry now is.
- **Nothing the views would hide can be created.** New folder names cannot
  start with `.`, `#` or `@`, use a reserved name anywhere, or look like
  temporary files. New filenames must be visible queue names. Destinations
  must be real category folders, matched by exact letter case.
- **Undo means what it says.** A notification's UNDO reverses exactly the
  decision it reported, in the project that made it (`expect_decision_id`);
  the server refuses if something newer has happened since.
- **Safe, bounded previews.** Browser-native types (PDF, images, audio,
  video) are served raw with `nosniff`. HTML and SVG are served under a
  sandboxing Content-Security-Policy (no scripts, no network) and shown in a
  sandboxed frame or as an image. CSV and Excel become tables; Word,
  PowerPoint, OpenDocument and iWork show the preview image they already
  store; notebooks, EPUB, email, RTF and any plain-text file become text;
  archives become listings. Every reader stops at a budget, with a "load
  more" request for larger slices. Large PDFs and images wait for an explicit
  load. Only the standard library is used; nothing is converted with
  external software.
- **Labels.** A file placed in `school/economics/EC2101` yields three routing
  decisions: root → `school`, `school` → `economics`, `economics` →
  `EC2101`. A stop decision is added when the chosen folder has subfolders.
  `GET /api/projects/{project_id}/labels.jsonl` exports them.
- **Reorganizing.** Folders can be grouped under a new level, moved and
  renamed. Decisions are never rewritten: each folder move is appended to a
  log and replayed when decisions are read. Labels follow the current tree,
  and `decided_label` keeps the original choice. Each folder row has a ⋯
  menu (also right-click, the menu key or Shift+F10): New subfolder,
  Rename… (F2), Move to…, and Group with siblings…, which ticks sibling
  folders to put under a new level. Press **Esc** to clear the chosen folder,
  then **N** to create a main category at the top level. With a folder
  chosen, **N** creates a subfolder. Esc also closes a menu or leaves a text
  field; an open dialog closes first.
- **Path search.** The folder filter takes a shell-style path:
  `/school/eco` lists school's subfolders starting with "eco", Tab completes
  the name, and Enter chooses the exact path. Plain text matches anywhere.
  ↑ ↓ move a cursor through the matches; Enter takes the one under it.
- **One ⋯ menu for the rest.** The header's ⋯ holds the exports, Look for
  new files now, Check every file's contents, the shortcut list and the
  dashboard link.
- **The log is label truth.** A correction appends a decision linked to its
  predecessor; the original row stays intact. A durable `document_id` follows
  the file through corrections and folder changes. The label export includes
  the active `decision_id`, predecessor ID, current library-relative
  `file_path`, tree-relative path, original dump context, recorded SHA-256
  and current label. Missing logged files are flagged, never silently
  relabelled from the filesystem. Pre-existing files remain explicitly
  unreviewed until a decision is made about them.
- **Access.** The page requires the private proxy's identity header **and**
  the platform administrator's dashboard session cookie. The cookie is
  forwarded to the control plane for validation on every request, so the
  service holds no session secret. Members are refused.

## Duplicate review

The indicator looks up full SHA-256 hashes in a persistent local index of
the project's dump and shared tree. It finds pre-existing and unlogged
copies too. Missing files, discarded copies, symlinks and whole-folder
items do not count as kept copies. Different trees are separate libraries.
Indexing runs in the background, including large files. Known matches stay
available during indexing; incomplete results are explicitly marked.

Use **CHOOSE ONE TO KEEP** on a dump indicator to resolve its matching dump
and tree copies. The Review tab's banner lists groups found by indexed
content hashes with at least two kept copies inside the tree. Pending dump
files are outside that review. Each copy is a card; the tree copy is
preselected, and its folder and name are kept unless **Change folder or
name** says otherwise, where any copy's name is one click.
**KEEP THIS COPY** rechecks the group's
content and membership, keeps one active copy, and moves the rest into
`_discarded`. Sorting refuses another identical kept copy; sorted review
refuses confirmation or correction until tree duplicates are resolved.
Once the index fully covers a project, that check reads same-size
candidates from the index instead of walking the NAS under the rename lock,
and re-reads each candidate from disk before trusting it. A copy added in
Finder since the last scan is then caught by the next scan, in duplicate
review, rather than at sorting time. Empty files are never duplicates.
Undo restores the whole group, including every original name and location.

The log records every copy's stable document identity and original context.
Manual decisions add `review_type: manual`; review decisions use `accepted`
or `corrected`. Each records modification time. Manual choices need no
second review; edited and discovered documents do. Content verification is
still required before training.
Discarded duplicates carry `duplicate_of_document_id` pointing to the
keeper, plus a shared `duplicate_group_id`. The manifest marks these copies
`training_eligible: false`; ordinary discard labels remain usable. The full
log also exports the durable operation plan saved before any rename.
Decision writes commit together; ordinary errors roll back the file moves.
If interrupted, a red banner appears under the header and writes stop
until its **RECOVER** restores
the pre-operation state using that journal. Changed content or ambiguous
extra copies cause recovery to refuse without overwriting them.

## File index and reconciliation

The SQLite database also holds document identities, current file locations,
full hashes and append-only observation events. IDs are independent from
decision IDs; existing document IDs survive migration. The indexer checks
metadata five minutes after the previous scan finishes and hashes only new
or changed files. A shared tree is walked once per scan. Directory names and
metadata from that walk are reused for unchanged files; changed observations
and every human action still receive fresh path and content checks. Scan
progress is persisted at most once per second and finalized on completion,
while document observations and decisions retain their durable transactions.
The index resumes from persisted hashes after restart.
Reading file contents does not hold the sorter's rename lock.

Failures are contained, and "unreadable" is never taken for "deleted":

- A project whose dump or tree is missing or unreadable is reported and
  skipped; other projects are still indexed.
- A walk that could not read some folder leaves that dump or tree
  *incomplete*: nothing in it is marked missing, and the last completion
  time does not advance.
- A file that cannot be read, or has an invalid name, is skipped and
  reported, while the scan continues. A file that is still being written is
  retried about a minute later.
- A document is marked missing only after a complete walk of its area, and
  only if its path is absent on disk at that moment. Sorting during a scan
  therefore no longer restarts the scan: each file is rechecked under the
  rename lock before its observation is published.
- No error ends the background thread. Skipped items are summarised in the
  index status.

Names that are not valid UTF-8 on disk are left out of the queue and the
index. A folder named `__stop__` (the label marker) is outside the tree.

Sorter moves, folder reorganizations and Undo carry identities and index
locations in the same database transaction as the decision or move log.
At one path, unchanged full content retains identity after a metadata
change. Changed content receives a new document ID requiring review. An
external move is a new discovery even if its hash matches a missing file:
matching content cannot prove which physical copy moved. Old decisions
remain in history and cannot attach to a replacement at the same path.

Existing manual choices remain reviewed at bootstrap. Recorded hashes are
checked where available. Otherwise, unless the recorded size and time match,
the row's reason is `legacy_baseline`: the label stands, but the manifest
reports it as not content-verified and not training-eligible. Accepting the
file in review records today's bytes as the labelled content. New dump
files stay in the sorting queue; new tree files need classification review.
Machine-classified provenance is reserved for later integration; this
service still contains no model.

Review queues an incremental refresh whenever the window regains focus,
and ⋯ → **Look for new files now** queues one on demand. ⋯ → **Check every
file's contents** queues a full content audit without blocking review. Normal scans also
recheck up to sixteen indexed files whose last content check is over seven
days old. Metadata reuse cannot detect every timestamp-preserving edit;
full verification remains necessary before duplicate resolution or training.

Authenticated index endpoints are `GET /api/projects/{id}/index` and
`POST /api/projects/{id}/index/refresh` (`verify_all=true` for a full audit).
Duplicate GET endpoints accept `indexed=true` for immediate inventory
results; the original fresh scan remains available for compatibility.
Review GET accepts `recursive`; the updated UI sends false by default for
a selected directory, while omitted parameters retain subtree behavior.

## Development

```bash
pixi run sorter-dev
```

This uses `data/sorter-dev/` (ignored by Git) as the library root, seeds a
project from `dump/` to `sorted/`, and serves `http://127.0.0.1:8103/` with a
development identity. Put sample files in the dump folder, or add more
folders under `data/sorter-dev/` and create projects for them. Never point
a development run at a real library.

Configuration lives in the `SORTER_` environment variables; see
[configuration](../../docs/configuration.md). The design rationale is in
[application services](../../docs/services.md).

## Tests

```bash
pixi run python -m pytest services/file_sorter
```

The tests use temporary dump and sorted folders. They cover queue rules,
path safety, no-overwrite moves, folder units, untidy on-disk names, label
expansion, reorganizing with labels that follow, preview headers, and the
administrator-session policy. `test_audit_regressions.py` holds one test per
failure found in the 2 October 2026 edge-case audit.

Unit tests do not exercise the browser. Before deploying a UI change, drive
the real page and press every action (Classify by click and by Enter,
including during a slow load, Skip, Discard, Undo by key, button and
notification, the folder ⋯ menu's New, Rename, Move and Group, both tabs,
J/K, review Confirm and Move, duplicate KEEP THIS COPY), and check the moves
on disk. Use a throwaway library under `/tmp`, never a real one.

## Training exports

⋯ → **Export labels** downloads the current label manifest for the chosen
tree, including every project that feeds it. Superseded and undone decisions
are excluded. ⋯ → **Export decision log** downloads
the complete tree history: projects, raw decisions (including corrections,
duplicate relationships and undo stamps), operation journals, folder moves,
descriptions, document observation events and current index associations.
Keep that log alongside the
manifest; the SQLite backup is the authoritative recovery copy.

The later text-extraction converter should read each manifest `file_path`
relative to the library root, require `file_status: present`, and verify the
recorded size and SHA-256 before pairing content with its label. Presence
checks alone do not verify content. Background indexing records full hashes
even above the normal sorting hash budget. The manifest separates
`classification_provenance`, `needs_review`, `content_verified`,
`duplicate_eligible` and `training_eligible`. Human-reviewed content must
match the current indexed snapshot and have no known unresolved duplicate
or redundant-copy link to be eligible. Discovery rows need review; a
replacement leaves a separate old record with `file_status: replaced` and
no current `file_path`. Group routing examples by document,
and split by document or content hash to keep the same document out of both
training and validation sets. Neither export contains document text.
Exclude rows with `training_eligible: false` from routing training: discarding
an identical extra is a duplicate decision, not evidence that its content
belongs in the discard class.

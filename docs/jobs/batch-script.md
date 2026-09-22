# Batch Script Job

**Job type:** `batch`  
**Specification:** Home Platform Batch Script Standard, version 1  
**Status:** numeric arrays plus uploaded, verified HTTPS, and logical NAS file inputs

This is the authoring standard for the general PBS-like Home Platform job. The
live subset accepts a ZIP project, parses this wrapper, binds uploaded or
verified-HTTPS files by logical name, creates numeric array children, and runs
the wrapper with Bash inside the approved container. Job Desk can resolve
member-safe `Home/...` and `Shared/...` defaults declared in the header.
Directory inputs, dependencies, alternate runtimes, single non-array batch
jobs, and nested artifact publication remain pending.

The words **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are requirements. An
implementation MUST reject an invalid or unknown directive; it must not guess
what the author meant.

## Minimal valid script

```bash
#!/usr/bin/env bash
#HP --version 1
#HP --name "cohort analysis"
#HP --runtime scientific-python:1
#HP --cpus 2
#HP --memory-mb 2048
#HP --time-limit 02:00:00
#HP --input cohort=Home/Workspace/Inputs/cohort.csv
#HP --array 1-4

set -euo pipefail

bash "$HOME_PLATFORM_PROJECT_DIR/run-one.sh" \
  "$HOME_PLATFORM_ARRAY_INDEX"
```

The project submitted with this script must also contain `run-one.sh`. In Job
Desk, the declared default is resolved under the signed-in member, reviewed,
hashed, and exposed to every child at `$HOME_PLATFORM_INPUT_DIR/cohort`. An
advanced override can replace it for one submission.

## What is uploaded

A live batch submission consists of one immutable project object plus zero or
more named immutable file objects:

```text
project ZIP
├── submit.hp
├── child shell scripts
├── source and configuration
└── small project-owned test data

input bindings
└── logical-name -> uploaded file reference
```

The compressed upload limit is 20 MiB. The archive may contain at most 1,000
entries and expand to at most 100 MiB; absolute paths, traversal, backslashes,
encryption, and symlinks are rejected. The worker verifies the archive's size
and SHA-256 and repeats safe-path checks before extraction.

The CLI accepts a directory and creates the ZIP automatically, or accepts an
existing `.zip`. Each repeated `--input NAME=PATH` stages an arbitrary file up
to 20 MiB separately. The same reference is reused by every child and removed
from coordinator staging only when no unfinished child needs it.

For a larger file, bind a verified HTTPS source with the three matching options
`--input-url NAME=URL`, `--input-sha256 NAME=SHA256`, and
`--input-size-bytes NAME=BYTES`. Its bytes travel directly from the source to
the selected worker, not through the coordinator. The worker refuses redirects,
requires the source hostname to be explicitly allowlisted, verifies the exact
size and digest, and caches the content by digest. Array children normally
hard-link that cache entry into their private run directories, avoiding one
physical copy per child when cache and run storage share a filesystem.

Files already on the NAS may instead be bound with
`--input-storage NAME=LOGICAL_PATH`, using the same `Home/...` and `Shared/...`
vocabulary as Job Desk and `#HP` defaults. The coordinator resolves and
hashes the selected regular file without copying it into upload staging. The
coordinator serves those bytes from its authenticated Synology mount over the
worker's control-plane connection; the worker verifies the recorded size and
digest and uses the same content-addressed cache. This removes the browser
upload limit, but it is not yet the final direct-storage data plane.

## Header grammar

1. The file MUST be UTF-8 text and SHOULD use the `.hp` suffix to distinguish
   the PBS-like submission wrapper from ordinary project `.sh` files. A `.sh`
   suffix MAY also be accepted; the contents, not the extension, are normative.
2. Line 1 MUST be exactly `#!/usr/bin/env bash`.
3. Every Home Platform directive MUST use this form:

   ```text
   #HP --option VALUE
   ```

4. Directives MUST appear in the header block: after the shebang and before the
   first executable shell statement. Blank lines and ordinary comments MAY
   appear in that block.
5. Each directive occupies one physical line. Continuations are not allowed.
6. A value containing spaces MUST be shell-quoted. Quotes group text only;
   variables, command substitutions, and globs are never expanded in headers.
7. Windows CRLF line endings MAY be accepted and normalized.
8. Unknown options, missing values, malformed values, repeated singleton
   options, or `#HP` lines after execution begins MUST cause validation failure.

For parser authors, the canonical grammar is:

```text
script       = shebang, newline, header, body ;
shebang      = "#!/usr/bin/env bash" ;
header       = { blank | comment | directive } ;
directive    = "#HP", space, option, space, shell_word, newline ;
```

Parse the value as one shell word without performing shell expansion. A parser
must not execute or source a submitted file to read its headers.

## Directives

| Directive | Cardinality | Meaning |
|---|---:|---|
| `--version 1` | exactly 1 | Batch-script contract version; version 1 is the only value defined here |
| `--name TEXT` | exactly 1 | Human-readable run name; it need not be unique |
| `--runtime ID` | exactly 1 | Approved immutable logical runtime, such as `scientific-python:1` |
| `--cpus NUMBER` | exactly 1 | Requested CPU quota; decimal values such as `0.5` are valid |
| `--memory-mb INTEGER` | exactly 1 | Hard memory limit in MiB |
| `--time-limit HH:MM:SS` | exactly 1 | Hard wall-time limit |
| `--input NAME` | 0 or more | Declare a logical input that must be bound when submitted |
| `--input NAME=REFERENCE` | 0 or more | Safe default Job Desk reference rooted at `Home/...` or `Shared/...` |
| `--env KEY=VALUE` | 0 or more | Set a non-secret environment value |
| `--array START-END` | exactly 1 in the live subset | Create one child task for every integer in the inclusive range |
| `--after-success ID` | 0 or more | Start only after the named job or group succeeds |
| `--worker ID` | 0 or 1 | Request a particular registered worker |

The first six directives and `--array` are currently required. Explicit resource requests
make the script reviewable and reproducible instead of silently depending on a
client's defaults.

Use the table's order when writing a new script: version, name, runtime, CPU,
memory, time, inputs, environment, array, dependencies, and worker. Parsers MUST
accept any order within the header block, but a canonical order makes reviews
and AI-generated diffs predictable.

Version 1 applies these validation limits:

| Value | Rule |
|---|---|
| name | 1–100 characters after trimming; no control characters |
| runtime ID | `[a-z0-9][a-z0-9._-]{0,63}:[0-9][A-Za-z0-9._-]{0,31}` |
| CPUs | decimal from `0.1` through `8.0` |
| memory | integer from `256` through `16384` MiB |
| time | `HH:MM:SS`, from `00:00:01` through `168:00:00` (7 days) |
| worker ID | 1–64 characters matching `[A-Za-z0-9._-]+` |
| array range | `START-END`; `1 <= START <= END`, with at most 1,000 tasks |

Names used by `--input` and keys used by `--env` MUST match
`[A-Za-z][A-Za-z0-9_]{0,31}`. The `HOME_PLATFORM_` prefix is reserved and MUST
NOT be supplied through `--env`. Secrets MUST NOT appear in a script header.
Repeating an input name or environment key in the base header is invalid.

The live parser accepts named `--input`, default logical paths, `--env`,
`--array`, and `--worker`. Every declared input must resolve to exactly one
binding and undeclared bindings are rejected. A default must be a normalized
`Home/...` or `Shared/...` logical path; absolute paths, traversal, and physical
NAS paths are rejected. Session-authenticated Job Desk resolves defaults
server-side. The operator CLI still supplies explicit bindings and may override
a default. `--after-success` remains unavailable. The only registered runtime
is `scientific-python:1`.

A runtime ID names an operator-approved, versioned environment. It is not an
arbitrary Docker image, registry URL, host path, or mutable `latest` tag. The
runtime supplies Bash and all program dependencies because jobs have no network
access.

Logical input references MUST NOT be absolute host paths and MUST NOT contain
`..` path segments. Upload identifiers and verified remote-source objects are
normally supplied by the client rather than hard-coded in a reusable script.

## Submission precedence

The effective job contract is resolved in this order, from strongest to
weakest:

1. platform security policy and worker capacity ceilings;
2. explicit CLI or GUI submission values;
3. `#HP` directives in the script; and
4. platform defaults for optional fields.

A client override may lower or replace a requested value, but it cannot bypass
policy, make an unavailable runtime valid, or exceed a worker's capacity. The
coordinator records the effective values so a later reader can reproduce the
decision.

Current CLI submission syntax:

```bash
pixi run client --url CONTROL_PLANE_URL \
  submit-batch ./forecast --entrypoint submit.hp \
  --input observations=./data/observations.csv
```

Use `--worker ID` to override the header's optional worker choice.

For a file already on the NAS, named by the same logical path Job Desk shows:

```bash
pixi run client --url CONTROL_PLANE_URL \
  submit-batch ./forecast --entrypoint submit.hp \
  --input-storage observations=Shared/Datasets/observations.csv
```

`Shared/...` means the same directory to everyone. `Home/...` means the
signed-in member's own tree, so the operator CLI — which authenticates as the
administrator and can reach every tree — must name the account explicitly as
`Home/USER_ID/...`. A `#HP` default cannot use `Home` at all from an
administrator session, because a project header is written once and reused and
so cannot know whose private tree a later run should read.

For a linked input, repeat the same logical name across the URL, digest, and
size options:

```bash
pixi run client --url CONTROL_PLANE_URL \
  submit-batch ./forecast --entrypoint submit.hp \
  --input-url observations=https://data.example/observations.csv \
  --input-sha256 observations=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --input-size-bytes observations=123456789
```

Local, URL, and HomeStorage bindings cannot reuse the same name. Every declared
`#HP --input` must have exactly one effective binding, and every supplied
binding must be declared. Job Desk may derive that effective binding from a
safe header default. Obtain the size and SHA-256 for URL inputs from a trusted
copy or publisher; those fields are integrity requirements, not optional hints.

Job Desk exposes the same execution contract. Choose **PBS-style project
array** and select a project ZIP or a project folder already in storage. For a
storage folder it previews the job name, runtime, resources, array, and inputs.
Safe `Home/...` and `Shared/...` defaults resolve automatically; **Input
overrides** is needed only for a declaration without a default or a deliberate
per-run replacement. HomeStorage project folders are snapshotted into the same
bounded immutable ZIP used by CLI submissions.

## Filesystem contract

The container starts in `HOME_PLATFORM_PROJECT_DIR`. The platform exposes:

| Variable | Access | Meaning |
|---|---|---|
| `HOME_PLATFORM_PROJECT_DIR` | read-only | Submitted project tree |
| `HOME_PLATFORM_INPUT_DIR` | read-only | Directory containing one file per declared named input |
| `HOME_PLATFORM_OUTPUT_DIR` | writable | Durable result tree collected as artifacts |
| `HOME_PLATFORM_TMP_DIR` | writable, temporary | Scratch data discarded after the attempt |
| `HOME_PLATFORM_JOB_ID` | value | Server-generated job UUID |
| `HOME_PLATFORM_JOB_NAME` | value | Effective human-readable name |
| `HOME_PLATFORM_WORKER_ID` | value | Worker executing this attempt |
| `HOME_PLATFORM_ATTEMPT` | value | One-based attempt number |
| `HOME_PLATFORM_CPU_LIMIT` | value | Effective CPU quota |
| `HOME_PLATFORM_MEMORY_MB` | value | Effective memory limit |
| `HOME_PLATFORM_TIMEOUT_SECONDS` | value | Effective wall-time in seconds |
| `HOME_PLATFORM_ARRAY_INDEX` | value | Current numeric array index; present only for an array child |

For `#HP --input observations`, the stable path is
`$HOME_PLATFORM_INPUT_DIR/observations`. Scripts MUST NOT assume a worker
hostname, host path, user home, NAS mount, or drive letter.

The worker invokes the entrypoint with Bash inside the selected container. It
never runs submitted Bash in the macOS or Windows host shell. The container is
unprivileged, has a read-only root filesystem, no network, no platform
credentials, no Docker socket, and only the declared mounts.

## Results and exit behaviour

- Exit code `0` means `COMPLETED`.
- Any other exit code means `FAILED / EXECUTION_ERROR`, unless the platform
  reports a stronger cause such as `MEMORY_LIMIT_EXCEEDED`, `TIMED_OUT`,
  `CANCELLED_BY_USER`, or `WORKER_LOST`.
- Every top-level regular file below `HOME_PLATFORM_OUTPUT_DIR` is currently an
  artifact candidate. Nested artifact trees remain planned.
- Each array child publishes into its own directory beneath the submission's,
  keyed by array index, so one run reads as one tree:

  ```text
  artifacts/<owner-id>/cohort-analysis-3b2e91c4/
  ├── 1/result.txt
  └── 2/result.txt
  ```

  The directory name comes from `#HP --name` plus a short group UUID. A script
  cannot choose it, and two submissions sharing a name never collide. The
  `<owner-id>/` level is the prepared owner-scoped layout; the current
  deployment stores runs flat directly under the artifact root, and access is
  derived from job ownership in both — see
  [files in and out of a job](storage-workflow.md).
- Symlinks, devices, sockets, absolute paths, and paths containing traversal
  segments MUST NOT be published.
- Standard output and error are diagnostic logs, not the result transport.
- Normal artifacts are published only after successful completion. Failure
  diagnostics are retained separately; partial output is not presented as a
  successful result.

Authors SHOULD begin non-trivial scripts with `set -euo pipefail`, quote path
variables, and write temporary intermediate data to `HOME_PLATFORM_TMP_DIR`.
Current transfer limits are 100 MiB per file and 512 MiB per job; authors must
not assume those limits will be raised for the general runner.

## Task arrays

Use an array when many independent runs should be visible and schedulable
separately. For example, `#HP --array 1-4` creates four child jobs. Every child
runs the same submitted Bash wrapper with `HOME_PLATFORM_ARRAY_INDEX` set to
`1`, `2`, `3`, or `4`. The wrapper owns the meaning of that number and may call
another `.sh` file from the submitted project:

```bash
bash "$HOME_PLATFORM_PROJECT_DIR/run-one.sh" "$HOME_PLATFORM_ARRAY_INDEX"
```

The scheduler does not require or interpret a task manifest. A project may
still contain an ordinary file such as `samples.txt`; that file belongs to the
program, not to the scheduler. For example:

```bash
sample=$(sed -n "${HOME_PLATFORM_ARRAY_INDEX}p" \
  "$HOME_PLATFORM_PROJECT_DIR/samples.txt")
bash "$HOME_PLATFORM_PROJECT_DIR/align.sh" "$sample"
```

All children share the script's runtime, resources, project reference, and input
references. Each worker verifies and caches the bytes independently. A loop
that performs every run inside one child remains one scheduled
job and cannot be distributed. A numeric array creates independently
placeable, retryable, and cancellable children, while leaving arbitrary
dispatch logic in the submitted Bash code. This is analogous to a PBS job
array such as `-J 1-4`; it is not a workflow-description language.

### How arrays appear in Job Desk

One submission creates one durable **job group** and one child **task run** per
integer in the range. The queue presents the group as one row by default:

```text
regional forecast                         2 / 4 complete
├── north       COMPLETED   worker-a
├── south       RUNNING     worker-b
├── east        QUEUED      —
└── west        FAILED      worker-a   MEMORY_LIMIT_EXCEEDED
```

Expanding the row reveals task status, assigned worker, attempts, failure kind,
logs, and artifacts. A group's displayed state is derived from its children:

| Children | Group state |
|---|---|
| every task is queued | `QUEUED` |
| any task is running, or queued after another finished | `RUNNING` |
| every task completed | `COMPLETED` |
| no task is active and at least one failed | `FAILED` |

Cancelling a group will cancel queued children and request cancellation of
running children. Cancelling one child affects only that task. A group ID and
task ID are distinct from every child job UUID; repeated display names never
establish membership.

Group persistence, atomic child creation, numeric `#HP --array` expansion,
independent claiming, aggregate state, Bash execution, artifact publication,
and the expandable Job Desk view are implemented and live-verified. Group-wide
cancellation remains pending; individual children can already be cancelled.

## Complete project example

```text
forecast/
├── submit.hp
├── forecast.py
├── run-one.sh
└── config.yaml
```

`submit.hp` is the PBS-like submission wrapper:

```bash
#!/usr/bin/env bash
#HP --version 1
#HP --name "regional forecast"
#HP --runtime scientific-python:1
#HP --cpus 1.5
#HP --memory-mb 1536
#HP --time-limit 00:30:00
#HP --input observations
#HP --env MODEL=linear
#HP --array 1-4

set -euo pipefail

bash "$HOME_PLATFORM_PROJECT_DIR/run-one.sh" \
  "$HOME_PLATFORM_ARRAY_INDEX"
```

`run-one.sh` maps the scheduler's number to this project's datasets and model
choices. Other projects may use the number completely differently.

```bash
#!/usr/bin/env bash
set -euo pipefail

index=$1
offset=$((index - 1))
regions=(north south north south)
models=(linear linear random-forest random-forest)

python "$HOME_PLATFORM_PROJECT_DIR/forecast.py" \
  --config "$HOME_PLATFORM_PROJECT_DIR/config.yaml" \
  --observations "$HOME_PLATFORM_INPUT_DIR/observations" \
  --region "${regions[$offset]}" \
  --model "${models[$offset]}" \
  --output "$HOME_PLATFORM_OUTPUT_DIR"
```

## Rules for human and AI authors

Before presenting a batch script, verify every item:

- Use only directives listed in this document; never invent a PBS or Slurm flag.
- Include the exact Bash shebang and all six required singleton directives.
- Put every directive before the first executable statement.
- In Job Desk, declare ordinary NAS defaults as
  `#HP --input NAME=Home/...` or `Shared/...`. Leave off the default only when
  the submitter should choose a file for every run.
- In the operator CLI, bind small local files with `--input NAME=PATH`, NAS
  files with `--input-storage NAME=Shared/...` or `NAME=Home/USER_ID/...`, or
  externally hosted large files with the URL, SHA-256, and byte-size triple.
- Address project files through `HOME_PLATFORM_PROJECT_DIR` and write durable
  results only below `HOME_PLATFORM_OUTPUT_DIR`.
- Give the submission a `#HP --name` worth reading later; it becomes the
  directory its array children publish into.
- Never embed credentials, device names, host paths, or private network details.
- Do not install dependencies or download data during execution.
- Use a named immutable runtime containing all dependencies.
- Use a numeric array for independently schedulable work, not a hidden loop.
- Treat `HOME_PLATFORM_ARRAY_INDEX` as the only scheduler-provided task
  selector; keep index-to-input logic in project code.
- Treat non-zero exit as failure and keep important output out of stdout alone.

If a requirement cannot be represented by this contract, say so explicitly.
Do not silently encode it in an unrecognized header or depend on worker-specific
behaviour.

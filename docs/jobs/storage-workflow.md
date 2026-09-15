# Files In and Out of a Job

How a member's files are organised, how the platform resolves them, and how they
reach a job and come back as published output.

> The member-safe Home/Shared picker and resolved input defaults are
> implemented. Workspace create/upload is active only for the provisioned pilot
> member after its separate NAS identity, mount, and ACL matrix were accepted;
> browser and multi-user acceptance remain pending.

Files reach a job through the `HomeStorage` share rather than a browser upload
form, which keeps projects and datasets off the coordinator while preserving the
same PBS-style job contract. The original Pi-hosted Samba share is a migration
bridge: member `Home` and `Shared` paths already resolve on the dedicated
Synology NAS, while published output stays on the Pi SSD until its separate
cutover. In the end state the NAS is the only SMB server.

Directory names below are real; account identifiers are placeholders.

## File areas

A member never addresses storage by its real location. Three logical areas are
presented instead, each resolved against the signed-in account:

```text
Home/                        private to you
├── Workspace/               the only area the browser may write to
│   ├── Inputs/              files you supply for jobs to read
│   └── Projects/            submit.hp and the scripts it calls
└── ...                      anything else you add over SMB

Shared/                      common to every member; read-only in the browser
└── ...                      reference data worth reusing across accounts

Artifacts/                   published output of the jobs you own
└── <job-name>-<short-id>/   one directory per submission
    ├── 1/  metrics.json …   one per array index, for a PBS-style array
    └── 2/  metrics.json …
```

| Area | Read | Browser writes | SMB writes | Others see it |
|---|---|---|---|---|
| `Home` | you | `Home/Workspace` only | you | no |
| `Shared` | every member | no | every member | yes, by design |
| `Artifacts` | your own jobs | delete only | read-only | no |

Deleting artifacts removes bytes, never the job record — status, failure reason
and file names survive. It is refused while one of your jobs is running.

### How they resolve

`Home` and `Shared` are path rewrites: one logical path, one stored path.

```text
Home/notes.csv     ──►  HomeStorage/users/<stable-user-id>/notes.csv
Shared/ref.fa      ──►  HomeStorage/shared/ref.fa
```

`Artifacts` is not. Published output is stored flat, so every member's runs are
siblings under one root and a path cannot establish who may read it. The
readable set is assembled from the jobs a member owns — which stays correct if
the prepared per-owner layout is enabled later. A run appears only if you own
the job **and** it published files.

## Share layout

This is the structure the logical areas are built from. An administrator sees it
directly, as `Storage/` and `Artifacts/`, because administrative scope spans
every account:

```text
HomeStorage/
├── users/                        one directory per member, keyed by account ID
│   ├── <stable-user-id>/         a member's private Home
│   │   └── Workspace/            the only browser-writable subtree
│   │       ├── Inputs/
│   │       └── Projects/
│   └── <stable-user-id>/         another member; mutually unreadable
├── shared/                       common to every member
├── projects/                     transitional: operator-managed project folders
├── inputs/                       transitional: operator-managed job inputs
└── artifacts/                    published job output
    ├── <job-name>-<short-id>/    one directory per submission
    │   ├── 1/                    one per array index, for a PBS-style array
    │   └── 2/
    └── <job-name>-<short-id>/    a different member's run, side by side
```

Note that `users/<a>/` and `users/<b>/` are siblings: the path shape prevents
nothing. The `Home` mapping confines a member to their own tree, and filesystem
permissions enforce the same boundary independently for anyone on SMB.
`artifacts/` is flat for the same reason `Artifacts` is derived from ownership;
setting `HOME_PLATFORM_ARTIFACT_OWNER_SCOPED` inserts an `<owner-id>/` level but
does not change who sees what.

Two ways in: connect over SMB with Finder, Explorer, or the iOS Files app for
large trees, or use **Files** in Job Desk for browsing, downloads, and bounded
`Home/Workspace` edits.

## Submit from Job Desk

1. Prepare the complete project folder in your private `Home` tree.
2. Keep external personal inputs in `Home` and deliberate collaboration data in
   `Shared`.
3. Open Job Desk and choose **PBS-style project array**.
4. Choose **Folder on the NAS**, select **Browse**, and choose the project
   directory.
5. Leave the entrypoint as `submit.hp`, unless the project uses another safe
   project-relative name.
6. Review the contract detected from `submit.hp`. A declaration such as:

   ```bash
   #HP --input cohort=Home/Inputs/cohort.csv
   #HP --input reference=Shared/References/reference.fa
   ```

   resolves automatically. Use **Input overrides** only for a declaration with
   no default or to replace a default for this run.

7. Choose automatic placement or a specific worker and submit.

The platform packages the project folder into a bounded immutable ZIP. Input
files are not copied into upload staging. Each becomes a reference containing
the logical storage ID, safe relative path, exact size, and SHA-256. Workers
verify those fields before making the file visible at
`$HOME_PLATFORM_INPUT_DIR/NAME`.

## Submit from the CLI

The project may still be local while its data is already in HomeStorage:

```bash
pixi run client submit-batch ./cohort-analysis \
  --input-storage cohort=Home/USER_ID/Inputs/cohort.csv \
  --input-storage reference=Shared/References/reference.fa
```

CLI and Job Desk use one vocabulary and create the same
`BatchSubmissionCreate` contract and the same parent/child records. The
difference is whose `Home` is meant: a signed-in member's session supplies that
implicitly, while the CLI authenticates as the administrator — whose reach
spans every tree — and so must name the account as `Home/USER_ID/...`.
`Shared/...` is identical for both.

The same flag exists for Python script jobs as `--dataset-storage`:

```bash
pixi run client submit-python-batch train.py \
  --dataset-storage Shared/Datasets/training-data.csv \
  --name "cohort model"
```

## Current boundaries

- HomeStorage inputs are regular files. Directory inputs are the next storage
  contract extension.
- Application member accounts are owner-scoped. Provisioned members see virtual
  `Home/...` and `Shared/...` paths; the server maps `Home` to the signed-in
  account's stable UUID and rejects paths outside those roots. Other accounts
  fail closed until explicitly provisioned.
- Job Desk folder creation and file upload are limited to
  `Home/Workspace/...` and require a separate workspace service mount. Those
  mutations are live for the provisioned pilot member after the separate mount
  and NAS access matrix passed. The global flag remains false, and browser
  create/upload acceptance plus second-member isolation are still pending.
- Do not rename or edit an input after submission. If its bytes no longer match
  the recorded digest, the worker fails safely instead of running changed data.
- Because the present SSD is physically attached to the coordinator, its
  authenticated API serves selected file bytes to workers. They are streamed
  rather than placed in SQLite or copied to microSD staging. A dedicated NAS
  should later resolve the same logical reference directly to workers.
- Project archives remain limited to 20 MiB compressed, 100 MiB expanded, and
  1,000 entries. Put large data in named inputs, not inside the project.

Job Desk uses the authenticated storage browse API for a visual folder/file
picker. Paths are still validated and resolved by the server; hiding a path in
the browser is never treated as an authorization boundary.

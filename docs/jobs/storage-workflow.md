# Getting Files Into a Batch Job

> The member-safe Home/Shared picker and resolved input defaults are
> implemented. Workspace create/upload is active only for the provisioned pilot
> member after its separate NAS identity, mount, and ACL matrix were accepted;
> browser and multi-user acceptance remain pending.

The first storage-backed workflow uses the `HomeStorage` Samba share. It keeps
projects and datasets out of browser upload forms while preserving the same
PBS-style job contract that a future dedicated NAS will use.

The original Pi-hosted share is a migration bridge. The provisioned member's
logical Home/Shared paths now resolve on the dedicated NAS, while artifacts
remain on the Pi SSD until their separate cutover. In the end state the
dedicated NAS is the only SMB server.

## Share layout

```text
HomeStorage/
├── projects/   project folders containing submit.hp and called scripts
├── inputs/     administrator-managed workload inputs during the transition
├── shared/     deliberately reusable household files
└── artifacts/  completed job output; read-only through Samba
```

Connect to the share with Finder, Windows Explorer, or the iOS/iPadOS Files app
using the private server name supplied by the operator. Uploads happen through
SMB, not through Job Desk, so ordinary file-copy tools handle large files and
folders.

## Submit from Job Desk

1. Prepare the complete project folder in your private `Home` tree.
2. Keep external personal inputs in `Home` and deliberate collaboration data in
   `Shared`.
3. Open Job Desk and choose **PBS-style project array**.
4. Choose **HomeStorage project folder**, select **Browse**, and choose the
   project directory.
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

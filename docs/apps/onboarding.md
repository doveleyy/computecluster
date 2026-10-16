# Register a published application

Operators can register an app and prepare its host policy with one command.
The command creates the app account, private directories and application
credential. It appends one policy entry while preserving the other apps'
fingerprints. Installation still requires the administrator's preview and
approval workflow.

## Prepare the platform and publication

The control plane must already have a migrated database and a configured
application instance. The root installer runtime, policy and shared host
directories must already be provisioned. The policy's database, core identity,
environment, app API URL and timezone must match that instance. A new host can
start with an empty `apps` array. Onboarding does not provision the platform
itself or migrate its database.

The app must have a stable release from its publication workflow. Onboarding
verifies the latest stable release through the same GitHub publication checks
as release discovery. The repository, branch, workflow and image repository
are operator inputs. The verified manifest supplies the application ID,
capabilities, SQLite state definition, maintenance tasks and probe endpoints.
Release verification runs as the core account, without new privileges.

For a private repository, provide a private release-token file readable by
root. Its owner must be root or the core account. The token travels to the
unprivileged verifier through standard input. It is absent from command
arguments, policy, onboarding records and output. Separately configure the
installer's registry login when the container image requires authentication.
Onboarding does not test image pulls.

## Write the operator specification

The smallest specification names the repository and its read-only
compatibility command.

```json
{
  "repository": "example/notes",
  "compatibility_argv": ["python", "-m", "notes.check_state"]
}
```

The compatibility command runs inside the app image during a migration
rehearsal. Its stdout must be nonempty and deterministic. See
[Migration compatibility](installation.md#migration-compatibility).

Optional fields customize the operator's registration.

| Field | Default or meaning |
|---|---|
| `branch` | `main` |
| `workflow` | `.github/workflows/publish.yml` |
| `image_repository` | `ghcr.io/` followed by the lowercase repository |
| `access` | `administrator`; use `linked-user` to admit linked members |
| `route` | `/` followed by the manifest's application ID |
| `label` | Application ID with words capitalized |
| `accent` | `#6b9bd2` |
| `kicker`, `description` | Empty strings |
| `settings` | Array of `{"name": "NOTES_THEME", "value": "dark"}` |
| `resources` | The standard registration resource policy |
| `secrets` | Array of `{"name": "provider-key", "file_path": "/etc/example/key/value"}` |
| `mounts` | Array of `{"name": "library", "source": "/opt/example/library", "read_only": true}` |

Secret bindings must exactly match the manifest's secret names. The operator
must provision these files first, owned by root and private to root. Each
secret's parent directory must be separate from every other policy storage
root. Mount sources must already exist, resolve without symlinks and belong
to the planned application UID. Onboarding checks these prerequisites and
does not change existing secrets, mounts or unrelated directory ownership.

Maintenance timeouts must fit `resources.maintenance_timeout_seconds`. The
default is 300 seconds. Increase it explicitly when the manifest requires a
longer task.

## Review and apply

Use the root-owned installer's Python interpreter in isolated mode. Replace
the placeholders with the provisioned runtime, policy and optional token file.

```console
sudo <installer-python> -I -m ops.onboard notes.json --policy <policy-file> --release-token-file <private-token-file> --dry-run
sudo <installer-python> -I -m ops.onboard notes.json --policy <policy-file> --release-token-file <private-token-file>
```

Omit `--release-token-file` for public repositories. The policy path is always
explicit. Dry run prints the proposed account, port, route and directories,
checks prerequisites and writes no host or core records. If a mount needs the
new UID, use the printed proposal when provisioning it, then repeat the dry
run. Allocation can change until an apply reserves it.

Apply has no additional confirmation prompt. It holds `installer.lock`, pins
the verified manifest and allocation in a root-owned onboarding record, then
creates only missing resources. Account IDs are chosen above the observed
account and policy IDs and prior onboarding reservations. Group IDs, listening
ports and other reservations' ports, IDs and routes also participate in
collision checks. Keep onboarding records
when removing apps so their IDs remain reserved.

The command uses these generic path conventions.

| Resource | Path |
|---|---|
| App account | `hp-<app-id>` |
| App state | `/var/lib/home-platform/<app-id>` |
| Backups | `/var/lib/home-platform-installer/backups-<app-id>` |
| Credential directory | `/var/lib/home-platform-installer/credentials-<app-id>` |
| Credential file | `app-credential` inside that directory |
| Onboarding record | `onboarding/<repository-sha256>.json` inside the policy journal |

Every app gets state and backup directories. A SQLite app also gets a stable
daily backup schedule between 01:00 and 05:59 in the instance timezone, with
up to ten minutes of jitter. Stateless apps get no backup timer.

After registration and credential creation, the command validates the full
candidate policy against the real host. It then replaces the policy
atomically. A conflicting registration, unsafe path, changed policy or
occupied resource stops the command. Earlier completed resources stay in
place for a retry. Repeat the same command after correcting the prerequisite.
Retries reuse the pinned manifest and allocation, verify active credentials
and never rotate them implicitly.

Changing a reserved specification is refused. An exact existing registration
or policy entry can be adopted; different definitions require the separate
operator registration-change workflow. Onboarding never edits another app's
entry or approves a release.

## Complete installation

Open the administrator's Apps page, check for updates, select the verified
release, run Preview and Check compatibility, then Approve and deploy.
Onboarding does not run an installer pass, start a container, enable a worker
or bypass those checks. See [Approval and installation](installation.md).

## Follow the implementation

`ops/onboarding.py` owns the specification, observed facts and pure plan. Its
single registration builder supplies both the core record and the host policy.
`ops/onboard.py` owns the root CLI, host checks and ordered effects.
`ops/onboard_child.py` owns release verification and core database writes under
the core identity. The routine `installer_child` keeps its read-only database
contract. These paths join only at the onboarding command.

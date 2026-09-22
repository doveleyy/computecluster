# Accounts and access control

Home Platform has two human roles and one separate machine credential. The
roles control application records; Synology DSM ACLs and SMB authentication are
a second boundary.

## Roles

| Capability | Member | Administrator |
|---|---:|---:|
| Sign in to Job Desk | Yes | Yes |
| Submit jobs and uploaded inputs | Yes | Yes |
| List, inspect, cancel, or download jobs | Own only | All |
| Reuse staged uploads | Own only | All |
| See worker identity and job ceilings | Yes | Yes |
| See worker telemetry and current job IDs | No | Yes |
| Open the operations Dashboard | No | Yes |
| Enable workers, change capacity, or control Pi power | No | Yes |
| Create or disable member accounts | No | Yes |
| Select files from the NAS Home/Shared tree | Provisioned members | Yes |
| Browse or download NAS files | Own Home and Shared only | All provider paths |
| Create folders or upload through Job Desk | Provisioned Workspace only | No |
| Rename, move, copy, or delete working files | Own Workspace only | No |
| Browse or permanently delete job results | Own only | Via job/operator APIs |

The API token is the elevated administrator and machine credential used by the
CLI and workers. It must never be given to a member. Members authenticate with
their own username and password through Job Desk.

## Account lifecycle

An administrator opens **Operations** from the Dashboard with the owner API
token. The **Members** panel creates a lowercase username and an initial password of at least 12
characters. The server stores an independently salted scrypt hash, never the
plaintext password. The same panel can disable or re-enable a member.

Disabling is immediate: every request resolves the signed session back to the
current user record, so an already-issued cookie stops working as soon as the
account is disabled. The administrator account is bootstrapped by migration and
cannot be disabled through the member endpoint.

Members can change their own application password from Job Desk. The owner can
reset a member password from Operations. Either operation increments a
server-side session version, immediately invalidating every browser session for
that member; the changed account must sign in again.

Browser sessions are HttpOnly, SameSite=Strict, signed with the server-side API
secret, and expire after 30 days. A session contains only a stable user ID,
expiry, and revocation version. Username, role, credential version, and disabled
state are read from SQLite on each request; changing server-side account state
therefore does not depend on waiting for a cookie to expire.

## Ownership model

Jobs and job groups have an immutable `owner_user_id`. Existing records were
backfilled to the administrator. Database triggers reject a missing owner and
reject attempts to change an owner after insertion.

Uploaded datasets, scripts, projects, and named inputs are registered to the
authenticated uploader. A member submission may reference only uploads owned
by that same member. Knowing another upload or job UUID does not grant access:
foreign detail, cancellation, artifact-list, and artifact-download requests
return the same not-found response as an unknown ID.

Idempotency keys are unique per owner rather than globally. Two members may use
the same client-generated key without seeing or colliding with each other's
submission.

The CLI remains an administrator interface for now. Per-user API tokens are not
implemented, so family members should use Job Desk rather than receiving the
shared service token.

Administrator Job Desk receives a separate administrator-only ownership map so
rows and details can show the owning username. This keeps owner metadata out of
the worker wire contract and does not expose the cross-user map to members.

## Storage boundary

Application ownership is live before multi-user NAS acceptance. Provisioned
members may browse only virtual `Home` and `Shared` roots; unprovisioned members
fail closed. Filtering a path in the browser is never treated as protection for
the same file over SMB.

The storage layer uses one private Synology location per stable user ID, a
shared collaboration location, and an administrator view across all users.
The DSM groups, service identities, directory tree, first-member ACL matrix,
and application storage cutover are live. A second-member cross-user acceptance
test remains pending. DSM filesystem ACLs and SMB authentication enforce
the disk boundary; the application maps a member's logical paths only into
their private tree or the shared tree. Application passwords and SMB passwords
remain separate credentials.

The Files browser uses logical paths. A request for `Home/Projects/model.py` is
mapped on the server to the signed-in member's stable storage directory; its
listings and download URLs never contain the physical UUID. `Shared/...` maps
to the common tree. Download authorization repeats this mapping for every
request rather than trusting a path previously rendered by JavaScript. The
current web mutation surface remains limited to `Home/Workspace`; Shared is
read-only. The older job-submission adapter still round-trips a verified
provider reference and is a separate remaining cleanup.

`Artifacts` is a virtual provider assembled from the jobs a member owns. Its
Synology layout is also owner-scoped, but filesystem placement never replaces
the database authorization check. Access is not granted by guessing a job or
directory UUID. Members may delete their own artifact files or clear their
entire Artifacts tree permanently, but the application refuses this while one
of their jobs is running. Deleting bytes does not delete the durable job record.

Browser-originated workspace writes use a distinct, disabled-by-default service
identity and mount. On NAS systems with a share-level SMB gate, that identity
may need Read/Write at the share gate so the CIFS session can mount; directory
ACLs must then provide the real least-privilege boundary. The accepted pilot
grants traverse-only on the member parent, Read/Write on that member's
`Workspace/` descendants, no write to `Shared`, and an explicit deny on
`artifacts`. The API additionally binds every request to the session's immutable
owner ID and accepts only `Home/Workspace/...`. Because the NAS sees the shared
service identity rather than the human actor, this is application-enforced
per-user isolation with a storage-level blast-radius limit—not true delegated
NAS identity. Artifact publishing remains a separate credential and permission
boundary. Cross-user denial still requires a second-member acceptance test.

Do not expose Job Desk or SMB beyond the private network, and do not enable
router forwarding or a public tunnel as a substitute for authorization.

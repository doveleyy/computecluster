# Accounts and access control

Home Platform has two human roles and one separate machine credential. The
roles control application records; Synology and Samba permissions are a second
boundary that is being introduced separately.

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
| Create folders or upload through Job Desk | Provisioned Workspace only | No |

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
The initial DSM groups, service identity, directory tree, and first-member ACL
matrix are provisioned, but cross-user denial and full application cutover
remain acceptance gates. DSM filesystem ACLs and Samba authentication enforce
the disk boundary; the application maps a member's logical paths only into
their private tree or the shared tree. Application passwords and SMB passwords
remain separate credentials.

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

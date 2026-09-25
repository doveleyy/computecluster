# Accounts and access control

Home Platform has two human roles and one separate machine credential. The
roles control application records; Synology DSM ACLs and SMB authentication are
a second boundary.

## Roles

| Capability | Member | Administrator |
|---|---:|---:|
| Sign in to Job Desk | Yes | No — operator views instead |
| Submit jobs and uploaded inputs in the browser | Yes | No |
| List, inspect, cancel, or download jobs | Own only | All, from operator Jobs |
| Reuse staged uploads | Own only | No browser submission path |
| See worker identity and job ceilings | Yes | Yes |
| See worker telemetry and current job IDs | No | Yes |
| Open the operations Dashboard | No | Yes |
| Enable workers, change capacity, or control Pi power | No | Yes |
| Create or disable member accounts | No | Yes |
| Select job inputs from the NAS Home/Shared tree | Provisioned members | No job submission path |
| Browse or download NAS files | Own Home and Shared only | All provider paths |
| Create folders or upload | Own provisioned Workspace | Any member Workspace through operator Files |
| Rename, move, copy, or delete working files | Own Workspace only | Any member Workspace; never across accounts |
| Browse or permanently delete job results | Own only | All artifacts through operator Jobs/Files |

The API token is the elevated administrator and machine credential used by the
CLI and workers. It must never be given to a member. Members authenticate with
their own username and password through Job Desk. The browser roles are
deliberately asymmetric: a member submits work, while an administrator
observes and operates the platform but cannot submit through Job Desk.

## Account lifecycle

An administrator opens **Operations** from the Dashboard with the owner API
token. The **Members** panel creates a lowercase username and an initial
password of at least 12 characters. The server stores an independently salted
scrypt hash, never the plaintext password. The same panel can disable or
re-enable a member.

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

## Linking application-service identity

The stable Home Platform user UUID remains the owner identity across jobs,
artifacts, storage, and application services. A private-network login such as a
Tailscale account is an authentication method, not a replacement owner ID.

A signed-in user may link the Tailscale identity attached by the trusted HTTPS
reverse proxy to their existing Home Platform account. The server requires both
proofs in the same request and never links accounts by comparing email or
username text. One Tailscale identity can belong to only one Home Platform user,
and one user can have only one linked identity for that provider.

Application services resolve the external subject through a loopback-only
service call protected by a dedicated least-privilege token. They receive the
stable user UUID, username, and role; they never receive password hashes, the
browser cookie signing secret, or the elevated job/worker API token. A disabled
user does not resolve. Services store the returned UUID in their own database,
preserving database-per-service without allowing them to open the control-plane
SQLite file directly.

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

Operator Jobs receives a separate administrator-only ownership map so
rows and details can show the owning username. This keeps owner metadata out of
the worker wire contract and does not expose the cross-user map to members.

## Storage boundary

Two boundaries protect the same files, and both are real. The application maps
a member's logical paths only into their own tree or the shared tree, and
re-checks that mapping on every request rather than trusting a path the browser
previously rendered. Independently, file-server permissions enforce the same
boundary over SMB, where the application is not involved at all. Filtering a
path in the browser is never treated as protection for the same file over SMB.

Application passwords and file-server passwords are separate credentials.
Provisioned members browse only their private and shared roots; unprovisioned
members fail closed. See [Storage](storage/README.md) for the file areas
themselves.

`Artifacts` is assembled from the jobs a member owns rather than rewritten from
a path, so filesystem placement never replaces the database authorization
check, and guessing a job or directory UUID grants nothing. Members may
permanently delete their own artifact files or their whole artifact tree, but
the application refuses while one of their jobs is running, and deleting bytes
never deletes the durable job record.

Browser-originated writes use a distinct, disabled-by-default service identity
and mount, confined to member workspace subtrees, with no write to the shared
tree and no artifact write privilege. A member route can name only its own
Workspace. An administrator may manage any member Workspace, but a move or copy
cannot cross accounts. Because the file server sees the service identity rather
than the human actor, this is application-enforced per-user isolation with a
storage-level blast-radius limit — not delegated file-server identity. Artifact
publishing and operator artifact deletion remain a separate credential and
permission boundary.

Do not expose the web interfaces or SMB beyond the private network, and do not
enable router forwarding or a public tunnel as a substitute for authorization.

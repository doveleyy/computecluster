# Release discovery and isolated previews

Publication evidence is separate from the strict v1 manifest. A registered
source fixes the repository, push branch, workflow path, and image repository.
The platform accepts a release only after GitHub reports a successful push run
and the downloaded workflow artifact matches GitHub's SHA-256 digest.

The artifact contains `manifest.json`, `publication.json`, and every workload
asset. The release must contain exactly the same bytes. Publication metadata
records the repository, branch, workflow, source commit, run ID, run attempt,
manifest hash, and asset hashes. It excludes its own hash. Artifact names
include
the run attempt so that a rerun cannot reuse an earlier attempt's proof.

Discovery keeps verified archive bytes in an immutable store. Its latest
candidate pointer changes independently of selected preview attempts. Manual
manifest ingestion does not establish workflow authority. Provider failures
preserve installed applications, accepted bytes, and the last successful check.
A conditional release-list response does not suppress retries for candidates
whose workflow was still running.

A release that fails verification is retried with backoff. The delay starts
at five minutes and doubles after each failure. After five failed attempts,
discovery stops checking that release and reports it as abandoned. Attempt
counts and retry times survive restart. A provider error, timeout, or rate
limit during verification is not a release failure. It reports the provider
as unavailable, records no attempt, and leaves the last successful check
unchanged. Discovery logs and skips an invalid stored registration, then
continues with the other applications.

The discovery task is disabled by default. When enabled through
`HOME_PLATFORM_RELEASE_DISCOVERY=true`, it checks every five minutes.
`HOME_PLATFORM_RELEASE_TOKEN_FILE` supplies an optional private-repository
read credential. The `check-updates` operator command calls the same discovery
operation. The store refuses further releases at its byte ceiling instead of
silently deleting evidence selected by an operator. A full store is not a
release failure. Discovery reports the store as full, records no attempt, and
leaves the last successful check unchanged. The release is retained on the
first check after space is freed.

Each preview attempt binds an application, manifest hash, and verified archive
hash. The authenticated staging API checks all three on every upload and status
request. A lost response leaves the same attempt reserved until reconciliation.
Replacing an attempt requires explicit retirement. Retiring an attempt that
staging reports as unknown closes it locally, because staging refused it
before recording it. A ready preview retains its application slot while another
application can install. Only one installation can run globally in the guest.

The guest owns its journal and Docker lifecycle. It derives state paths,
networks, container names, ports, accounts, credentials, and resource limits.
Release metadata cannot supply host commands, bind mounts, or core and worker
images. Operator policy supplies fixed core and worker images plus synthetic
provider bindings. Missing bindings refuse installation.

The application has a separate peer in an internal Docker network. Its core
callbacks cannot impersonate the loopback proxy. A loopback bridge lets the
existing gateway reach the application. Browser ingress uses a separate
authenticated proxy with a per-attempt secret that the application cannot read.
The core has no Docker socket. A separate synthetic worker uses the guest's
Docker daemon and the existing native execution contracts.

Staging reports actual container image identity, startup, readiness, version
identity, linked-user access, direct-request denial, and forged-proxy denial.
Restart reconciliation inspects only resources carrying the guest's ownership
labels. It rechecks ready previews, removes unknown owned resources, and cleans
interrupted installations before allowing another installation.

The [publication example](../../examples/apps/publication/README.md) provides
a standalone workflow and strict parser for an independent repository.

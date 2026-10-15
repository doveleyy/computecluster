# Application CI/CD pipeline

Every household application lives in its own repository and ships through the
same pipeline. GitHub Actions builds and publishes each release. The core
platform then discovers it, verifies it, and tries it in isolation. An
administrator approves it before anything reaches production. Nothing in the
pipeline deploys on its own: CI publishes evidence, and a person decides.

This page is the overview. The linked contracts hold the exact rules.

## The path of a change

```
  App repository                    GitHub
 ┌──────────────┐  push to main  ┌───────────────────────────────────────┐
 │ code, tests, │ ─────────────▶ │ GitHub Actions                        │
 │ Dockerfile,  │                │  1. test and lint                     │
 │ manifest     │  pull request  │  2. build a multi-architecture image  │
 └──────────────┘ ─ ─ ─ ─ ─ ─ ─▶ │  3. push it to the container registry │
                  (checks only)  │  4. publish a release: manifest and   │
                                 │     workflow proof, pinned by digest  │
                                 └───────────────────┬───────────────────┘
                                                     │ read-only discovery
                                                     ▼
 ┌───────────────────────────────────────────────────────────────────────┐
 │ Core platform                                                         │
 │                                                                       │
 │  Discover ──▶ Verify proof ──▶ Eligible release                       │
 │                (workflow run, branch, commit, hashes)                 │
 │                                    │                                  │
 │                    Apps page       ▼                                  │
 │                ┌──────────────────────────────────┐                   │
 │                │ Preview         isolated staging │ ◀─ synthetic data │
 │                │                 guest, startup,  │                   │
 │                │                 identity checks  │                   │
 │                ├──────────────────────────────────┤                   │
 │                │ Compatibility   migrate a copy   │ ◀─ copy of the    │
 │                │                 of current data  │    real database  │
 │                ├──────────────────────────────────┤                   │
 │                │ Approve         administrator    │                   │
 │                │ and deploy      decision         │                   │
 │                └────────────────┬─────────────────┘                   │
 │                                 ▼                                     │
 │         Installer: pull by digest ─▶ back up ─▶ switch ─▶ probe       │
 │                                 │                                     │
 │                       healthy ──┴── unhealthy                         │
 │                         │               │                             │
 │                    new version     previous version restarted         │
 └───────────────────────────────────────────────────────────────────────┘
```

## Stages

**Build (CI).** A pull request runs the app's tests and checks only. A push to
the main branch also builds the container image for every supported
architecture and pushes it to the registry. The workflow then publishes a
release containing the app's manifest and a proof record. The proof names the
workflow run, the source commit, and the hash of every published file. The
manifest pins the image by digest, never by a movable tag.

**Discover and verify (core).** The core checks each registered repository
with a read-only credential, on a schedule or when an operator presses Check
for updates. A release becomes eligible only when GitHub
confirms that a successful push run on the registered branch and workflow
produced it, and when every hash matches. Hand-uploaded or edited releases do
not qualify.

**Preview (staging).** An operator presses Preview on the Apps page. The core
sends the release to an isolated staging guest that holds only synthetic
data. There the app must start, become ready, and enforce the platform's
identity rules. The core accepts the preview only when every check passes.
The staging guest can stay off between releases; Preview reports when it is
unreachable, and the same attempt resumes once it is back.

**Compatibility.** The production host rehearses the release against a copy
of the app's current database. It runs the app's migrations on the copy and
compares the app's state probes before and after. The real data is not
touched.

**Approve and deploy.** An administrator approves the exact release that
passed both checks. A narrowly scoped installer pulls the image by digest and
backs up the app's data. It switches the app to the new version and probes
it. The new version is accepted only when it answers healthy. Otherwise the
installer restarts the previous version against the current data.

**Roll back.** An accepted installation keeps its predecessor. Rollback
restores the previous image against the current data, so no records are lost.
A settings change follows the same approval path as a new release.

## What each side owns

| Owner | Responsibility |
|---|---|
| App author | Code, tests, the workflow, the image, and the manifest |
| GitHub Actions | Building, publishing, and recording proof of the build |
| Core platform | Verification, previews, compatibility, approval records |
| Administrator | The decision to deploy, roll back, or reject |
| Installer | The only host change: pull, back up, switch, probe |

## Further reading

- [Build an independent app](apps/build-an-app.md) — the author's workflow
- [Release contract](apps/release-contract.md) — what a release must contain
- [Release discovery and previews](apps/releases.md) — proof and isolation
- [Approval and installation](apps/installation.md) — the installer and
  recovery

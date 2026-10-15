# Publish an independent app

Copy `publish.yml` to `.github/workflows/publish.yml` in your app repository.
Copy `publish.py`, `package-workload.py`, `manifest.py`, and the complete
`vendor/` directory to `publishing/`.
Provide a Dockerfile, requirements.txt, tests, and a v1 manifest template named
`manifest.template.json`. Your tests must check the application's runtime
contract.

Put each declared workload's files in `workload/<workload id>/`.
`package-workload.py` parses the template with the pinned standalone checker and
writes each workload's `asset` ZIP to `workloads/`. It fails when an asset is
not a plain file name or a workload directory is missing or has no files.
Entries are stored uncompressed, sorted, and stamped with a fixed time, so
the same sources give the same bytes. The declared
`entrypoint` is executable. Every other file is read-only data. Symlinks are
refused.

The workflow runs tests on pull requests and pushes. The check job also
validates the manifest template with the pinned checker and packages
the workloads. Only a push to `main` publishes a multi-architecture image and
release. Every action is pinned to a full commit SHA. When you update an
action, replace the SHA with the commit of the release you reviewed. The
platform tests refuse a template action that is not pinned.

The publish job packages the workloads again. After the image build, the
publication script fills the source commit, image digest, and workload hashes.
It validates the final manifest with the same checker artifact used by preview.
The artifact contains the standard-library public release validator. It needs
no core checkout, runtime client, or additional package credential. The schema
JSON is available for editors. Update the vendored artifact, its hash pin,
`manifest.py`'s expected hash, and the exported schema together after review.
The platform tests run the shared conformance cases against that artifact.

Templates may use `${SOURCE_COMMIT}`, `${IMAGE}`, and `${SHA256}` in their
matching release fields. The template flag permits those unresolved values.
Resolved templates still use final validation, and final publication always
requires resolved values.

The Actions artifact and release contain identical files. `publication.json`
binds the workflow run and attempt to each manifest and workload hash. Its
`assets` map excludes `publication.json` to avoid a recursive hash. The core
downloads the artifact using its GitHub API identity and verifies GitHub's
artifact digest before comparing the release files.

Keep publication credentials inside the repository's CI scope. The workflow
needs no platform address, application credential, SSH key, or installer token.
For a private repository, give the platform a separate read credential for
repository releases and Actions artifacts. The preview guest uses its own
registry credential to pull private images.

# Independent application integration

The application contract separates app development from platform operation.
An app owns its repository, dependencies, tests, container image, and private
state. The core owns identity, job scheduling, shared file permissions,
application routing, and approved installation.

This section specifies contract version 1 for managed applications.
The specifications define the interface that implementations must satisfy;
deployment availability is recorded separately by each platform operator.

Use these documents for different tasks.

- [Application CI/CD](../ci-cd.md) gives a one-page overview of the whole
  pipeline, from a push to an approved deployment.
- [Runtime contract](runtime-contract.md) defines container configuration,
  request identity, routing, state, and platform capability boundaries.
- [Release contract](release-contract.md) defines what an app publishes and
  what the platform registers, verifies, previews, and approves.
- [Registry](registry.md) describes operator-owned registration, immutable
  release records, navigation, and optional monitoring.
- [Register a published application](onboarding.md) prepares a new app's
  registration, credential and host policy with one operator command.
- [Release discovery and previews](releases.md) describes workflow proof,
  retained assets, pinned attempts, and guest isolation.
- [Approval and installation](installation.md) describes exact approval,
  the bounded host helper, migration checks and current-data recovery.
- [Run a preview guest](preview-operator.md) describes synthetic guest setup
  and the operator commands for discovery, preview, and retirement.
- [App compute API](compute-api.md) defines installed workloads, scoped data
  inputs, idempotent runs, cancellation, and complete result downloads.
- [App API v1 HTTP reference](api-reference.md) gives HTTP examples and links
  to the generated [OpenAPI document](api-reference.json).
- [Build an independent app](build-an-app.md) describes the author's workflow
  without requiring a platform checkout.
- [Integration sandbox](sandbox.md) describes the test-only image that runs
  the real core and worker with synthetic accounts next to an app container.

Job Desk, Files, accounts, worker software, and the scheduler are core
functions. An app uses their supported HTTP interfaces rather than copying
their implementation or opening the core database. See
[Compute](../compute/README.md), [Storage](../storage/README.md), and
[Accounts and access control](../access-control.md).

Multiple pages of one app remain one application when they share state and a
lifecycle. Independent repositories do not require a container per page.

Applications are reviewed code operated by trusted people. A gateway limits
an app backend's credentials; applications sharing a browser origin do not
have isolation from malicious scripts on that origin.

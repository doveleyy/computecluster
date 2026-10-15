# Documentation

Home Platform organizes independent applications and shared compute. Start with
[Architecture](architecture.md) for the whole system and its ownership
boundaries.

## Understand the platform

| Page | Purpose |
|---|---|
| [Architecture](architecture.md) | Core, applications, workers, data ownership, and failure limits |
| [Network](network.md) | Private reachability, trusted ingress, and signed application context |
| [Accounts and access control](access-control.md) | Roles, sessions, identity linking, and ownership |
| [Compute](compute/README.md) | Durable jobs, leases, placement, and isolated execution |
| [Storage](storage/README.md) | Logical file areas, published results, and backups |

## Write applications and jobs

| Goal | Guide |
|---|---|
| Integrate an independent application | [Application integration](apps/README.md) |
| Build and test an application | [Build an independent app](apps/build-an-app.md) |
| Run a single Python script | [Python script job](compute/python-script.md) |
| Run a project or numeric job array | [Batch script job](compute/batch-script.md) |
| Prepare inputs and collect results | [Files in and out of a job](storage/workflow.md) |

## Look up a contract

| Reference | Contents |
|---|---|
| [Configuration](configuration.md) | Core and worker settings, defaults, and limits |
| [Job and API contract](compute/job-contract.md) | Job schemas, endpoints, and execution guarantees |
| [Application runtime](apps/runtime-contract.md) | Routing, identity, state, and platform capabilities |
| [Application releases](apps/release-contract.md) | Published manifests, images, and compatibility requirements |
| [Application HTTP API](apps/api-reference.md) | HTTP examples and the generated API specification |

Application registration, release discovery, previews, and approved installation
are indexed under [Application integration](apps/README.md). Deployment-specific
configuration and operational evidence are maintained outside this public tree.

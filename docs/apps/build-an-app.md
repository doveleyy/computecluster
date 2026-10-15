# Build an independent app

Build and publish an app from its own repository using managed-app contract
version 1. The platform operator registers the repository once and approves
production releases. You need the public contract and an app registration,
not the core source, its database, host access, or operator credentials.

This guide describes the author workflow. Use the operator's supported client
and local fixture when available, or implement the documented HTTP boundary.
Confirm contract support with the operator before connecting an installation.

## Create the app repository

1. Create a repository containing only your app's source, dependency lock,
   tests, Dockerfile, image-build workflow, and `app.yaml` source template.
2. Start from the
   [identity-only definition](../../examples/apps/identity/app.yaml).
   Choose a stable app ID and request additional capabilities only when needed.
3. Build entirely from this repository. Do not copy or import platform
   implementation modules, or require a sibling core checkout.
4. Implement `/health`, `/ready`, and `/version` at the declared container port.
   Keep liveness free of provider requests and shared-file access.
5. Read the supplied [runtime configuration](runtime-contract.md). Store app
   data beneath `APP_STATE_DIR` and generate browser URLs with `APP_BASE_PATH`.
6. Resolve the incoming user context through the core before accessing user
   records. Store the stable user ID, not a display name or network login.

**Check.** Build from a fresh app checkout. Health works without user identity;
user-data routes refuse missing context. Assets, redirects, and app cookies
work beneath a non-root URL prefix.

## Develop and test locally

1. Run your app with throwaway state, a development credential file, and the
   configured local platform-client fixture or the
   [integration sandbox](sandbox.md).
2. Keep any fictional identity adapter in the local/test profile. A managed
   deployment never selects that adapter or accepts caller-chosen users.
3. Test two fictional users. Each sees only their records; changing a display
   name leaves ownership unchanged. Test core unavailability and access denial.
4. Run your repository's checks and the operator's contract checker when
   supplied. Keep local fixture credentials out of the release definition.
5. Test that the previous image can use disposable state after the new image's
   database migrations. Use compatible changes for ordinary releases.

**Check.** Your tests do not require production accounts, files, services,
provider credentials, or any core implementation file.

## Add compute or file inputs when required

1. Request `jobs` for app-owned input uploads and fixed compute workloads.
   Use the [compute definition](../../examples/apps/csv-report/app.yaml).
2. Package the workload as a validated project archive with a fixed entrypoint
   and approved runtime. Give its release asset a SHA-256 digest.
3. Request `files` only to select permitted logical input files. Use returned
   handles; do not construct NAS paths or supply another user's owner ID.
4. Submit the installed workload with input handles and a stable submission
   key. Preserve that key when retrying a lost response.
5. Show queued, running, failed, cancelled, and completed states. Retrieve
   results through the app API and verify downloads against their manifest.

**Check.** Cross-user and cross-app references fail. A retry creates one run.
Unavailable storage produces a useful message. The app never needs a worker
token, arbitrary-code submission, or access to the scheduler database.

## Publish a candidate release

1. Ask the administrator to register your repository, publish branch, workflow,
   image repository, external route, access policy, and requested bindings.
2. Configure the workflow to run tests and contract checks on the publish
   branch, build supported images, and publish the final release assets.
3. Replace source-template placeholders with the actual commit and artifact
   digests. Publish `manifest.json` only after its image and workloads exist.
4. Push your change to the configured publish branch from your normal checkout.
   Keep host SSH and deployment credentials out of the workflow.
5. Check the workflow result and the platform's discovered candidate. Fix build
   or preview failures in your app repository and publish another release.

**Check.** The candidate identifies the exact commit and image. The platform
discovers it through outbound requests; your build does not call a private
home-server deployment endpoint.

## Preview and release

1. Open the preview with its fictional accounts and disposable data.
2. Verify the normal user path, routing, identity denial, and database behavior.
   Inspect app-specific scheduled or external-provider behavior with fixtures.
3. Ask the administrator to review the candidate and its effective permissions.
   Their approval selects the tested artifacts without rebuilding them.
4. Verify the installed app through its external route after deployment.
5. Make the next feature change only in the app repository and repeat the push.

**Check.** Ordinary releases require no new core router, hardcoded navigation
entry, host script, copied source, or shared secret exchange. If a missing
contract forces one of those workarounds, record the failed task for the
platform maintainer instead of adding a private dependency.

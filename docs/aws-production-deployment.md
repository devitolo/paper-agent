# AWS production release candidate

This production-only review series starts from main commit
`2f84763ce94dd762cd691b20f6cf894e4a84f9be`. It does not import the divergent AWS
preparation branch. It does not create the proposed `aws-production` release
branch, push that branch, install host components, dispatch a run, or deploy.

## Release and GitHub protection

After review, create the dedicated `aws-production` branch through the approved
release process and promote only the reviewed AWS commits to it. The workflow
checks `github.ref == 'refs/heads/aws-production'` on both jobs; runs from main,
preparation branches or pull requests cannot submit a deployment. No workflow
triggers on push or PR. An authorized operator must dispatch it manually with an
already reviewed image digest, source commit and source bundle SHA256.

Before allowing any run, configure these GitHub controls:

- Protect `aws-production` with PR review, required status checks and required
  independent approval; require resolved discussions and dismiss stale approvals.
  Restrict push/merge access to the release maintainers. Block force pushes and
  deletion. Do not permit routine administrator/automation bypass.
- Protect edits to this workflow, controller, service, host contract and probe code
  with the project's code-owner/release review policy. Configure the actual
  repository reviewers/teams; no team identity is guessed by this patch.
- Create protected environment `aws-production` with required release reviewers,
  prevention of self-approval, and deployment branches restricted to the exact
  `aws-production` branch. Restrict environment bypass where supported.
- Route production jobs only to the exact labels
  `[self-hosted, linux, x64, project-paper-aws-deploy]`. Labels are routing, not
  authorization. Restrict the runner group to this trusted repository/workflow
  and release branch using the account's supported policy. If those controls
  cannot be enforced, runner production use remains blocked.
- Require checks that execute the controller/probe suites and validate workflow
  syntax before approving a release. Required check names must match actual
  configured CI jobs; the change does not invent successful status names.

Workflow concurrency `project-paper-aws-production` serializes submissions and
never cancels an in-progress deployment. A host flock independently rejects a
second transaction. Hosted syntax validation is not release authorization: a
root-owned approval must bind image/revision/bundle, GitHub run ID/attempt,
reviewer, expiry, compatibility evidence, storage budget and one-time approval ID.

## Files and credential boundary

- `.github/workflows/aws-production.yml`: manual digest promotion. Short hosted
  validation job, then protected self-hosted submission and sanitized status.
  No checkout, downloads, builds, Docker commands or credentials on the runner.
- `scripts/aws_deploy.py`: standard-library, isolated-mode root-owned controller,
  intended installed path `/usr/local/sbin/project-paper-aws-deploy`.
- `deploy/aws/project-paper-aws-deploy.service`: independent transaction and
  `ExecStopPost` recovery. Boot/recovery activation is a separate operator task.
- `deploy/aws/host.example.json`: disabled, incomplete host contract. Null resource
  values and missing reviews are intentional fail-closed gates.
- `paper_agents/web.py`: signed read-only probe behavior only; normal main routes
  and data import/export semantics are preserved.

Runner sudo must be limited to the installed helper's `submit` and `status`
commands. Do not grant a Docker group/socket, arbitrary shell/Python/systemctl,
`run`/`recover`, or write access to helper/config/service/approvals. Helper,
configuration and parent directories must be root-owned, nonsymlink and not
writable by group/other. Installation is not automated here and must use reviewed
bytes independently of a job checkout.

| Location | Required item | Purpose |
| --- | --- | --- |
| Dispatch input | `image` | `ghcr.io/devitolo/paper-agent@sha256:<64 lowercase hex>` |
| Dispatch input | `revision` | Approved image's 40-character source commit |
| Dispatch input | `bundle` | Approved source archive SHA256, 64 lowercase hex |
| GitHub environment | `aws-production` | Reviewer and exact release-branch policy |
| Custom GitHub variables | None | Host identities and thresholds are private root-owned configuration |
| Custom GitHub secrets | None | No AWS, SSH, HMAC, Gemini or registry secret on deployment runner |
| Existing app environment | `PAPER_AGENT_SYNC_SHARED_SECRET` | Candidate's container-local HMAC verification |
| Existing app secret/config mounts | Preserve | Gemini/HMAC identity and configuration unchanged |
| Root-owned credential broker | `registry_auth_adapter` | Short-lived registry-read-only token, private stdin login |

The image must have been built for Linux AMD64 on a GitHub-hosted build runner,
with reviewed source revision/bundle and `cloud-production` runtime labels. This
series does not add or change Dockerfiles/build workflows. Current main lacks a
qualified `cloud-production` target; producing and accepting that release artifact
is an explicit prerequisite, not an implicit build on AWS or a reason to import
unrelated preparation changes. Candidate and retained rollback images must each
pass the embedded read-only copied-state validator using existing main schema
helpers. Missing helpers, unsupported schema, missing files or labels fail closed.

## Existing production state and host gates

Populate `/etc/project-paper/aws-deploy.json` from fresh SRE evidence, not these
docs or guessed paths. The required asserted identity is project
`project-paper-prod`, service `app`, container `project-paper-prod-app-1`. Verify
it on the actual host before approving the contract. Configure canonical Compose
paths and env files, exact mount dictionaries and protected file hashes. Only
existing external named volumes and explicitly allowlisted read-only secret binds
are supported. Missing volumes, mount drift, writable secrets or unexpected
Compose configuration block deployment; nothing creates replacement state.

A separate final image env file contains only
`PAPER_APP_IMAGE=ghcr.io/devitolo/paper-agent@sha256:<approved digest>` and is the
only selected-image configuration the helper changes. Existing DB/config,
feedback/profiles, summaries, sync identity, secrets, logs and backups are retained.
The old live image must already be retained by qualified immutable registry digest
and match the running image ID. A local-only image ID or tag is insufficient.
Bootstrap of a previously local-built release requires a separately reviewed plan.

The root-owned registry adapter returns a registry-read-only username/password
and an expiry no more than 15 minutes away. The controller uses temporary private
Docker auth, stdin login and cleanup/recovery; no token appears in workflow,
arguments, journals or public logs. The host config must record the adapter hash.

The small AWS host's envelope remains unproven. SRE must measure minimum available
RAM/free swap, maximum memory PSI, disk/inodes, swap trend and OOM lookback, pull/
unpack/storage budget and bounded validator/command/health limits. Controller
preflights run before pull, after pull, before replacement and before commit.
There is no package installation, image build, instance resize, automatic pruning,
or inferred resource threshold. Root controller/validator and dockerd's combined
resource demand must be qualified; runner cgroup limits alone do not limit Docker.

The all-writer fence is mandatory. An independently installed root-owned adapter
must implement bounded `acquire`, `status` and `release` for the transaction. Its
proof must show `fenced=true`, `durable=true`, `active_writers=0` and
`unknown_writers=false`; a file marker alone is not proof. It must block/drain UI,
sync and background writes and persist across runner loss, helper death and host
reboot while allowing read-only health probes. Release proof must match the same
transaction. Missing fence enforcement or ambiguous release leaves an incident.

## Transaction and rollback

1. Lock, verify host configuration/mounts, old retained image and existing health.
2. Resource-check, pull only the approved digest and verify AMD64/source/runtime
   labels. Recheck capacity. No image is built on AWS.
3. Persist fencing state, acquire/drain and verify all writers are excluded.
4. Back up current live data/config using SQLite backup, file hashes, integrity/FK
   checks and fsync in a unique private directory. No old snapshot is imported.
5. Validate candidate and previous images against the copied state on read-only
   mounts with no network, secret environment, capabilities or Docker socket.
   The embedded validator never migrates, seeds or restores a DB.
6. Recheck resources/fence/old health. Persist replacement phase and recreate only
   the app using `--no-deps --no-build --pull never`, retaining existing volumes.
7. Require Docker healthy, zero restarts/OOM, exact image, HTTP `/ready`, `/health`,
   `/runtime`, existing public page checks, local Gemini configuration, private
   TLS `/ready` and `/runtime`, and candidate signed sync probes below.
8. Back up fenced state again; require unchanged schema, logical DB rows and
   other files. Validate rollback compatibility again, commit image selection,
   release fence and report success.

Readiness/probe failure after replacement triggers the existing independent
service's image-only rollback over the same live volumes, followed by old health
verification and canonical image selection restoration. It reports failed release
even when rollback succeeds. Changed schema/data or failed/ambiguous rollback
leaves an incident and keeps writes fenced. No stale DB is restored over feedback.
Recovery uses durable journal phases, `ExecStopPost` and reviewed boot handling,
not merely a runner exit trap. SIGKILL/reboot, cancellation, disk failure and
concurrent submission require rehearsal before activation.

Actions sees only sanitized transaction/image/phase/errors. Private copied data,
credentials, UI bodies and journals must not be uploaded as CI artifacts. Approve
retention and encrypted independent backups before activation; the controller does
not delete old evidence automatically to make space.

## New read-only app behavior and limits

`GET /sync/input` with valid timestamped HMAC and header `X-Paper-Sync-Probe: 1`
returns only `{schema:1,status:"ready",route:"/sync/input",read_only:true}` and
bypasses snapshot export/revision creation. `POST /sync/pipeline-results` with that
header, body exactly `{}`, and matching HMAC returns the analogous metadata.
Unsigned probes return 401; malformed bodies/length/transfer encoding return 400.
POST without the probe header retains main's existing 404 behavior; this change
does not introduce a new data-writing alias or modify the normal results contract.

The controller verifies unsigned rejection and signed success inside the candidate
container, with no proxies/redirects, bounded reads/timeouts and no secret/body
logging. Previous images retain their already qualified public health contract.
A probe verifies routing/authentication, not the complete data-transfer contract.
Candidate source/API/schema compatibility and real sync roundtrip still need
separate acceptance evidence before any later sync activation.

The review series runs only synthetic controller failure injection and temporary
HTTP/SQLite tests. Actual host limits, installed service/fence, image/mounts,
credential availability, private TLS, runtime readiness and disaster recovery
remain qualification gates. No runner, host data, sync worker or cron is activated.

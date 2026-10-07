# Mini migration candidate: app + Ollama, existing host cron

This is the consolidated first-cutover recipe. It supersedes the earlier slice
plans where they say Gemini packaging or Compose is not implemented. Nothing in
this document has been executed against the Mini. The implementation is ready for
local review/testing; image construction and real runtime qualification remain
operational gates. The existing custom scheduler is preserved but parked: do not
select the `scheduler` profile or enable it for this candidate.

For the approved isolated build/save/load rehearsal only, use the explicit
[local config-ID identity route](mini-rehearsal-images.md). Production registry
digest validation remains unchanged. SDLC must refresh its source archive/hash
after these changes; do not use the previously prepared bundle.

## One configuration list

Populate a **private** copy of `deploy/mini-migration.env.example`:

- App image built with `migration-gemini`, then identified by its real registry
  digest; qualified immutable Ollama image. Neither digest has been fabricated.
- Fixed isolated Compose project, loopback app port, six separately provisioned
  external volumes (data, config, model, runtime-control, logs, backups), approved
  snapshot manifest path, verified Phoenix DNS on `paper-observability_default`.
- Gemini enabled=1 and telemetry=1 for native feature parity. Dedicated
  `PAPER_GEMINI_KEY_FILE` absolute path, containing only the API key, optionally
  one final newline, owned by UID10001 with mode0400 or0600. Provision privately;
  no key content goes into env, build args, source, logs or chat. Compose file
  secrets are read-only bind mounts: host ownership/mode must actually permit
  UID10001 access; Compose UID/mode remapping is not assumed.
- `PAPER_AGENT_GEMINI_DEFAULT_MODEL`: **unresolved operator/model-policy input**.
  No model is guessed. A nonempty approved CLI name/alias is required. The
  application first call retains `model=None`; this default is supplied only to
  the CLI child via `GEMINI_MODEL`. Existing explicit application overrides and
  quota fallback to `gemini-3.1-flash-lite` are unchanged.
- Existing Semantic Scholar API key provisioned in the private env file (existing
  source contract); keep `PAPER_AGENT_SCHEDULER_ENABLED=0`.

Missing Gemini runtime/key/model leaves imported browsing and raw/structured
feedback saving available with a fixed actionable error. It does not silently
start login or call a provider. Monday's host launcher checks local Gemini
readiness and fails if incomplete. Local readiness does **not** prove provider
access, model availability, or Linux/CPU compatibility.

## Image recipe and recorded inputs

For a subsequent explicitly approved build, from this candidate checkout:

```sh
docker build --target migration-gemini \
  --build-arg PYTHON_BASE_IMAGE="$APPROVED_PYTHON_BASE" \
  --build-arg NODE_BASE_IMAGE="$APPROVED_NODE_BASE" \
  --build-arg VCS_REF="$CANDIDATE_REVISION" \
  --build-arg VERSION="$CANDIDATE_VERSION" \
  --build-arg BUILD_DATE="$BUILD_DATE" \
  -t paper-mini-candidate .
```

Choose actual base digests during qualification. Reference defaults are
`python:3.12-slim` and `node:22.23.2-bookworm-slim`; the latter's existence and
platform/CPU behavior have not been confirmed by a build here. The build checks
Node22.23.2 and installed Gemini0.52.0 without running Gemini. It resolves the npm
lock, then uses `npm ci --ignore-scripts`; native optional dependency needs must
be checked in the image smoke test, not assumed. The image retains:

- `/app/image-runtime-inputs.json`: Python, Debian packages, source/telemetry tools.
- `/opt/paper-gemini/{package.json,package-lock.json,installed-dependencies.json}`.
- `/opt/paper-gemini/runtime.json`: actual Node/CLI entry hashes and versions,
  lock hash, supplied Node base reference, explicit unqualified status.

The first lock resolution can change across builds: this is **not** a fully
locked reproducible image. Export/review/retain the resolved lock and base/image
digests as qualification evidence; use that same lock in subsequent release
rebuilds before claiming repeatability. The default final Docker target stays
`runtime`, preserving existing fresh-install packaging.

## Isolated snapshot, import, recovery

1. Preserve native checkout/config/cron/service definitions and deployed revision.
   Pause native scheduled/manual/web writers and wait for in-flight processes.
   Record capture time. Keep the original state intact for rollback. No native
   credentials/home are part of this snapshot.
2. Create a separate snapshot containing `data/` and `config/`. Use SQLite backup
   into the snapshot for `data/paper_agent.db`, rather than copying an active DB.
   Include every referenced relative artifact and profile/topic file. Complete
   the backup and close its connection before manifesting. No WAL/SHM/journal
   sidecars, symlinks or external artifact paths are accepted. This step requires
   quiescence across DB and filesystem artifacts, not merely a consistent DB.
3. Against the isolated snapshot only, generate the approved manifest with this
   candidate's code (output outside `data/` and `config/`):

   ```sh
   python3 - "$SNAPSHOT_ROOT" "$MANIFEST_FILE" <<'PY'
   import json, sys
   from pathlib import Path
   from paper_agents.import_state import manifest
   value = manifest(Path(sys.argv[1]))
   with Path(sys.argv[2]).open('x') as output:
       json.dump(value, output, indent=2)
       output.write('\n')
   PY
   ```

   The schema comes from this candidate's trusted `sql/schema.sql`, resolved
   relative to the installed code; the snapshot needs only data/config, and a
   snapshot-supplied schema is never trusted.

   Exact schema compatibility, every table's logical rows, file hashes, profile,
   topic parsing and artifact paths must pass. Do not regenerate a manifest from
   a damaged destination to bypass a failure. Preserve the approved snapshot.
4. Provision distinct **empty** external volumes. Copy the snapshot's data/config
   into their corresponding volumes using the already built image as a helper,
   with the snapshot mounted read-only; make copied data/config and empty
   control/log/backup volumes writable by UID10001. Check the destination is
   empty before copying. Copy the inventoried Ollama store into its separate model
   volume while native model writers are stopped. Do not reuse native writable
   mounts or import the native Gemini credential store. Volume creation, copying,
   ownership adjustment and real-state verification remain SDLC operations.
5. Set `PAPER_MIGRATION_ENV_FILE` to the absolute private env-file path. Resolve
   configuration first (avoid printing the secret-bearing expanded config):

   ```sh
   docker compose --env-file "$PAPER_MIGRATION_ENV_FILE" -f docker-compose.mini-migration.yml config --quiet
   docker compose --env-file "$PAPER_MIGRATION_ENV_FILE" -f docker-compose.mini-migration.yml up -d app ollama
   docker compose --env-file "$PAPER_MIGRATION_ENV_FILE" -f docker-compose.mini-migration.yml exec -T app python -m paper_agents.package_runtime check-app
   docker compose --env-file "$PAPER_MIGRATION_ENV_FILE" -f docker-compose.mini-migration.yml exec -T app python -m paper_agents.package_runtime check-gemini
   docker compose --env-file "$PAPER_MIGRATION_ENV_FILE" -f docker-compose.mini-migration.yml --profile model-verification run --rm prepare-model
   ```

   These are future operation commands, not actions performed in development.
   The last command lists model identity only: no pull or inference. App startup
   imports before serving, without model-service dependencies or provider calls.
   Gemini readiness reads only the dedicated mounted file and packaged inventory.
6. Rehearse saved browsing, exact feedback preservation and restart against the
   copied state. Only under separate live-call authorization validate bounded
   Gemini response/fallback, Ollama inference on the Mini CPU, source requests,
   and actual Phoenix delivery. Do not switch cron before these pass.
7. Rollback before accepting new writes: stop candidate containers/jobs, preserve
   candidate evidence, restart the untouched native state/services/cron. After
   new writes: stop all writers, snapshot the full candidate data/config/artifacts
   consistently, verify schema and identities, and explicitly reverse-transfer
   before restarting native. Never simply switch back to stale native DB or mix
   artifacts from different snapshots. Rehearse this reverse transfer before
   production cutover. Do not use the default fresh-Compose package backup helper
   for these external migration volumes; never bypass its in-use-volume guard.

## Existing host cron remains the scheduler

Replace execution commands only after qualification, preserving the approved host
schedule/timezone (America/Los_Angeles) and existing log destinations:

| Existing time | Invocation from candidate checkout |
| --- | --- |
| Daily04:00 | `bash scripts/mini_container_job.sh openalex` |
| Daily05:00 | `bash scripts/mini_container_job.sh arxiv` |
| Daily06:00 | `bash scripts/mini_container_job.sh semantic` |
| Monday02:00 | `bash scripts/mini_container_job.sh profile` |
| Sunday01:00 | `bash scripts/mini_container_job.sh backup` |

Export only the private env-file **path** to cron. The launcher passes the file to
Compose without shell-sourcing it, uses `exec -T app` to enter
`paper_agents.migration_job`, then fixed known wrappers,
shared container lock directory, slot0, existing biweekly anchor2026-09-07,
self-update disabled, and explicit backup paths. Existing wrapper/scout locks
remain authoritative. Each job has its own shared runtime lease and independent
Linux watcher, from before provider preflight through wrapper completion and
descendant cleanup. Parent loss closes a liveness pipe; the watcher remains
responsible for cleanup and initialization exclusion. The default job budget is
1800seconds (maximum7200). An exclusive initialization lease prevents any job
launch. Monday is still a dry-run profile comparison. Native jobs
must be paused before container cron is enabled, and the parked scheduler must
remain disabled to prevent two scheduling authorities. Container stdout/stderr
flow to the existing host cron log redirection; persistent application logs and
backups use their explicit volumes.

## Gemini isolation and bounded cleanup

The private key reader rejects missing, symlinked, multiply linked, nonregular,
wrong-owner, permissive, empty, non-ASCII or >4096-byte files. A guardian creates a
new private HOME/CWD/settings directory per call. The child receives only an
allowlisted environment, `gemini-api-key` selected auth and the approved default;
no native HOME, settings, workspace context, MCP/extensions or host auth overrides
are copied. The actual installed entry point is resolved from package metadata at
build time. Application prompts and streaming/error handling stay unchanged.

Migration supervision now requires Linux subreaper and `/proc` support; it fails
before CLI launch on other platforms. Native Gemini handling is unchanged. The
guardian has a parent-liveness pipe and its own execution deadline, launches the
CLI in a separate process group, then performs TERM,250ms grace,KILL. As the
Linux subreaper, it also kills and reaps adopted children irrespective of session
or process-group changes, repeating as further descendants are adopted.

The runtime lease and private HOME stay owned until there are no remaining
children. If cleanup exceeds its normal bound or process inspection fails, the
guardian reports pending cleanup and retains ownership instead of exiting. The
caller waits at most3seconds for cleanup, then fails without SIGKILLing the
ownership guardian. This intentionally keeps initialization blocked if the kernel
has not completed cleanup. The same supervisor is reused by host-cron job
watchers, so job ownership covers writes outside individual Gemini calls.

The process-tree behavior has Linux-only synthetic regressions, including a
session-detaching child, timeout, successful completion, caller death and job
initialization exclusion. **Those Linux regressions have not been executed on
this Mac.** They must pass in SDLC's Linux rehearsal before this cleanup guarantee
is qualified. The parked scheduler remains unused for first cutover.

Primary implementation references: [pinned CLI configuration](https://github.com/google-gemini/gemini-cli/blob/v0.52.0/packages/cli/src/config/config.ts),
[pinned auth precedence](https://github.com/google-gemini/gemini-cli/blob/v0.52.0/packages/core/src/core/contentGenerator.ts),
[pinned CLI package metadata](https://github.com/google-gemini/gemini-cli/blob/v0.52.0/packages/cli/package.json).

## Local verification evidence

Before the consolidated corrections, the local synthetic suite reported 397
tests with 17 skipped, and a narrower Gemini/host-cron run passed 13 tests.
Those results do not validate the corrected import, detached-child cleanup or
independent cron-job lease contracts. The current focused Mac run completed 74
tests in 35.172 seconds: 69 passed and five Linux-only process tests skipped.
Shell syntax and diff whitespace checks passed. No image build, provider call,
native key access, Mini change or copied-state rehearsal was performed.

Linux reaping reference: [PR_SET_CHILD_SUBREAPER](https://man7.org/linux/man-pages/man2/PR_SET_CHILD_SUBREAPER.2const.html).

## Mini rehearsal evidence

Subsequent isolated Mini rehearsals were executed against copied state and local
save/load image identities, not production cutover state:

- `/home/devitolo/paper-mini-rehearsal/qwen-smoke-20260922-181445.log`:
  rehearsal Ollama started from the copied model volume, listed
  `qwen2.5:1.5b-instruct`, completed one bounded CPU inference on the AVX-only
  Mini, then stopped. Native `ollama.service` and `project-paper-web.service`
  were active before and after.
- `/home/devitolo/paper-mini-rehearsal/app-qwen-smoke-20260922-184021.log`:
  rehearsal `app` and `ollama` services started together, the app container
  reached `http://ollama:11434/api/generate`, received `app qwen ok`, served the
  Paper UI on loopback port18080, then stopped. Native services remained active.
- `/home/devitolo/paper-mini-rehearsal/provider-telemetry-smoke-20260924-220642.log`:
  Phoenix telemetry sent a synthetic trace and Phoenix remained reachable on
  loopback6006. The early combined smoke script had a false PASS after a Gemini
  launcher failure; use the later Gemini-specific log below for provider
  qualification.
- `/home/devitolo/paper-mini-rehearsal/gemini-provider-rawkey-trustfix-20260924-223959.log`:
  after adding explicit Gemini CLI headless workspace trust and rebuilding the
  rehearsal image, the container used the dedicated raw AI Studio key file and
  received `gemini rehearsal ok` from a bounded one-shot provider call.
- `/home/devitolo/paper-mini-rehearsal/fresh-restore-20260921-201800.log` and
  `/home/devitolo/paper-mini-rehearsal/native-compatibility-20260921-202133.log`:
  fresh isolated volumes restored the synthetic recovery copy, and deployed
  native code rendered that restored queue and saved local feedback against an
  isolated copy.

The valid Compose service name is `app`; there is no `paper-agents` service in
the migration package. Remaining production gates are final quiescent production
snapshot/final volumes, production restart policy, real source discovery and
host-cron cutover. No source discovery or production switch is qualified by
these logs.

### Consolidated QA correction status

The correction batch uses the candidate's trusted schema rather than a
snapshot-supplied file, adds adopted-child killing with lease retention, and
routes host cron through a per-job watcher. The five skipped Linux cases must
run in SDLC's Linux rehearsal before the descendant-cleanup and lease guarantees
are qualified. Image/CPU, mounted-secret authentication, selected model, real
copied-state import/restart/backup, and after-write reversal remain separate
qualification gates. The exact focused command and Linux follow-up are in the
QA correction handback; no full-suite rerun after this batch is claimed.

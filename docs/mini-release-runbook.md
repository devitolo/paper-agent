# Mini release runbook

This runbook is the shared release path for the operator Mini. It replaces ad
hoc `git pull` or production rebuilds.

Do not deploy application code by `git pull`.

For the automated GitHub Actions path with a self-hosted Mini deployment runner,
use [Mini self-hosted CI/CD](mini-self-hosted-cicd.md). The manual commands in
this file remain the fallback procedure.

## Policy

- Deploy application changes by building a tested `mini-production` image from a
  committed source revision.
- Production updates pull an immutable `image@sha256` reference and recreate the
  existing Compose `app` service only.
- Keep the existing Compose project identity, persistent volumes, imported
  database/papers/config, Gemini secret file, Phoenix network, host crontab, and
  paused afternoon jobs.
- Do not recreate Ollama unless the release explicitly changes the Ollama image.
- Do not run provider/live discovery calls as part of image publication. Validate
  provider behavior in bounded release acceptance or the approved production
  update window.
- Schema/state changes require a compatible rollback plan. An old image alone is
  not a rollback if the new code mutates database or on-disk state.

## Prerequisites

The operator must already have:

- Git push access to `devitolo/paper-agent`.
- Permission to run GitHub Actions in the repository.
- GHCR package write permission for candidate/release image publication.
- Docker and Compose access on the Mini for the production update.
- Access to the existing Mini production files under
  `/home/devitolo/paper-mini-rehearsal`.

Do not store registry credentials, Gemini keys, or source-provider keys in git.

## Build and publish a candidate

From a clean reviewed branch:

```sh
git status --short
python3 -m unittest tests.test_migration_foundations tests.test_migration_gemini \
  tests.test_migration_scheduler tests.test_mini_container_job \
  tests.test_rehearsal_images tests.test_import_legacy_schema -v
git push origin HEAD
```

Trigger the Docker workflow and publish a candidate:

```sh
gh workflow run Docker \
  --ref "$(git branch --show-current)" \
  -f publish_candidate=true
```

Watch it finish:

```sh
gh run list --workflow Docker --branch "$(git branch --show-current)" --limit 5
gh run watch RUN_ID --exit-status
```

Get the immutable image reference from the workflow summary or metadata
artifact. The shape must be:

```text
ghcr.io/devitolo/paper-agent@sha256:<digest>
```

The Docker workflow validates the public package target, validates the Mini
production target, publishes the `mini-production` target only when explicitly
requested or on a release tag, and runs CI exact-image acceptance against the
published digest.

Candidate publication is not production approval.

## Optional local exact-image check

After publication, any team role with registry read access can run:

```sh
PAPER_MINI_ACCEPTANCE_IMAGE='ghcr.io/devitolo/paper-agent@sha256:<digest>' \
PAPER_MINI_ACCEPTANCE_VERSION='mini-candidate-<sha>' \
bash scripts/mini_release_acceptance.sh
```

This checks the exact image digest, Mini runtime label, source labels, imports,
OpenTelemetry runtime packages, Node runtime, and bundled Gemini CLI dependency.
It does not call Gemini, Ollama, OpenAlex, arXiv, Semantic Scholar, or Phoenix.

## First production update command batch

Run this on the Mini only after the candidate image has passed exact-image
acceptance and the release owner approves the production attempt.

```sh
cd ~/paper-mini-rehearsal/candidate

export PAPER_MINI_APP_IMAGE='ghcr.io/devitolo/paper-agent@sha256:<tested-digest>'
export PAPER_MINI_FINAL_ROOT='/home/devitolo/paper-mini-rehearsal/production-cutover/final-20260924-230636'
export PAPER_MINI_PRODUCTION_OVERLAY='/home/devitolo/paper-mini-rehearsal/production-cutover/docker-compose.production.yml'

bash scripts/mini_production_update.sh
```

The helper:

- records current container, crontab, env, and image evidence under
  `~/paper-mini-rehearsal/production-releases/<timestamp>`;
- pulls the exact image digest;
- verifies the image is the `mini-production` target from this repository;
- updates only `PAPER_MIGRATION_APP_IMAGE` in the production env file;
- removes local-rehearsal identity keys from the forward production env;
- updates the managed cron overlay list so future jobs use registry identity
  mode instead of the old local-rehearsal overlay;
- runs a database backup inside the existing app container;
- recreates only the `app` service with Compose;
- checks app readiness, UI reachability, and Qwen inference;
- writes a rollback script in the release evidence directory.

Keep the terminal output and evidence directory path with the release notes.

## Manual rollback shape

If the update fails before readiness, inspect the release evidence directory
printed by the helper. It includes:

- `production.env.before`
- `before-ps.txt`
- `before-app-inspect.json`
- `pre-update-db-backup.log`
- `rollback.sh`

For compatible rollback, run:

```sh
bash ~/paper-mini-rehearsal/production-releases/<timestamp>/rollback.sh
```

Before using rollback after a schema/state-changing release, confirm the release
notes say old image plus current state is compatible. If not, restore using the
documented database/state backup path for that release instead of only
recreating the old app image.

## Production sanity checks

After update:

```sh
docker ps --filter name=paper-mini-production
crontab -l
curl --fail --silent --show-error http://127.0.0.1:8000/ >/dev/null
systemctl --user is-active project-paper-web.service || true
systemctl is-active ollama.service || true
```

Expected native service result is `inactive` for both services.

Then verify the changed release behavior within scope. For an OpenAlex release,
use the bounded OpenAlex acceptance documented in that release package before
declaring production success.

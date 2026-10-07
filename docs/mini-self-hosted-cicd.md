# Mini self-hosted CI/CD

This document describes the small GitHub Actions deployment path for Project
Paper on the production Mac mini.

## Confirmed production shape

The Mini production host inspected on 2026-09-25 is:

- Host OS: Ubuntu Linux on `x86_64` / `amd64`.
- Docker Engine: Linux `amd64`.
- Production Compose project: `paper-mini-production`.
- App service: `paper-mini-production-app-1`, loopback port
  `127.0.0.1:8000->8000/tcp`.
- Ollama service: `paper-mini-production-ollama-1`.
- Phoenix remains a separate observability container on loopback port `6006`.
- Scheduler: host crontab, not an in-container scheduler.
- Persistent state: external Compose volumes for data, config, logs, backups,
  runtime control, and Ollama model data.
- Secrets: existing private files referenced from
  `/home/devitolo/paper-mini-rehearsal/production-cutover/final-20260924-230636/production.env`.

The deployment branch is `mini-production`. Pull requests to that branch run
tests only. Pushes to that branch build and publish a `linux/amd64`
`mini-production` image, then deploy that exact immutable digest on the
self-hosted Mini runner.

## SSH access policy

Treat the Mini like a production environment. Do not SSH into it for routine
development, release verification, or normal deployment. The default production
interface is GitHub Actions, the `mini-production` branch, committed runbooks,
workflow logs, and deployment evidence under
`/home/devitolo/paper-mini-rehearsal/production-releases`.

Use SSH only when there is a bounded production reason that the workflow cannot
handle:

- incident response or service recovery;
- one-time runner, Docker, cron, or host maintenance;
- read-only evidence collection that is not available from Actions logs;
- an approved manual fallback deploy or rollback;
- explicitly requested operator diagnostics.

SSH requires explicit operator approval before connecting. Approval can cover a
bounded set of work, such as troubleshooting one production problem or carrying
out one approved maintenance procedure, but it expires when that work ends. A
new problem, a new maintenance task, or a move from read-only inspection to
mutation requires fresh approval.

Before requesting SSH approval, state the purpose, expected commands or
evidence, whether the session is read-only or mutating, and the condition that
ends the approval window. Keep the session short, avoid exploratory changes, and
record any command that changes production state in the release or incident
notes. Do not use SSH to bypass the `mini-production` branch, build images on
the Mini, edit production files ad hoc, run live discovery experiments, or
debug by poking at the host when repository tests, exact-image acceptance, or
workflow evidence are sufficient.

Use SSH requests as feedback about missing production visibility. When a request
is for recurring observability, health, schedule, deployment, model, telemetry,
or source-ingestion evidence, record the need and prefer adding it to the health
dashboard, workflow summaries, or committed runbooks instead of making SSH the
normal inspection path.

## One-time GitHub setup

Create the deployment branch once:

```sh
git fetch origin
git switch -c mini-production origin/main
git push -u origin mini-production
```

In GitHub repository settings:

1. Actions > General:
   - Allow GitHub Actions for the repository.
   - Keep `GITHUB_TOKEN` permissions least-privilege by default; workflows set
     explicit `contents` and `packages` permissions.
2. Environments:
   - Create environment `mini-production`.
   - Optional but recommended: require manual approval for this environment.
     That keeps the image build automatic while making production replacement a
     deliberate approval.
3. Branch protections:
   - Protect `mini-production`.
   - Require pull request review and required checks before merge.
   - Do not allow untrusted PR code to run on self-hosted runners.
4. Packages:
   - Use GitHub Container Registry package `ghcr.io/devitolo/paper-agent`.
   - The workflow publishes with repository `GITHUB_TOKEN`; no registry secret
     is stored in git.

## Install the self-hosted runner on the Mini

Use GitHub UI to create the runner:

1. Repository Settings > Actions > Runners > New self-hosted runner.
2. Choose Linux x64.
3. Copy the current GitHub-provided download/config commands.
4. Add these labels when configuring the runner:

```text
project-paper-mini
linux
x64
```

Install under the Mini user that already owns Project Paper Docker access:

```sh
mkdir -p ~/actions-runner/project-paper
cd ~/actions-runner/project-paper

# Run the GitHub-provided download/extract commands here.

./config.sh \
  --url https://github.com/devitolo/paper-agent \
  --token <runner-registration-token-from-github> \
  --name clarkmini-project-paper \
  --labels project-paper-mini,linux,x64 \
  --work _work \
  --replace

sudo ./svc.sh install
sudo ./svc.sh start
sudo ./svc.sh status
```

Do not run the runner inside the Project Paper application container. It is a
separate host service so it survives app container replacement and starts after
reboot.

## Production deployment flow

1. Open a pull request targeting `mini-production`.
2. GitHub-hosted runners run tests only.
3. Merge to `mini-production`.
4. GitHub-hosted runner builds and publishes the Mini image for `linux/amd64`.
5. The deploy job runs on the self-hosted runner labeled
   `project-paper-mini`.
6. The deploy job:
   - logs in to GHCR using the job token;
   - pulls the exact `image@sha256` produced by the build job;
   - skips if that digest is already deployed;
   - takes the deployment lock so new cron jobs skip while deployment runs;
   - waits up to `PAPER_MINI_DRAIN_TIMEOUT` for active jobs to finish;
   - writes rollback evidence before mutating production config;
   - backs up SQLite;
   - recreates only the app service;
   - checks app readiness, UI reachability, and Qwen readiness;
   - writes post-deploy verification for revision, container health, and the OpenAlex cursor flag to the workflow logs and release evidence;
   - restores the previous env, crontab, and app container if deployment
     validation fails after production mutation begins;
   - reports the evidence and rollback path in GitHub Actions.

Overlapping deployments are serialized by GitHub Actions concurrency and a host
deployment lock. Queued stale deployments check the current remote branch head
before mutating production and exit without replacing a newer deployment.
Deployments are not canceled halfway through container replacement.

## Scheduled jobs

Cron jobs cooperate with deployment through
`PAPER_MIGRATION_DEPLOY_LOCK_FILE`. During deployment, new scheduled jobs print
`Skipping Project Paper job: deployment is in progress.` and exit successfully.

Active jobs run inside the app container under the existing runtime lifecycle
lease. Deployment waits for that lease to become available. If the drain timeout
expires, no app replacement is attempted and the deployment job fails.

## SQLite and migrations

The deployment script creates a SQLite backup before replacing the app
container. If a deployment fails after production mutation begins, the script
automatically restores the captured env and crontab. If app replacement has
started, it also recreates the previous app image and checks readiness before
exiting failed.

For schema-changing releases, the release notes must explicitly state whether
the previous image is compatible with the post-migration database. Automatic
rollback is image-only and should be used only when the database/state remains
compatible.

If compatibility is unknown, stop at failure evidence and restore from the
recorded backup path with an explicit recovery procedure.

## Inspect status

GitHub Actions:

```sh
# In the GitHub UI:
# Actions > Mini Production
```

Mini host:

```sh
sudo ~/actions-runner/project-paper/svc.sh status
journalctl -u actions.runner.devitolo-paper-agent.clarkmini-project-paper.service -n 100 --no-pager

docker ps --filter name=paper-mini-production
crontab -l
curl --fail --silent --show-error http://127.0.0.1:8000/ >/dev/null
```

Deployment evidence is written under:

```text
/home/devitolo/paper-mini-rehearsal/production-releases/<timestamp>
```

## Pause deployments

Use any one of these:

- Disable the `Mini Production` workflow in GitHub Actions.
- Pause the `mini-production` environment approval.
- Stop the runner service:

```sh
cd ~/actions-runner/project-paper
sudo ./svc.sh stop
```

Stopping the runner does not stop the Project Paper containers.

## Manual deploy

Manual deploy still uses the same script and exact digest:

```sh
cd ~/paper-mini-rehearsal/candidate

export PAPER_MINI_APP_IMAGE='ghcr.io/devitolo/paper-agent@sha256:<accepted-digest>'
export PAPER_MINI_FINAL_ROOT='/home/devitolo/paper-mini-rehearsal/production-cutover/final-20260924-230636'
export PAPER_MINI_PRODUCTION_OVERLAY='/home/devitolo/paper-mini-rehearsal/production-cutover/docker-compose.production.yml'

bash scripts/mini_production_update.sh
```

## Rollback

Use the rollback script from the failed or last successful deployment evidence:

```sh
bash /home/devitolo/paper-mini-rehearsal/production-releases/<timestamp>/rollback.sh
```

Rollback restores the previous production env and crontab captured before the
deployment, then recreates the app service and checks readiness.

## Mini offline behavior

If the Mini or runner is offline, GitHub-hosted tests/build/publish can still
succeed, but the deploy job waits queued for a self-hosted runner. No production
change occurs while the Mini is offline. When the runner comes back, stale queued
deployments compare their commit SHA to the current `origin/mini-production` and
skip if a newer deployment branch head exists.

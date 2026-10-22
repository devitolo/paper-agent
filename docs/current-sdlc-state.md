# Current SDLC state

Status reconciled after the Mini non-production, Mini production, and AWS
production release work was consolidated into `main` at commit `f082112`.
This page is the compact operational summary; detailed procedures remain in the
linked runbooks.

## Repository and release lines

- `main` is the consolidated, external-facing code line.
- `mini-production` deploys the Mac Mini production runtime.
- `mini-nonprod` preserves the isolated Mini validation line.
- `aws-production` deploys the AWS serving runtime.
- `release/0.1.x` is retained for the older maintenance line.

Temporary `codex/*` branches and their attached worktrees are historical
development artifacts and are not deployment lines. Obsolete branches were
removed after review; new work should merge into `main` before an explicit
promotion to a release branch.

## Runtime roles

The Mini owns discovery, curation, local model execution, and the authoritative
production pipeline. Its production app runs in pipeline-only mode with no web
listener or published port; host cron remains authoritative. Qwen remains in
the Ollama service. The staggered production schedule is:

| Job | Schedule |
| --- | --- |
| Profile rebuild | Monday 02:00 |
| CORE source pipeline | Daily 03:00 |
| OpenAlex | Daily 04:00 |
| arXiv | Daily 05:00 |
| Semantic Scholar | Daily 06:00 |
| Database backup | Sunday 01:00 |
| ZenML | Sunday 23:30 |

The managed template and installed schedule were verified with commit
`e941e31`. Each source runs serially through the existing lifecycle and job
leases. A source can produce zero recommendations for different reasons:
provider failure or HTTP 429, candidates fetched but rejected by freshness or
quality filters, or no final recommendations. Health and logs should preserve
those distinctions and track consecutive 429 streaks and source trends.

Recent verification observed OpenAlex 1 recommendation, arXiv 3, Semantic
Scholar 0 after HTTP 429, and CORE candidates fetched but no final cards. The
Semantic Scholar key is configured, but shared/provider quota remains the
remaining cause to confirm for its repeated 429 pattern.

AWS serves the production UI and receives synchronized pipeline results. The
authenticated `/sync/pipeline-results` route and source-by-source exchange
path are implemented and synthetic Mini-to-AWS exchange validation succeeded,
including current-input revision checks, batch application, and receipt
creation. Mini and AWS retain separate production data/configuration roles;
feedback, topics, profiles, and secrets are not silently copied by the result
exchange.

## Mini non-production

Mini non-production is isolated with separate containers, database, config,
logs, generated artifacts, and lifecycle state. The UI is visibly labeled
`MINI NON-PROD -- DATA COPY`. At qualification time its copied database held
1,523 papers and 321 recommendations; the UI displayed 263 recommendations.
Non-prod scheduling, sync, Gemini, telemetry, and MiniLM remain disabled.
Production is not modified by a non-prod refresh.

## AWS delivery and safety rules

AWS delivery uses hosted AMD64 builds, immutable image digests, protected
environment approval, serialized deployment, backup/readiness/rollback
safeguards, and the existing simple Compose service. The AWS UI is tailnet-only
at `https://paper-prod.tail5fc461.ts.net`. The controller/orchestration path is
paused; do not add a general-purpose runner or unnecessary orchestration to the
small host. The host-install rehearsal is inert and registry/fence adapters are
deferred.

CI/CD gates must complete in order: validate, publish, then deploy. Do not build
ARM unless explicitly requested. Pass the immutable published digest directly
to deployment, avoid duplicate triggers and parallel duplicate runs, and track
the non-blocking GitHub Actions Node.js 20 deprecation warning for a future
action upgrade.

## Operator boundary

Do not change production cron, SSH hosts, AWS configuration, or deployment
targets without the applicable approval. SSH inspection and SSH-based changes
require explicit user permission. Normal repository work, commits, pushes, and
approved CI/CD flows can proceed without SSH. For every production release,
retain the exact image digest, source revision, deployment receipt,
health/readiness result, and rollback state.

MiniLM shared-service qualification was removed during final branch cleanup and
is not a current production requirement. The code still contains the explicit
HTTP target for future qualification, but production documentation must not
claim a separate MiniLM service is active unless a future release restores and
qualifies that design.

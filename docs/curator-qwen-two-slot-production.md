# Curator Qwen two-slot production tuning

Status: deployed to `mini-production` in commit
`4ec18b4e68b4c134665be1d5e9076a1d2c922fc7`. The successful release workflow is
[40598984740](https://github.com/devitolo/paper-agent/actions/runs/40598984740),
and the deployed app image is
`ghcr.io/devitolo/paper-agent@sha256:be279a8c466c6a9e8b8d84feff4ff6b928358198f316f8fc8bcd826cad87ae85`.

## Current contract

When Curator evidence review is enabled in production, up to two evidence
assessments run concurrently in Ollama. Only inference runs in worker threads;
SQLite reads, writes, ranking, and final result assembly remain on the owning
thread. Curator waits for both workers before unloading the model. The existing
prompt, schema normalization, correction attempt, interest/eligibility gates,
and maximum ten evidence candidates are unchanged.

Each assessment allows 512 output tokens for its initial response and its one
correction attempt. The per-assessment timeout is 900 seconds. The whole source
job still has its existing 1,800-second outer timeout; the 900-second setting
does not extend that limit or guarantee that all ten slow assessments finish.

Ollama is qualified for two actual inference slots on the Intel i7 CPU-only Mini
within the deployed 6-CPU, 5-GiB, no-container-swap, 128-PID bounds. This is
feasibility evidence for the selected two-worker setting, not a broad throughput
or semantic-quality guarantee. No GPU is assumed.

## Evidence and limits

The pre-deployment comparison found that the older 220-token cap could leave
both the initial answer and correction incomplete JSON even after increasing
review deadlines. With 512 tokens, two concurrent reviews completed valid on
the first try in approximately 237 and 264 seconds, using 206 and 290 tokens.
A separate three-slot pilot completed three of three first-try reviews in about
404, 433, and 463 seconds, using 167, 213, and 295 tokens. Two slots were chosen
because the small pilot observed about 0.454 valid reviews/minute versus 0.389
for three slots.

These are small one-batch CPU feasibility measurements, not a promise of
recommendation quality, sustained throughput, or transient-spike capture.
The production rollout did not rerun a full discovery cycle or repeat the
benchmark live after deployment.

## Rollback and schedule

The rollout preserved persistent mounts, credentials, and the existing schedule.
The updater records the prior Compose configuration and model configuration as
rollback material. The scheduled source order in the versioned template is:

| Job | Schedule |
| --- | --- |
| Semantic Scholar | 03:00 Pacific |
| OpenAlex | 04:00 Pacific |
| arXiv | 05:00 Pacific |
| CORE | 06:00 Pacific |

Other managed schedules remain unchanged. Use the deployment evidence and
rollback receipt rather than reconstructing runtime settings from this page.

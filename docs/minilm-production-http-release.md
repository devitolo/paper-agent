# Standalone MiniLM production app target

## Released Mini state

The standalone MiniLM production target was released from `main` commit
`5970bcdbb6bde52c852df57fc32b41fd92a1a4cf` through the
[qualified main release workflow](https://github.com/devitolo/paper-agent/actions/runs/39507617042).
The deployed app image is
`ghcr.io/devitolo/paper-agent@sha256:bbbe83615264947497b9dea0a5099e1c98cec07710c3fb083dc64347134d6204`;
the separate MiniLM service image is
`ghcr.io/devitolo/paper-agent-minilm@sha256:6015647271e23c9e667c8f7c50440d2330a36ef780578990b4c4f932fd08ff1b`.

The app now uses the explicit `mini-production-http` Docker target and calls a
private MiniLM service over the internal Docker network. The app image no longer
contains MiniLM inference dependencies or mounts the model; the service loads the
verified model once from its read-only mount. Qwen/Ollama remains a separate,
unchanged service. Schema, import/restore behavior, Gemini behavior, production
cron, and authoritative production data/configuration remain unchanged.

Release qualification compared the retained embedded scorer and the HTTP service
on real production model assets: all nine valid Curator pairs, ordering, maximum
winner, invalid-input handling, and two legacy raw pairs matched with maximum
score delta `0`. This is functional transport parity, not a load-capacity or
recommendation-quality claim.

A separate non-production app deployment is running from source revision
`246483b33d7e4cbc285b592f6239097004e7ce7b` after Actions run `39509451567`.
It is intentionally untested, its automatic health check is disabled, and its
browser ingress is unresolved; it is not evidence for this production release.
The retained non-production MiniLM service remains stopped.

## Release contract

Select Docker target `mini-production-http` explicitly. It inherits the same
migration/Gemini runtime as the embedded target but does not install MiniLM
inference requirements. Other targets keep their defaults. The new target sets
PAPER_AGENT_MINILM_BACKEND=http; SDLC supplies the exact internal service URL.
At start, before initialize/import touches state, HTTP readiness must match the
pinned API/model identity. `check-minilm` performs real synthetic Curator scoring.
The existing `check-model` remains Qwen verification and is not repurposed.

Caller audit: Curator/pipeline/release-check use default_interest_scorer. Evaluation
and shadow use default_eval_scorer; embedded remains default in legacy images,
while explicit HTTP routes to /v1/score-raw. This endpoint preserves their historical
raw whitespace/short-abstract input and fixed INTERESTS max semantics. It requires
nonempty strings and uses the same pinned score_pair without altering tokenizer
truncation. /v1/score retains Curator valid-abstract handling. Both retain request,
batch, concurrency and response limits. Production eval/shadow cron is currently
0 and remains unchanged; separate legacy experiment image wrappers are not replaced.
No source caller is silently forced onto a new ranking/preparation policy.

Service production requirements retain ORT1.23.2/Transformers4.57.1 and pin
NumPy2.5.3/tokenizers0.22.2 to the inspected production runtime. Capture all resolved
packages from the image; transitive dependency equivalence is not assumed.
SIGTERM/SIGINT stop the HTTP accept loop cleanly; the release owner drains requests
before stopping. No new cancellation/retry or invented scores are introduced.

SDLC owns backup, writer exclusion, exact current mounts/network/env retention,
production-specific internal inference network and service validation before app
cutover. Keep prior image/config/embedded model mount as rollback. Never run the
legacy broad update script merely to apply this image: it rewrites cron; current
scope preserves cron. No AWS action or public runtime image publication.
All actual image/archive identities and test results accompany the delivery file.

For an authorized main release, manually dispatch `mini-minilm-release.yml` with
ref `main`. Dispatches on other branches are rejected. The workflow builds and
qualifies both immutable AMD64 images on GitHub-hosted runners and records the
main commit and source bundle in its evidence artifact. It does not deploy;
SDLC performs a separately authorized Mini cutover using both recorded digests.

The immediately previous app and service images, production configuration, and
backup remain rollback assets. A rollback changes app/service images and
configuration over current authoritative data; it must not automatically restore
an old database snapshot. The main release retained a verified backup at
`/backups/minilm-main-20261020` and left the production database, config, model
tree, and cron unchanged.

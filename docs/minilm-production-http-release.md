# Standalone MiniLM production app target

This release is based on deployed source6cf16d1798bdd09f5a679dc90b9f73cffdd46f1d.
Only MiniLM transport, its explicit new app target and verification change; schema,
import/restore, Qwen/Gemini behavior and production cron remain unchanged.

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
scope preserves cron. No main promotion, AWS action or public image publication.
All actual image/archive identities and test results accompany the delivery file.

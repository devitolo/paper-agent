# Offline ranking quality experiment: independent QA

Status: acceptance preparation; no proposed ranking change cleared.
Production defaults and active profile 18 must remain unchanged. Developer owns
application implementation; tester owns independent acceptance. No live Mini,
provider, source, profile mutation, push, or deployment is authorized by this plan.

## Freeze before evaluation

Record baseline/proposal revisions, scoring and prompt versions, corpus hashes,
profile content hash, source-text snapshots, available evidence, configuration,
random seeds where supported, recorded model responses, and budget limits.
Both systems must use the same frozen inputs; identify any comparison that changes
both evidence selection and scoring, and use an ablation to separate their effects.

The rejected 1/5 Future of SRE and kept 3/5 SSRN 7265684 / paper 618 are known
development anchors, not held-out validation. Neither IDs nor title matching may
drive ranking rules. Freeze train/development/held-out assignment before tuning.
Disclose prior exposure to labels. Partition by paper family/version to prevent
duplicates leaking between splits. If no untouched labeled set exists, report that
limitation rather than describing development results as held-out performance.

## Acceptance matrix

| Area | Independent checks |
| --- | --- |
| Fit versus rigor | Emit distinct current-topic fit and contribution-appropriate rigor/usefulness evidence. An off-topic rigorous paper and an on-topic unsupported paper must not become indistinguishable. Explain any aggregation used for order. |
| Contribution type | Architecture/conceptual work can earn usefulness through explicit reasoning, assumptions, tradeoffs and limitations. Empirical claims need corresponding empirical support. No blanket theory exclusion or mandatory deployment evidence for every contribution type. |
| Feedback nuance | Preserve keep/reject separately from ordinal score. A 3/5 keep can indicate useful introductory material without creating a strong specialist preference or implying top-tier rigor. Test conflicting/limited feedback and no invented negative preference. Proposal remains reviewable; never silently apply it to profile 18. |
| Evidence selection | Test decisive passages beyond the first two chunks, contradictory passages, section boundaries, repeated boilerplate, and a paper with no relevant section. Record selected passage IDs, selection reason and coverage within an explicit budget. Do not imply whole-paper inspection. |
| Grounding | Each citation resolves to the frozen text and correct passage/page/offset convention. Verify quoted text and that it supports the associated claim. Reject fabricated, wrong-paper, out-of-range, or mismatched citations. Correct quote location alone does not prove support. |
| Missing evidence | Distinguish unavailable text, uninspected sections, inspected absence and explicit negative evidence. Timeouts, truncation, parser errors and missing PDFs must not be interpreted as proof of absent rigor. Abstract-only assessment must disclose its evidence boundary. |
| Input attacks/failures | Include paper text containing instructions, malformed model JSON, placeholder fields, unsupported contribution labels, absent citations, and partial valid output before timeout. No private prompt/feedback leakage in diagnostics. |
| Bounded work | Agree numeric text/passage/token/call/time/concurrency limits before running. Check limits on oversized papers, retries, failures and short documents. Report actual calls/usage/runtime and missing usage as unknown. No hidden live calls in offline replay. |
| Safe defaults | With the experiment off or unavailable, baseline ranking, order and business results match exactly on fixed inputs. Experimental options cannot silently change scheduled production behavior. No UI/source/scheduling changes. |
| State integrity | Use disposable/read-only snapshot databases. Compare profiles, feedback applications, pending feedback and recommendation/evaluation rows before/after replay. No production feedback consumed, profile activated, or historical rows rewritten. |

## Evaluation report

Use a frozen labeled corpus with labels appropriate to fit, usefulness and rigor;
do not treat one feedback score as a complete multidimensional ground truth.
Include helpful architecture papers, weak empirical claims, useful empirical work,
off-topic papers, introductory material and incomplete-text cases.

Report paired baseline/proposal results on the same examples: ranking positions,
false positives and missed useful papers at a predeclared shortlist size/threshold,
per-category outcomes, evidence/citation failures, unknown/abstention counts,
model-call/token counts where observed, and runtime distribution. Give sample
counts and uncertainty; small samples cannot establish broad improvement.
Do not equate historical ranking scores such as 71.6 with 1–5 user feedback.
Inspect regressions as well as improvements, and distinguish a reproducible
recorded-response replay from actual model-quality validation.

Additional architect constraints: record coverage/truncation and offsets even when
headings are missing or unconventional; reaching a text cap is not whole-paper
absence. Include a fallback for unconventional section organization. Numbers or
baseline terminology alone do not establish empirical credibility, and architecture
buzzwords alone do not establish useful tradeoffs. Preserve the preference for
empirical rigor while allowing useful introductory work. Predeclare useful/rejected
criteria and all metric denominators. Default replay must be network-free using
stored inputs/judgments; any live assessment is a separate explicitly authorized
mode, never an automatic fallback. If a sanitized reviewed corpus is unavailable,
request it early and label synthetic results as semantic checks only.

## Handoff and gate

Developer supplies ready diff, entrypoint, manifest, split disclosure, numeric
budgets and expected contracts. QA adds executable checks against those concrete
interfaces and reports exact tested code/input state, failures, passes/skips and
remaining uncertainty to developer and architect. Passing tests is not approval
to change production ranking or profile 18. Prepare results and any commit/push
for user review; do not publish or deploy without authorization.

## Independent retest result

Local offline engineering acceptance: PASS for the reviewed uncommitted proposal
at baseline `8e9bdcc1344ee2c945556facad03d01be53ebe44`. This supersedes the initial
six failures and two subsequent claim-polarity failures; it is not approval to
activate ranking changes or evidence of improved paper selection.

- Focused developer/independent tests: 21 passed, including eight independent
  regressions in `tests/test_ranking_quality_v4_qa.py`.
- Final independent core suite: 344 total, 327 passed, 17 skipped, 63.440 seconds.
  Private-snapshot and optional-telemetry coverage remains skipped in this run.
- A separate disposable-DB probe exercised actual Curator persistence, export,
  and replay: recorded V3 assessment and score 72.26 were preserved, with identical
  database bytes before and after export. Production ranking/default files are
  unchanged. File identity guards cover direct paths and symlink/hardlink aliases.
- Proposal SHA-256:
  `05d67758b0e73fa692ab75682aae1345abae14d46e4dc9f4ccf73c22a99d448b`.
- Replay SHA-256:
  `576fab626c6d8224c7387b33aa2ee644694679ff2d12caa7d9efae2b4d7ddbb3`.
- Export script SHA-256:
  `ca0d6ca5ddab8f0d9bb29b9517557ef9a0075bae6936e372144362296d1db07c`.

Limits: lexical citation checks are screening heuristics, not semantic proof.
Stored summaries are not primary-paper text. No real corpus replay, untouched
held-out evaluation, or active profile 18 audit has been performed. Export contains
profile and paper/assessment text and should be inspected before sharing; omission
of dedicated raw-feedback fields is not universal free-text redaction. No live
Mini/provider/source request, production mutation, commit, push or deployment was
performed by QA. CORE discovery acceptance is a separate task.

## Completeness-report guard follow-up

Final guard engineering retest PASS: 27 focused tests, including 13 independent
regressions. Replay SHA-256 now
`533105c938053403865def26a46ef09daf1ea373c215e7bc19c810f88cf07050`.
Invalid/missing assessments use neutral zero-rigor fallback. Exact references and
grounded usable citations are separate; wrong-type enums are invalid rather than
crashing. Quality-claim readiness remains false pending external review. Developer
reports a separate full run of 350 tests, 333 passed and 17 skipped; that run is
developer evidence, not independently rerun here.

Earlier independent real-corpus checks used profile 19, 15 development papers,
zero held-out examples, generated summaries only, and no proposed assessments.
The report correctly became INCOMPLETE after guard fixes. Corpus SHA-256:
`9e48a353a2bb0a1387f3774c4a22babf4bc6db5c23268a6ae7d8eb883b15f7fb`.
Label perturbation left deterministic scores unchanged; this does not validate a
future assessor's input isolation. Kept papers 88 and 144 each had 12-point
component-word negative-profile penalties requiring separate semantic review.
The original Downloads file was unavailable at final retest, so this final guard
hash was tested with synthetic regressions, not a fresh real-corpus run. This
updates the earlier no-real-replay statement without establishing completed V4
evaluation or ranking improvement. No production changes or publication by QA.

Restored-corpus verification: the user restored the original JSON and its hash
matches `9e48a353a2bb0a1387f3774c4a22babf4bc6db5c23268a6ae7d8eb883b15f7fb`
exactly. The final replay hash `533105c938053403865def26a46ef09daf1ea373c215e7bc19c810f88cf07050`
was independently rerun on it: profile 19, 15 development / 0 held-out / 0
unassigned papers, all stored triage summaries, 15 missing assessments, zero exact
references and zero usable citations. Result: INCOMPLETE, evaluation_complete=false,
quality_claim_ready=false, all fallback rigor zero. Input bytes unchanged; zero
network/model calls. This resolves the missing-file validation limitation, not
the missing assessments/evidence/held-out evaluation.

## Substantive assessment stage acceptance

User authorization covers proceeding with development-corpus assessment, not
production ranking activation. Developer owns implementation. Before execution,
record primary-text availability per paper, originating artifact/source and hash,
extraction method, coverage and offsets; generated summaries cannot stand in for
primary text. Summary-only/missing text remains unknown or explicitly limited.

The assessor input builder must allowlist necessary paper passages/provenance and
any explicitly intended frozen-profile context. Test the complete outgoing request
using distinct sentinels in decision, user_score, raw review, expected rank, and
baseline/proposed scores; none may reach the model. Changing those labels while
holding passages/profile fixed must leave requests identical. Profile 19 already
reflects historical learning; disclose that separately from direct label leakage.

Freeze and record model identity, prompt/config versions, per-paper and total
call/token/text/time/retry budgets before running. Record actual usage and unknown
usage accurately. No automatic external provider fallback. Verify invalid outputs,
timeouts and budget exhaustion cannot create usable evidence or modify production
state. Persist judgments and input hashes for deterministic replay; citations must
resolve to supplied primary passages, with model assertions distinguished from
independently established support.

Review any missing-text export for read-only snapshot handling, input/output alias
protection, explicit text scope and private-path/credential exclusion before giving
the user a command. Actual execution environment availability is a dependency;
unavailable Mini access or external/paid execution requires user input. QA does not
initiate those actions from the handoff. Report development-only results and
regressions; 15 previously seen papers and zero held-out examples cannot establish
held-out improvement. Commit, push, deployment and production profile/ranking
changes remain separately gated.

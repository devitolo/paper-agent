# AI Stack

This document records the current AI/model boundaries for Project Paper. It
separates the public package baseline from the richer production Mini stack so
release claims do not drift.

## Current v0.2 Direction

v0.2 is about recommendation quality and installability. The product goal is a
small number of technical papers genuinely worth the user's time, not a larger
pile of loosely related papers.

Current production quality work separates several concerns:

- source retrieval quality: arXiv, improved OpenAlex retrieval, progressive
  Semantic Scholar retrieval, and opt-in CORE;
- topical interest fit: MiniLM scores title/abstract fit before active Curator
  evaluation;
- evidence judgment: Qwen3 4B assesses research type and evidence quality for
  the top preliminary candidates;
- review summaries: Qwen 2.5 1.5B extracts the Review Queue fields `Problem`,
  `Why it matters`, and `Approach`;
- feedback/profile learning: user feedback remains stored separately and can
  guide future retrieval and profile updates.

MiniLM Eval and paired-shadow experiments are disabled as production navigation
surfaces. Their historical tables/tests may remain for migration compatibility
and future analysis, but current recommendation behavior uses the active Curator
path rather than those temporary evaluation pages.

## Production Mini Stack

The production Mini uses local models with distinct responsibilities:

- **MiniLM:** topical interest fit from title and abstract. This helps decide
  which candidates deserve deeper Curator attention.
- **Qwen3 4B:** active Curator evidence assessment for the top 10 preliminary
  candidates. It can adjust Curator scoring through structured assessment.
- **Qwen 2.5 1.5B:** extraction model for `Problem`, `Why it matters`, and
  `Approach`.
- **Gemini:** optional low-volume profile synthesis and rebuild workflows.

Qwen3 failure must not become a paper-quality signal. If Qwen3 times out, fails,
or returns invalid output, Curator falls back to deterministic scoring.
Conceptual, architecture, framework, and threat-model papers are not penalized
merely because they are non-empirical; unsupported experimental claims remain
weak evidence.

## Public Package Boundary

The accepted public package baseline is still v0.1.3. It provides a local-first
Compose install, manual arXiv discovery, local Qwen 2.5 extraction, Review
Queue, feedback storage, diagnostics, and packaged backup/restore. It does not
require OpenAI, Gemini, OpenAlex, Semantic Scholar, CORE, host cron, systemd, or
Mini production deployment.

v0.2 package acceptance has not yet been run. Until it is, do not claim that a
fresh public install requires or fully supports the production Mini stack.
MiniLM can be included in v0.2 if installer and acceptance evidence support it
cleanly. Qwen3 Curator judging should remain optional/advanced until public
package acceptance proves it is safe to require.

## Optional Sources and Cloud Use

Advanced/operator source adapters include OpenAlex, Semantic Scholar, and CORE.
They have source-specific API and rate-limit rules and should stay out of the
default first-user path unless packaged acceptance covers them.

Cloud model use is optional and low-volume. External Paper Discussion remains a
manual handoff: Project Paper owns discovery, recommendation, local review
state, feedback storage, and personalization; the user can use a preferred
external reading or AI workflow for deep paper discussion.

## Future Hosted Direction

The next productization topic is whether Project Paper should run on AWS while
staying simple. Treat that as a productization investigation, not a current
runtime requirement. Any hosted path must preserve the public/local install
story, avoid surprise credential or data exposure, and keep recommendation
quality evidence separate from deployment mechanics.

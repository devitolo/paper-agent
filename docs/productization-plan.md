# Project Paper: V1 productization assessment and plan

Status: M1.1 installer/Compose packaging and M1.2 manual discovery are implemented in the working tree. Independent code QA passes; [live M1 acceptance](m1-qa-report.md) passes conditionally on macOS Apple Silicon, with Linux x86-64 and published-image qualification outstanding. Updated 2026-09-09 against repository baseline `0b8401f` and the PM's supplied “Productization Lead Background.”

## Brief and revised direction

Make the existing product portable enough that another technically competent user can install and use it without its creator. The first milestone is **Fresh Install Produces Papers**: clone → minimal configuration → Docker Compose → prepared local Qwen → open UI → configure topics → run bootstrap discovery → see recommendations → restart and retain data.

The PM has selected Docker/Compose as the primary V1 deployment mechanism, fronted by an installer script that detects and validates the customer's supported host configuration. This supersedes this plan's earlier host-native recommendation. The existing Mini's systemd/cron deployment remains operational and untouched; it is not a second new-user installation path to productize. Scheduling, operational polish and showcase work follow the first milestone. Recommendation-quality evaluation continues independently and does not need to be “finished” before productization proceeds.

Preserve Scout → Curator → Review Queue → external Paper Discussion → pasted feedback/score → existing learning paths. The Review Queue is sufficient unless fresh-user testing identifies a concrete problem. Gemini is optional; direct ChatGPT integration is unnecessary. Do not redesign ranking, retrieval, or topic agents to make packaging appear more complete.

## Current-state assessment

“Reuse” means an existing implementation is available, not that a fresh Compose installation has been validated.

| Work area | Existing behavior and evidence | Smallest productization gap |
| --- | --- | --- |
| Docker/Compose | [Dockerfile](../Dockerfile) packages the Python CLI; [Compose](../docker-compose.yml) mounts `.env` and `data`, then runs `profile`. | Package required `sql/`, configuration templates and `pdftotext`; start web, publish loopback port, add Ollama/model preparation. Remove personal `data/` from build distribution. |
| Local model | [Extraction](../paper_agents/local_extract.py), [pipeline](../paper_agents/pipeline.py) and [Curator evidence](../paper_agents/curator_evidence.py) use Qwen/Ollama. | Persistent Ollama service/model cache; prepare `qwen2.5:1.5b-instruct` once; wire all app calls to the Compose service instead of container-local localhost. |
| Persistence | [SQLite schema/init](../paper_agents/db.py), [topic config](../paper_agents/topics.py), [profile file](../paper_agents/store.py). | Stable writable volumes for DB, artifacts, profile and topics; initialize only absent state, without creator history or learned preferences. Confirm permissions and restart/recreate behavior. |
| Configuration/secrets | Existing CLI options and provider configuration; current service/cron use different environment sources. | Clean `.env.example`, documented minimum settings and consistent container configuration. Mounting a file alone does not establish that every execution path reads it. No secrets in logs/build context. |
| Optional dependencies | Three source adapters; Gemini profile synthesis in [feedback](../paper_agents/feedback.py). Feedback commits before synthesis. | Explicit Gemini-disabled behavior: current [web save](../paper_agents/web.py) always queues synthesis. Missing credentials must not create a broken first-run experience. |
| Cold start | Existing pipeline orchestrates Scout → Curator → Reviewer with bounded rescout. | Connect a clean topic configuration and empty DB to this pipeline; do not use creator seed papers to demonstrate successful discovery. |
| Rate limits/degradation | [Scout adapters](../paper_agents/scout.py), pipeline/source diagnostics and existing wrappers. | Validate per-source partial success in the new manual orchestration; one failed source must not erase successful results or prevent reviewing prior papers. |
| Manual/automated runs | CLI `pipeline-daily`, managed [Mini cron](../deploy/project-paper.crontab) and locks work today. Web POST routes cover topics/feedback, not discovery. | First-class UI Run Scout action invoking the full existing pipeline, with status and duplicate-run protection. Packaged automation comes later; no host cron knowledge for first use. |
| Topic configuration | `/topics` already persists UI edits and includes an experimental Topic Agent. | Resolve the minimal guided setup experience with PM; reuse persistence and validation. Advanced Topic Agent work remains paused. |
| Paper Discussion | Existing Copy discussion prompt and feedback parser/UI in [web](../paper_agents/web.py). | Retain handoff; publish a reusable prompt and verify returned feedback format. No built-in reader. |
| Upgrades/backups | [DB init](../paper_agents/db.py) has compatibility migration; [backup](../scripts/backup_db.sh) uses SQLite online backup and integrity checks. | Define release/schema version compatibility and volume-preserving upgrades; rehearse matched code/data rollback and full-state restore before public release. |
| Health/failure UX | `/health`, diagnostics and repair runbooks in [workflow](workflow.md). | Add or connect model-preparation/manual-run status and actionable errors. Avoid building another monitoring system. |
| README/docs | Extensive engineering history and operational instructions exist. | Replace the public front door with one supported Compose path; move historical details to `/docs`. Current required OpenAI key/Docker quickstart/runtime claims conflict. |
| Fresh-machine testing/showcase | [Test suites](../tests), [QA snapshot](../qa/README.md), no demonstrated clean Compose acceptance. | Real fresh-host milestone test first; screenshot/demo from a successful sanitized install afterward. |

### What already works and should be reused

Keep source adapters, rate-limit handling, bounded candidate refill, Scout/Curator separation, Qwen evidence/triage, canonical SQLite history, Review Queue, scores, raw feedback persistence, optional Gemini synthesis, discussion-copy behavior, health diagnostics and existing backup primitive. Package these before proposing replacements. Container path/configuration fixes are integration work, not reasons to rewrite their algorithms.

Local structured feedback directly informs Scout guidance through [scout_guidance.py](../paper_agents/scout_guidance.py). Curator uses the active profile; current automated profile synthesis requires Gemini. Therefore Gemini-off can preserve feedback and existing local Scout adaptation, but must not be marketed as equivalent local profile evolution. PM's optional-Gemini requirement does not authorize a new local synthesis algorithm.

## M1: Fresh Install Produces Papers

### Launch-blocking work for this milestone

1. **Installer and complete multi-architecture application image:** publish one Project Paper image name with tested `linux/amd64` and `linux/arm64` variants behind a multi-platform manifest. Provide one idempotent installer entry point that detects the host OS/architecture, checks Docker/Compose and resources, prepares customer configuration, selects any required platform-specific Compose override, and starts the supported stack. Include schema/assets/default templates and extraction dependency; default to the web process, not `profile`; distribute no private database, feedback, papers or learned profile.
2. **Compose-managed inference:** Ollama service plus an idempotent model preparation step and persistent model volume. Show pulling/ready/failed state and a retry. Do not bake Qwen into the app image or require selecting a model during onboarding.
3. **Consistent state/configuration:** persistent application data and writable topic configuration, safe initial state and correct service URLs. A clean install must not require any provider key.
4. **Topic-to-discovery entry point:** minimum PM-approved in-product topic configuration and manual Run Scout, using existing Scout → Curator → Reviewer. A source-only command that never produces recommendations does not satisfy M1.
5. **Bounded progress/failure behavior:** prevent duplicate launches, preserve partial results and show understandable source/model errors. Existing papers remain reviewable if a dependency fails.
6. **Minimal quickstart and fresh-install verification:** exact tested startup commands and restart/recreate instructions. Full README polish is later; usable instructions are part of M1.

Gemini-off must be safe from the first package, but full optional-Gemini setup/retry polish need not delay M1. Preserve existing feedback and handoff controls; do not expand M1 into a feedback quality experiment or an operations platform.

### M1 acceptance criteria

- [ ] QA records the exact commit/image, Compose/runtime versions, host OS/architecture/resources and default model. Only that configuration is claimed tested.
- [ ] A technically competent tester unfamiliar with the project follows the quickstart with empty app/model volumes, no private fixture/history/profile and no API/cloud credentials. No creator help, internal YAML edits, or host cron/systemd setup is needed.
- [ ] Compose starts the app and Ollama, prepares the default Qwen model, and exposes the UI on a host loopback port. Failed or interrupted model preparation is visible and retryable.
- [ ] User configures research topics in the product and triggers a bounded bootstrap pipeline. With reachable core sources and a documented known-productive topic, at least one real discovered recommendation appears in Review Queue; QA records source/run IDs and elapsed time.
- [ ] The default arXiv-only no-key path is exercised. Absence of OpenAlex, Semantic Scholar and Gemini does not block use. OpenAlex's no-key path and cross-source partial-failure behavior are validated before OpenAlex is presented as a supported opt-in.
- [ ] If all sources fail or genuinely return no candidates, the UI explains the condition and offers a bounded retry or topic correction. This fault test verifies recovery, but does not replace the required successful real-paper test. Never fabricate papers or retry indefinitely to meet a quota.
- [ ] User saves feedback/score on a returned paper. Restart and container recreation preserve its exact content, recommendations/history, topic configuration and profile state. A seeded test profile may test persistence separately but is not used to fake cold-start personalization.
- [ ] Normal restart/recreation uses the cached model without pulling its weights again; failed readiness never masquerades as a working model.
- [ ] Repeated Run Scout clicks cannot start duplicate concurrent pipelines. Existing Qwen/ranking behavior and evidence labels are preserved.
- [ ] Relevant existing regression tests pass; no live Mini state or schedules are modified. Record limitations instead of claiming supported versions/hardware that were not tested.

## Runtime ownership for V1

| Component | Ownership and boundary |
| --- | --- |
| Installer and Docker/Compose | A single installer is the product entry point. It detects the customer's OS/architecture, rejects untested configurations clearly, checks Docker/Compose and resources, generates or preserves configuration, and starts the primary packaged runtime. Project Paper uses one multi-architecture image reference rather than separate Mac/Linux product images; Docker selects the matching Linux container variant. Platform-specific Compose overrides are allowed only for verified host/runtime differences. No host Python, Ollama, cron or systemd knowledge required. Compose manages application and model services. |
| Web | Application container starts the existing web UI. Bind within the container as required for port mapping; publish only on host loopback. No accounts or public serving in V1. |
| Ollama/Qwen | Separate Compose service, persistent model storage, idempotent preparation for the default model. Ollama endpoint stays on the Compose network; app image contains no model weights. All discovery/extraction/topic call paths use the configured endpoint. |
| SQLite/data | Bundled library, no database container/service. Persistent app volume holds DB and artifacts/profile; persistent writable config location holds topics. Exact mount layout is Architect-owned and must preserve existing relative-path expectations or adapt them minimally. |
| Credentials | `.env.example` documents infrastructure only. Keys/auth are opt-in and excluded from image/context/logs. Gemini currently uses authenticated CLI execution; its container authentication contract is a later explicit architecture task, not an assumed API-key integration. |
| Sources | arXiv/OpenAlex core, Semantic Scholar optional (key recommended). Apply existing bounded source-specific rate handling; partial results remain useful. |
| Scheduling | Manual Run Scout is M1. Later choose one Compose-owned automated mechanism using the same run/exclusion path. No new-user host cron installation; existing Mini cron remains untouched. |
| Backups | Product supplies verified backup/restore instructions or commands; user owns backup destination. Include DB, topics and non-reconstructible user state; retain artifacts or explicitly document their recovery limitations. Model cache may be re-pulled and is not authoritative user data. |

Before public release, choose tagged releases and a supported upgrade range. Update code/images while retaining volumes; back up state, quiesce writers, validate schema compatibility, migrate, verify queue/feedback and resume. Failed upgrades restore matching code and snapshot. Never use `db reset`, delete volumes, or silently reseed user state as an upgrade strategy. Destructive volume removal must not appear in normal restart/update instructions.

## Phased, dependency-aware backlog

PM owns scope/priorities/UX. Productization Lead coordinates evidence and sequencing. Architect owns boundaries; Developer implements the smallest missing integration; QA verifies clean-user behavior; Technical Writer owns executable user documentation. Planning does not authorize broad implementation.

| ID / phase | Accountable owner; collaborators | Dependency | Deliverable / acceptance |
| --- | --- | --- | --- |
| A0 Assessment/contract | Architect; PM, Developer | Current assessment | Confirm mount layout, model readiness and manual-run integration; PM resolves topic UX and bootstrap defaults. Existing components reused, no scoring changes. |
| M1.1 Installer and Compose package — implemented; Mac live pass | Developer; Architect | A0 | Idempotent installer, one multi-architecture app image reference, Ollama service/model preparation, volumes and `.env.example`; host detection plus empty-volume startup, endpoint and permissions checks pass on each claimed platform. Re-running the installer preserves data and customer choices. Linux and published-image qualification remain. |
| M1.2 First-run workflow — implemented; Mac live pass | Developer; PM, Architect | A0; integrate M1.1 | Existing topic UI → Run Scout path, durable status/retry and shared exclusion using the existing pipeline; three real papers reached the Mac QA queue without credentials. |
| M1.3 Quickstart — development guide drafted | Technical Writer; Developer | Draft with M1.1/M1.2 | Exact minimum setup/start/restart steps available before QA; no undocumented creator setup. Public README/image references remain release work. |
| M1.4 Fresh-install acceptance — Mac conditional pass | QA; Developer, Writer | M1.1–M1.3 | Mac Apple Silicon evidence is recorded in `docs/m1-qa-report.md`; repeat on Linux x86-64 and published artifacts before claiming both platforms. |
| V1.1 Data/upgrade safety | Architect; Developer, QA | M1 package layout | Developer implements minimal version/migration handling and matched backup/restore; QA rehearses upgrade, rollback, disk/write failure and feedback survival. Required before public users accumulate data. |
| V1.2 Optional integrations/learning | Developer; Architect, QA | M1.4 | Gemini opt-in/auth disclosure and durable pending/applied/failed/retry state; no duplicate apply after restart. Verify local Scout feedback path independently. Semantic Scholar optional key/failure coverage. |
| V1.3 Discussion handoff | Technical Writer; PM, QA | M1.4 | Publish prompt for section-by-section discussion, technical explanation, claims vs interpretation, user reactions, overall score and parser-compatible feedback. QA round-trip through existing copy/save UI; no ChatGPT dependency. |
| V1.4 Packaged scheduling | Architect; Developer, PM, QA | M1 run/exclusion path | Select smallest Compose-owned mechanism; user sees cadence/timezone and can disable it; no overlap or hidden failures. Manual first use remains independent. Cadence policy requires PM input. |
| V1.5 Health/support/docs | Technical Writer; Developer, QA, PM | M1; finalize after V1.1–V1.4 | Actionable health/recovery, bounded logs/redaction, supported upgrade/backup guide, concise README and limitations. QA follows docs on supported clean environment. |
| V1.6 Showcase/release | PM; Writer, QA | Public-release gates below | One sanitized README screenshot, one-minute project explanation, supported configurations and known limits. Additional demo assets are deferred. Publish only evidence-backed claims. |
| Later | PM prioritizes; appropriate engineering owner | Concrete user evidence | Advanced configurability and deferred items below. No automatic promotion into launch blockers. |

V1.1–V1.4 can be scoped independently after mount/run contracts stabilize. Do not wait for documentation polish, automated schedules or ranking calibration to start or complete M1.

## Public-release readiness (after M1)

- [ ] M1 passed on a declared supported configuration; README leads with that single Compose path.
- [ ] Restart/rebuild/upgrade preserve DB, scores/reviews, raw feedback, profile, topics and model cache. Migration/version strategy and matched restore/rollback are exercised before public data accumulates.
- [ ] Feedback save succeeds independently of Gemini availability; optional cloud calls require explicit configuration/consent. Gemini-off local learning limitations are documented accurately.
- [ ] External Paper Discussion handoff and pasted feedback round-trip work with the documented prompt; no ChatGPT account/integration is required by the product.
- [ ] Source rate limits, partial/all-source failure, model failure and interrupted jobs produce visible, bounded behavior without discarding prior data. Saved papers remain usable during network outages.
- [ ] Manual discovery remains first-class. Any shipped automated schedule uses the packaged ownership/locking contract and can be disabled; no host scheduling expertise needed.
- [ ] Backup integrity and full-state restoration are demonstrated. Retain seven daily and four weekly backups, document the independent storage responsibility and recovery limitations, and verify pruning never removes the newest valid backup.
- [ ] Loopback exposure, private state permissions, credential redaction and safe local mutation/artifact access are reviewed. Support export is opt-in; private feedback/database contents are not silently uploaded. No automatic telemetry.
- [ ] README includes purpose, screenshot/demo, workflow, quickstart, requirements/configuration, optional integrations, discussion/feedback explanation and links to deeper architecture/operations docs. Historical Mini/reset instructions cannot be mistaken for new-user setup.
- [ ] QA reports exact test evidence and limitations. Support uses GitHub Issues with issue templates for bug reports and support requests; maintainers support only the current major version. Algorithm validation is tracked separately, with no unsupported “recommendations proven better” claim.

## Ranking workstream boundary

Continue productization while real-paper ranking evaluation proceeds. Existing regression suites protect behavior; they do not establish that quality is finished. [Product changelog](product-changelog.md) records V3 offline/mock QA with Mini quality/latency caveats and an earlier private preference-gate failure. Keep those visible in the separate quality workstream rather than making its completion an M1 dependency.

No further ranking change is proposed here. Architect must identify a repeated, evidenced failure before a separate isolated algorithm task is considered; PM scopes it, Developer isolates it and QA applies replay/held-out/runtime gates. If a demonstrated defect actually prevents the first-run workflow, surface that dependency explicitly rather than quietly tuning algorithms. Do not add new local profile synthesis to satisfy a stronger marketing claim.

## Open decisions and resolved directions

**Resolved by PM:** installer-fronted Compose primary; one multi-architecture Project Paper image reference with automatic platform selection; separate Ollama with persistent default Qwen; arXiv-only default source; no required cloud credentials; manual first use; external discussion boundary; existing Review Queue retained; scheduling/polish follow M1; sophisticated Topic Agent paused; seven daily and four weekly backups; GitHub Issues support for the current major version only; README screenshot as the only initial showcase asset. These no longer require reapproval.

| Remaining decision | Recommendation / consequence | Owner / needed by |
| --- | --- | --- |
| Topic setup UX | Reuse existing persistence/validation and make the smallest guided in-product path. Earlier user direction rejects forms/chat as the eventual experience; PM notes permit reuse but do not explicitly override that constraint. Review a small interaction proposal before UI implementation; do not assume legacy `/topics` alone satisfies it or launch a redesign. | PM, before M1.2 |
| Customer research domain/topics | Customer chooses topics in the product. Product supplies a known-productive AIOps/software example for acceptance and clearly states which domains have been tested. A customer choice outside tested domains is allowed but carries no relevance-quality guarantee. | Customer at onboarding; PM defines tested-domain wording before M1.3 |
| Customer source selection/bootstrap | Customer chooses sources. **Resolved default: arXiv only**, because it is currently the most reliable path. OpenAlex and Semantic Scholar are explicit opt-ins; Semantic Scholar may request an API key. Permit changing the selection before the initial run. Partial success remains acceptable when multiple sources are enabled. | Customer at onboarding; default approved by PM |
| Customer host platform | Customer uses their Mac or Linux machine; the installer detects OS/architecture and proceeds only for configurations Project Paper has tested. Architect and QA define the first support matrix and measured resource requirements. | Customer at install; Architect + QA define supported values before M1.4 |
| Customer Gemini choice | Customer explicitly enables or leaves Gemini disabled. Preserve current mechanism only if it can be packaged safely; disclose credential storage and sent data. Local feedback persistence/Scout guidance remain available, while direct profile evolution is not promised with Gemini off. | Customer at configuration; Architect defines packaging contract by V1.2 |
| Customer schedule/cadence | Customer chooses manual-only or a packaged cadence after the manual flow works. Recommend manual-only initially, then offer a simple default cadence with visible timezone and disable control. | Customer at configuration; Architect + PM define offered choices by V1.4 |
| Upgrade compatibility | Maintainers support the current major version only. Within that major, upgrades must preserve data across supported minor/patch releases. Moving to a future major requires a separately documented migration path; older majors stop receiving support when the new major becomes supported. | Architect defines version/migration policy before V1.1; PM confirms end-of-support wording |

Defer Go migration, multi-user/accounts, SaaS, Kubernetes, enterprise deployment, native installers, mobile, new sources, advanced Topic Agent/conversational infrastructure, built-in full reader/deep reading, direct ChatGPT integration, major Scout/Curator experimentation and alternate model selection. Do not add broad operations features solely to appear production-grade.

## Validation of this assessment

Architect and Developer/QA reviews ground the gaps in the current files. Earlier local QA ran 192 tests on Python 3.14: 187 non-skipped passes, 5 skips, overall OK, with SQLite connection ResourceWarnings. That is regression evidence only, not fresh Compose/source/model/upgrade acceptance. This turn changes planning documentation only; M1 remains to be implemented and tested.

# M1 acceptance report

Status: **pass for the first supported Ubuntu x86-64 package path**. The exact public v0.1.2 app and selected Ollama image passed fresh installer, Qwen, live arXiv discovery, feedback, persistence, recreation and recovery acceptance on a GitHub-hosted Ubuntu x86-64 VM on 2026-09-11. macOS Apple Silicon remains a preview path pending a clean exact-release end-to-end pass.

## Public v0.1.2 Linux acceptance

- Run: [Release Acceptance #2](https://github.com/devitolo/paper-agent/actions/runs/34574831727), completed successfully on GitHub-hosted Ubuntu x86-64.
- App: `ghcr.io/devitolo/paper-agent@sha256:9ca07d264124fff083f34f358fe0897b0a48fea2161e31349cf1d95b0b15c25f`.
- Ollama: `docker.io/ollama/ollama@sha256:684d8674b4315fa18f4f0e973a118ec2652ed96f67563277839985175858e0ba`.
- Passed: host/platform validation, normal fresh installer, model download and bounded inference, topic submission, live arXiv Scout, persisted papers and recommendations, feedback submission, app recreation with unchanged counts, abandoned-run reconciliation, evidence upload and volume cleanup.
- Evidence artifact: `project-paper-linux-amd64-acceptance-34574831727`, retained by GitHub Actions for 14 days.

## Code and static validation

- Earlier persistent-tester container/code checks reviewed the integrated M1.1 installer/Compose package and M1.2 manual Scout workflow before the final commit; they did not invoke live model or source-discovery calls.
- Previous full local suite: 228 total tests: 223 passed and 5 private ranking replay fixtures skipped. Current remediation retest: 233 total tests: 228 passed and 5 skipped.
- Previous focused package/manual-run suite: 23 passed. Current remediation-focused suite: 27 passed.
- `git diff --check`, Python compilation, Bash/sh syntax and Compose static configuration passed.
- Existing SQLite connection `ResourceWarning` output remains non-blocking and was not introduced as an M1 behavior change.

## Earlier live macOS Apple Silicon acceptance

The evidence in this section is distinct from the persistent tester's current code/container review above. These earlier acceptance checks did invoke bounded local Qwen inference and real arXiv discovery.

Test host used Docker Desktop with Docker Engine 29.6.2, Compose 5.3.1 and Linux arm64 containers. The application was built locally as `project-paper:local-m1`; this is not a published multi-platform release. Ollama resolved to `ollama/ollama@sha256:32931b46719f673c05fdbaa81ccb26da18ea4a1c57590a754874ab28ba269eb2`. The prepared model was `qwen2.5:1.5b-instruct`, ID `65ec06548149`, reported size 986 MB.

Passed evidence:

1. A sanitized copy with empty app/config/model volumes built the non-root application image and pulled the selected Ollama image.
2. An occupied default port produced a clear failure without terminating the owner or deleting volumes. Changing the isolated installation to port 8124 and rerunning preserved its installation identity and completed setup.
3. App/database readiness and a real bounded Qwen inference check passed. Ollama was not published to the host; the web UI was loopback-only.
4. A topic was created and edited through the HTTP UI. The first fast-path topic expansion produced zero arXiv entries; setting the explicit query to `AIOps` through the existing edit control produced 10 real candidates. This is onboarding evidence for supplying a known productive example, not authorization to alter retrieval.
5. The real full arXiv Scout → Curator → Reviewer pipeline completed as workflow cycle 2 with 3 recommendations. All three selected PDFs were downloaded and extracted through local Qwen.
6. Exact feedback with score 4.5 was saved through the HTTP UI. No Gemini profile-apply attempt was created while Gemini was disabled.
7. Installer rerun completed with the cached model and no pull. Forced app/Ollama container recreation preserved 3 recommendations, exact feedback, topic configuration and model data. Post-recreation app/database/Ollama/model-presence checks passed.
8. Independent code QA verified shared web/packaged-CLI exclusion, durable queued/running/completed/empty/failed/interrupted states, canonical worker launch, packaged-only UI/locking, and unchanged native Mini behavior.

## Remaining work

- Obtain a clean exact-release end-to-end pass on macOS Apple Silicon before promoting that preview path to supported. Two isolated attempts passed installation and model readiness; one later hit an Ollama 120-second generation failure and the other received arXiv HTTP 429 after the existing retries. Neither failure occurred in the Linux acceptance run.
- Exercise released-image upgrade/rollback and the later backup policy before claiming those operational guarantees. This is outside the Fresh Install Produces Papers milestone.
- Add the approved sanitized README screenshot during showcase work.

The first milestone is fully demonstrated on the supported Linux path. The package is not yet a two-platform supported release.

## Persistent Tester Final Gate (2026-09-10)

**SHIP** for the implemented macOS M1 scope at committed SHA `4ad40f1` (`4ad40f19bfc5693bde7679f04c18c2dcbf5971e1`). Earlier baseline-plus-uncommitted evidence above remains historical evidence, not the final gate identifier. The persistent tester found no actionable remediation defects.

- 58 independent malformed-state and readiness-cache probes passed.
- Full suite: 233 total tests; 228 passed and 5 private-fixture skips.
- Focused package/manual-run suite: 27 passed.
- Python compilation, `git diff --check`, shell syntax, and Compose static checks passed.
- No live model, source, or container calls were repeated for this final gate.

This historical gate was superseded by the public v0.1.2 Linux acceptance recorded above.

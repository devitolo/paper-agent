# M1 acceptance report

Status: conditional pass on macOS Apple Silicon; Linux x86-64 qualification and published multi-platform images remain outstanding. Test date: 2026-09-09. Earlier persistent-tester review evidence used baseline `d6ae935` plus then-uncommitted M1 productization changes; the final accepted implementation is committed on `main` as `4ad40f1`.

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

## Remaining gates

- Repeat the full acceptance on Linux x86-64 before claiming Linux support.
- Publish one pinned multi-platform Project Paper image and package/pin the model-preparation helper instead of bind-mounting it from the checkout.
- Choose and qualify release image references, Docker/Compose versions and resource thresholds. The current use of `ollama/ollama:latest` was limited to development QA; the digest above is evidence, not a release selection.
- Exercise a released-image upgrade/rollback and the later backup policy before public release. This is outside the Fresh Install Produces Papers milestone.
- Add the approved sanitized README screenshot during showcase work.

The first milestone is functionally demonstrated on one of the two target platforms. It is not yet a two-platform supported release.

## Persistent Tester Final Gate (2026-09-10)

**SHIP** for the implemented macOS M1 scope at committed SHA `4ad40f1` (`4ad40f19bfc5693bde7679f04c18c2dcbf5971e1`). Earlier baseline-plus-uncommitted evidence above remains historical evidence, not the final gate identifier. The persistent tester found no actionable remediation defects.

- 58 independent malformed-state and readiness-cache probes passed.
- Full suite: 233 total tests; 228 passed and 5 private-fixture skips.
- Focused package/manual-run suite: 27 passed.
- Python compilation, `git diff --check`, shell syntax, and Compose static checks passed.
- No live model, source, or container calls were repeated for this final gate.

This gate does not change the outstanding release qualifications above: Linux x86-64 validation and a pinned, published multi-platform image remain required before claiming a two-platform supported release.

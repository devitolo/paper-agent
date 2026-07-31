# Product Changelog

Durable product and process decisions for Project Paper. Keep entries chronological and focused on decisions that should survive across implementation threads.

## 2026-07-30

### Review Queue UI: denser review inbox

Decision: The Review Queue UI should become denser and more review-inbox-like so paper triage is faster and less visually noisy.

Product direction:

- Reduce overall font sizes and spacing.
- Make header controls more compact.
- Replace the large status filter button group with a dropdown/select.
- Replace latest vs score sorting with a compact control, icon button pair, or small select.
- Replace full vs condensed view with a dropdown/select.
- Prioritize quick review actions on paper cards and reduce visual clutter.
- Add an easy copy icon/button for the original article link on each paper card.
- Remove the visible "No ChatGPT review yet" placeholder.
- Remove the "Copy prompt/link" toggle because it is not needed in the current workflow.
- Show the score as just the number, without the word "score."
- Move Interested, Read later, Not interested, and Reviewed into a compact vertical action rail on the right side under the score.
- Rename the Notes textarea to Feedback.

### Feedback workflow: blob-first

Decision: Feedback should be blob-first rather than a highly structured form as the main user path.

Normal workflow:

1. Open the paper queue.
2. Pick a paper.
3. Discuss and summarize it in ChatGPT.
4. Paste the resulting feedback blob back into Project Paper.

Product direction:

- The Feedback box is where the user pastes the feedback blob from a ChatGPT paper discussion.
- The first UI version may keep using the existing lightweight feedback storage/table for status plus notes, but visually and conceptually the field should be Feedback.
- CLI and UI should eventually share one backend feedback ingestion function.
- Future V2 feedback ingestion should store the exact blob in `raw_feedback`, create a `feedback_parse_attempt`, and write normalized output into `structured_feedback`.
- Do not require the V2 parser/profile update in the first UI redesign; capturing the blob cleanly comes first.

### Feedback ingestion: deterministic V2 storage first

Status: Completed in commit `47c030b`.

Decision: The first V2 feedback ingestion step captures and deterministically parses blobs without changing future recommendations yet.

Completed behavior:

- The review queue Feedback box stores non-empty pasted blobs through the shared V2 ingestion path.
- Existing lightweight review status storage remains in place.
- Exact pasted blobs are stored in `raw_feedback` and deduped by content hash.
- Deterministic parser v1 records `feedback_parse_attempts` and writes normalized rows to `structured_feedback`.
- Parser v1 recognizes simple lines such as `Decision: keep|maybe|reject|interested|read later|not interested|reviewed` and `Score: 1-5`.
- `profile_versions`, ScoutAgent, and CuratorAgent do not consume structured feedback yet.

### Feedback profile evolution: Gemini apply step

Decision: Profile evolution from structured feedback should be explicit, reviewable, and tracked.

Completed behavior:

- Add `feedback_profile_applications` to link consumed `structured_feedback` rows to generated `profile_versions` rows.
- Add `feedback apply --provider gemini --dry-run` so the operator can preview Gemini's proposed profile update.
- Add `feedback apply --provider gemini` to create a new active profile version and mark the consumed structured feedback rows as applied.
- Keep profile evolution out of the review queue POST path.
- Keep ScoutAgent and CuratorAgent reading active `profile_versions` rather than consuming `structured_feedback` directly.

Operational guidance:

- Run dry-run first, inspect the proposed profile and change summary, then apply.
- Gemini is the first low-volume synthesis provider; other providers can be added behind the same provider boundary later.

### Feedback fast loop: auto incremental profile apply

Status: Implemented in commit `894593b`.

Decision: Feedback submission should become the fast learning loop. After the user pastes a feedback blob in the Review Queue UI, Project Paper should store raw and structured feedback, then automatically run a Gemini incremental profile update after submit.

Completed behavior:

- Review Queue feedback submit stores raw and structured feedback, then runs Gemini incremental profile apply.
- Existing `profile_versions` rows remain; each successful apply creates a new profile version with a `change_summary`.
- Manual CLI dry-run/apply remains available for testing and operations.
- The profile stays compact and human-editable; raw feedback may grow, but the active profile remains compressed into durable preferences.
- Gemini is used for low-volume profile synthesis to split provider/token usage.
- Local Qwen remains focused on high-volume extraction and triage.
- ChatGPT remains focused on human paper discussion.

Full rebuild concept:

- Incremental apply is like an incremental build: current profile plus new feedback produces the next profile.
- Full rebuild is like a full build: all structured feedback regenerates a compact profile.
- Full rebuild is available as a manual path at first, roughly monthly, after about 10 new feedback items, or when recommendation quality obviously drifts.

Action item:

- Review profile quality after 10 feedback items, or earlier if recommendation quality shows a clear downward trend.

### Operating model: split roles

Decision: Project Paper work should be split across durable roles so product direction, implementation, operations, and planning do not blur together.

Role ownership:

- Architect thread owns product direction, workflow decisions, schema/agent boundaries, and deciding what should exist and why.
- Developer thread owns implementation, tests, commits/pushes, and small docs tied directly to code changes.
- DevOps/SRE thread owns Mac mini operations, cron/systemd, logs, health checks, backups, tunnels, and networking.
- Tech Doc Editor thread owns durable project memory: product changelog, decision log, roadmap cleanup, README/workflow/architecture consistency.
- SDLC thread owns milestones, release planning, issue breakdown, and definition of done.

### Architecture state: current MVP pipeline

Current state: The MVP pipeline is `ScoutAgent -> CuratorAgent -> ReviewerAgent`, orchestrated by `pipeline-daily`.

Responsibilities:

- Scout retrieves candidates and stores metadata/history.
- Curator scores candidates and creates recommendations.
- Reviewer downloads PDFs, runs local triage extraction, and stores artifacts.
- Feedback Agent and V2 feedback ingestion are the next workflow area, but the immediate product focus is the Review Queue UI redesign for the feedback blob path.

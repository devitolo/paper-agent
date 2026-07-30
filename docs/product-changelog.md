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

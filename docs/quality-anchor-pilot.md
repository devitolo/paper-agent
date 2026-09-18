# Manual primary-evidence quality pilot

This development diagnostic uses the existing transferred packet at
`/tmp/quality-anchor-pilot`. It does not implement automated PDF selection or
change production ranking. The developer selected excerpts after seeing prior
reviews; results are not blind or held-out evidence. Both prompts and evidence
differ from the earlier abstract test, so differences cannot be attributed solely
to extraction.

Use the repository runner, not the old `run_pilot.py` in the packet. No new ZIP is
needed. From the repository on the Mini:

```bash
git pull --ff-only
python3 -m paper_agents.quality_anchor_pilot --model gemini-3.1-flash-lite --check
```

This explicitly requests the model already used by the project's Gemini fallback.
Availability still depends on the signed-in account. If the connectivity report
says `connectivity_ok`, run only the first paper:

```bash
python3 -m paper_agents.quality_anchor_pilot --model gemini-3.1-flash-lite --paper crystallization --run
```

Review the printed results directory before running TELLER. `--paper teller`
selects that paper, while `--paper both` runs serially and stops on any failure.
Omit `--run` for local hash, exact-slice, and input-budget validation only.

Limits: one connectivity call with a 30-second deadline; 180 seconds per paper;
at most two serial paper calls; 7000 selected characters, 2000 per window, eight
windows; 8 MiB combined CLI output. Cleanup uses TERM followed by KILL for the
process group with bounded grace periods. This avoids shell `timeout` waiting
indefinitely on a TERM-resistant process. It cannot cancel computation already
accepted by a remote provider.

The wrapper never retries or changes models. Gemini CLI can retry internally;
the pilot aborts when stderr reports a recognized 429/5xx, quota, or high-demand
failure and records allowlisted status/category/retry hints without retaining
arbitrary stderr. Unrecognized stalls still hit the wall deadline. This behavior
is opt-in for the pilot; other Gemini callers retain their prior retry behavior.

Reports distinguish provider failures from response/citation validation failures.
Completed raw responses are saved for local inspection; exact citations are checked
against supplied excerpts. Exact matches do not establish semantic support. The
requested model is recorded; resolved model and token usage remain unknown because
the current transport does not expose them. Calls use the invoking working
directory and existing Gemini configuration. No database is accessed.

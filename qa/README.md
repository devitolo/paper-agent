# Production QA snapshot

On the Mini, after pulling this commit:

```bash
python3 qa/collect_prod_snapshot.py
```

Send the printed `.tar.gz` bundle to the tester. Output goes into ignored
`qa/bundles/` with private file permissions. Python 3 is the only required runtime;
missing logs, git, or cron are recorded as unavailable rather than aborting.

The script opens the source database read-only and uses SQLite's backup API for
a consistent snapshot, including committed WAL data. It checks snapshot integrity,
captures the commit and working-tree status, installed cron, timestamp/timezone,
and the last 200 lines of each source pipeline log. It does not run agents, call
source APIs, apply profiles, or change production rows or cron. Backup waiting is
bounded to approximately two minutes. Failed collection may leave a partial folder;
only a successful collection produces a bundle.

The bundle contains saved feedback and paper data. Inspect logs and cron for
credentials before sharing. `.env` and shell configuration files are not collected.

Optional paths:

```bash
python3 qa/collect_prod_snapshot.py --db /path/to/paper_agent.db --output-dir /tmp/qa-output
```

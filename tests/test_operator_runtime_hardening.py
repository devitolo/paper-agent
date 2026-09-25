from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class OperatorRuntimeHardeningTests(unittest.TestCase):
    def test_scheduled_wrappers_do_not_self_update_unless_opted_in(self) -> None:
        for script_name in ("nightly_pipeline.sh", "openalex_pipeline.sh"):
            with self.subTest(script=script_name):
                script = (ROOT / "scripts" / script_name).read_text(encoding="utf-8")
                self.assertIn('PAPER_AGENT_SELF_UPDATE:-0', script)
                self.assertIn('== "1"', script)
                self.assertIn("git pull --ff-only", script)
                self.assertLess(script.index('PAPER_AGENT_SELF_UPDATE:-0'), script.index("git pull --ff-only"))

    def test_cron_managed_wrappers_take_distinct_nonblocking_locks(self) -> None:
        expected_locks = {
            "nightly_pipeline.sh": "project-paper-arxiv.lock",
            "openalex_pipeline.sh": "project-paper-openalex.lock",
            "semantic_scholar_pipeline.sh": "project-paper-semantic-scholar.lock",
            "backup_db.sh": "project-paper-backup.lock",
            "biweekly_profile_rebuild_compare.sh": "project-paper-profile-rebuild.lock",
        }
        seen: set[str] = set()
        for script_name, lock_name in expected_locks.items():
            with self.subTest(script=script_name):
                script = (ROOT / "scripts" / script_name).read_text(encoding="utf-8")
                self.assertIn('LOCK_DIR="${PAPER_AGENT_LOCK_DIR:-/tmp}"', script)
                self.assertIn(lock_name, script)
                self.assertIn("flock -n", script)
                self.assertIn("exit 0", script)
                self.assertIn("another run holds", script)
                self.assertNotIn(lock_name, seen)
                seen.add(lock_name)

    def test_cron_template_uses_home_paths_and_failure_chaining(self) -> None:
        template = (ROOT / "deploy" / "project-paper.crontab").read_text(encoding="utf-8")
        self.assertNotIn("/home/devitolo/workspace/paper-agent", template)
        self.assertIn('$HOME/workspace/paper-agent', template)
        for line in template.splitlines():
            if not line or line.startswith("#") or "=" in line and not line[0].isdigit():
                continue
            with self.subTest(line=line):
                self.assertIn(" && ", line)
                self.assertNotIn("; cd ", line)
                self.assertNotIn("; scripts/", line)

    def test_web_systemd_unit_and_docs_cover_native_supervision(self) -> None:
        unit = (ROOT / "deploy" / "systemd" / "project-paper-web.service").read_text(encoding="utf-8")
        self.assertIn("WorkingDirectory=%h/workspace/paper-agent", unit)
        self.assertIn("EnvironmentFile=-%h/.config/project-paper/project-paper.env", unit)
        self.assertIn(
            "ExecStart=/usr/bin/env python3 -m paper_agents.cli web --host 127.0.0.1 --port 8000",
            unit,
        )
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("StandardOutput=journal", unit)
        self.assertIn("StandardError=journal", unit)

        docs = (ROOT / "docs" / "native-operations-history.md").read_text(encoding="utf-8")
        for snippet in (
            "mkdir -p ~/.config/systemd/user ~/.config/project-paper",
            "cp deploy/systemd/project-paper-web.service ~/.config/systemd/user/",
            "systemctl --user daemon-reload",
            "systemctl --user enable --now project-paper-web.service",
            'loginctl enable-linger "$USER"',
            "journalctl --user -u project-paper-web.service -f",
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, docs)


if __name__ == "__main__":
    unittest.main()

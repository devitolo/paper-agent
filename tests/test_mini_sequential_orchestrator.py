from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class SequentialOrchestratorTests(unittest.TestCase):
    def test_single_authoritative_cron_and_ordered_stages(self):
        cron = (ROOT / 'deploy/project-paper.crontab').read_text()
        script = (ROOT / 'scripts/mini_sequential_orchestrator.sh').read_text()
        self.assertEqual(cron.count('scripts/mini_sequential_orchestrator.sh'), 1)
        self.assertLess(script.index('run_stage sync'), script.index('for stage in openalex arxiv semantic core profile'))
        self.assertIn('flock -n 9', script)
        self.assertIn('sync failed; discovery and profile stages were not run', script)
        self.assertIn('profile', script)

    def test_no_independent_source_or_profile_cron_entries(self):
        cron = (ROOT / 'deploy/project-paper.crontab').read_text()
        active = '\n'.join(line for line in cron.splitlines() if line.strip() and not line.lstrip().startswith('#'))
        for job in (' arxiv ', ' openalex ', ' semantic ', ' core ', ' profile '):
            self.assertNotIn('mini_container_job.sh' + job, active)


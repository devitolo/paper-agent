from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class SequentialOrchestratorTests(unittest.TestCase):
    def test_per_source_cron_uses_exchange_wrapper_and_profile_stays_separate(self):
        cron = (ROOT / 'deploy/project-paper.crontab').read_text()
        script = (ROOT / 'scripts/mini_sequential_orchestrator.sh').read_text()
        self.assertEqual(cron.count('scripts/mini_sequential_orchestrator.sh'), 0)
        self.assertIn('for stage in openalex arxiv semantic core profile', script)
        for source in ('core', 'openalex', 'arxiv', 'semantic', 'zenml'):
            self.assertIn(f'scripts/mini_container_job.sh {source}', cron)
        self.assertIn('scripts/mini_container_job.sh profile', cron)
        self.assertIn('flock -n 9', script)
        self.assertIn('stage failed; later stages were not run', script)
        self.assertIn('PAPER_AGENT_SYNC_PRE_RUN_COMMAND', script)
        self.assertIn('PAPER_AGENT_SYNC_POST_RUN_COMMAND', script)
        self.assertIn('ORCHESTRATOR_LOG_DIR', script)
        self.assertIn('profile', script)

    def test_zenml_and_profile_remain_independent_from_sequential_wrapper(self):
        cron = (ROOT / 'deploy/project-paper.crontab').read_text()
        active = '\n'.join(line for line in cron.splitlines() if line.strip() and not line.lstrip().startswith('#'))
        self.assertNotIn('scripts/mini_sequential_orchestrator.sh', active)
        self.assertIn('mini_container_job.sh zenml', active)
        self.assertIn('mini_container_job.sh profile', active)


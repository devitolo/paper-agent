import sqlite3
import tempfile
import unittest
from pathlib import Path
from paper_agents import import_state

class SyncSchemaMigrationTests(unittest.TestCase):
    def test_sync_tables_are_an_approved_additive_delta(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'data').mkdir(); (root / 'config').mkdir()
            (root / 'data/profile.json').write_text('{"interests":[],"positive_signals":[],"negative_signals":[],"feedback_history":[]}')
            (root / 'config/topics.yaml').write_text('topics:\n  - id: test\n    label: Test\n    query: test\n    sources: [arxiv]\n')
            baseline = Path('sql/schema.sql').read_text().split(import_state.SYNC_SCHEMA_MARKER, 1)[0]
            with sqlite3.connect(root / 'data/paper_agent.db') as connection:
                connection.executescript(baseline)
                self.assertIn(import_state.schema(connection), import_state.pre_sync_schemas())
            self.assertTrue(import_state.apply_approved_post_import_schema_deltas(root))
            with sqlite3.connect(root / 'data/paper_agent.db') as connection:
                names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn('sync_result_batches', names)
                self.assertIn('sync_profile_proposals', names)
                self.assertIn(import_state.schema(connection), import_state.trusted_schemas())


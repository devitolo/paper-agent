import shutil
import sqlite3
import tempfile
from pathlib import Path
import unittest
from paper_agents import import_state, db

ROOT=Path(__file__).resolve().parents[1]

class LegacySchemaTests(unittest.TestCase):
    def test_historical_schema_is_read_only_and_unknown_schema_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/'data').mkdir(); (root/'config').mkdir()
            shutil.copy(ROOT/'deploy/templates/profile.json',root/'data/profile.json')
            (root/'config/topics.yaml').write_text('topics:\n  - id: synthetic\n    label: Synthetic\n    query: synthetic\n    sources: [arxiv]\n    enabled: true\n')
            path=root/'data/paper_agent.db'
            con=sqlite3.connect(path)
            con.executescript((ROOT/'sql/schema.sql').read_text().replace('score REAL,','score INTEGER,'))
            db.migrate_structured_feedback_score_to_real(con); con.close()
            before=path.read_bytes()
            import_state.manifest(root)
            self.assertEqual(path.read_bytes(),before)
            con=sqlite3.connect(path); con.execute('CREATE TABLE unexpected(value TEXT)'); con.close()
            with self.assertRaisesRegex(RuntimeError,'schema incompatible'): import_state.manifest(root)

    def test_only_known_table_rendering_differs(self):
        fresh,legacy=import_state.trusted_schemas()
        differences=[(a,b) for a,b in zip(fresh,legacy) if a!=b]
        self.assertEqual(len(fresh),len(legacy)); self.assertEqual(len(differences),1)
        self.assertEqual(differences[0][0][:3],('table','structured_feedback','structured_feedback'))

if __name__=='__main__': unittest.main()

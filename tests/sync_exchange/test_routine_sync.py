import tempfile
import json
import unittest
from pathlib import Path

from paper_agents.db import init_db
from paper_agents.sync_exchange import apply_routine_snapshot, build_routine_snapshot, connect_db, register_routine_revision


class RoutineSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); (self.root / 'config').mkdir(); (self.root / 'data').mkdir()
        (self.root / 'config/topics.yaml').write_text('topics: []\n')
        (self.root / 'config/industry.json').write_text('{}')
        (self.root / 'data/profile.json').write_text('{}')
        self.db = self.root / 'data/paper_agent.db'; init_db(self.db)

    def test_allowlisted_roundtrip_preserves_identity_and_revision(self):
        with connect_db(self.db) as c:
            pid = c.execute("INSERT INTO papers(canonical_key,title) VALUES('arxiv:1','One')").lastrowid
            c.execute("INSERT INTO paper_sources(paper_id,source,source_id) VALUES(?,?,?)", (pid, 'arxiv', '1'))
            c.execute("INSERT INTO profile_versions(version,profile_json,active) VALUES(?,?,1)", (2, '{"interests":["ops"]}'))
        payload = build_routine_snapshot(self.db, self.root)
        self.assertEqual(payload['schema'], 2)
        self.assertEqual(payload['feedback_watermark'], 0)
        self.assertNotIn('sync_snapshot_revisions', str(payload))
        target = self.root / 'target'; (target / 'config').mkdir(parents=True); (target / 'data').mkdir()
        (target / 'config/topics.yaml').write_text('topics: []\n'); (target / 'config/industry.json').write_text('{}'); (target / 'data/profile.json').write_text('{}')
        target_db = target / 'data/paper_agent.db'; init_db(target_db)
        revision = apply_routine_snapshot(target_db, payload, target)
        self.assertEqual(revision, payload['revision'])
        with connect_db(target_db) as c:
            self.assertEqual(c.execute("SELECT canonical_key FROM papers").fetchone()[0], 'arxiv:1')
            self.assertEqual(c.execute("SELECT version FROM profile_versions WHERE active=1").fetchone()[0], 2)

    def test_routine_revision_registration_keeps_only_compact_marker(self):
        payload = build_routine_snapshot(self.db, self.root)
        register_routine_revision(self.db, payload)
        with connect_db(self.db) as connection:
            marker = json.loads(connection.execute(
                'SELECT content_json FROM sync_snapshot_revisions WHERE revision=?',
                (payload['revision'],)).fetchone()[0])
        self.assertEqual(marker['kind'], 'routine_revision')
        self.assertNotIn('papers', marker)


if __name__ == '__main__':
    unittest.main()

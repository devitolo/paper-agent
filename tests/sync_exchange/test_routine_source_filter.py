import tempfile
import unittest
from pathlib import Path

from paper_agents import sync_exchange as exchange
from paper_agents.db import init_db


class RoutineSourceFilterTests(unittest.TestCase):
    def test_unsupported_legacy_source_is_excluded_from_routine_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'config').mkdir(); (root / 'data').mkdir()
            (root / 'config/topics.yaml').write_text('topics: []\n')
            (root / 'config/industry.json').write_text('{}'); (root / 'data/profile.json').write_text('{}')
            db = root / 'data/paper_agent.db'; init_db(db)
            with exchange.connect_db(db) as c:
                paper = c.execute("INSERT INTO papers(canonical_key,title) VALUES('legacy','Legacy')").lastrowid
                c.execute("INSERT INTO paper_sources(paper_id,source,source_id) VALUES(?,?,?)", (paper, 'ssrn', '7265684'))
                c.execute("INSERT INTO paper_sources(paper_id,source,source_id) VALUES(?,?,?)", (paper, 'zenml', 'z-1'))
            snapshot = exchange.build_routine_snapshot(db, root)
            self.assertEqual([row['source'] for row in snapshot['paper_sources']], ['zenml'])
            self.assertEqual(snapshot['papers'][0]['canonical_key'], 'legacy')


if __name__ == '__main__':
    unittest.main()

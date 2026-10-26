import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_agents import sync_exchange as exchange
from paper_agents.db import init_db


class RoutineSnapshotConsistencyTests(unittest.TestCase):
    def test_config_update_during_database_read_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').mkdir(); (root / 'data').mkdir()
            (root / 'config/topics.yaml').write_text('topics: []\n')
            (root / 'config/industry.json').write_text('{}')
            (root / 'data/profile.json').write_text('{}')
            db = root / 'data/paper_agent.db'; init_db(db)
            original = exchange._rows; changed = False

            def rows(connection, table):
                nonlocal changed
                result = original(connection, table)
                if not changed:
                    changed = True
                    (root / 'config/industry.json').write_text('{"changed": true}')
                return result

            with patch.object(exchange, '_rows', rows), self.assertRaisesRegex(ValueError, 'configuration changed'):
                exchange.build_routine_snapshot(db, root)


if __name__ == '__main__':
    unittest.main()

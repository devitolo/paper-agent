import tempfile
import threading
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

    def test_concurrent_wal_writer_cannot_mix_paper_profile_and_watermark(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'config').mkdir(); (root / 'data').mkdir()
            (root / 'config/topics.yaml').write_text('topics: []\n')
            (root / 'config/industry.json').write_text('{}'); (root / 'data/profile.json').write_text('{}')
            db = root / 'data/paper_agent.db'; init_db(db)
            import sqlite3
            with sqlite3.connect(db) as c:
                c.execute('PRAGMA journal_mode=WAL')
                c.execute("INSERT INTO papers(canonical_key,title) VALUES('k','old paper')")
                c.execute("INSERT INTO profile_versions(version,profile_json,active) VALUES(1,'{\"name\":\"old\"}',1)")
                c.commit()
            writer_ready = threading.Event(); writer_done = threading.Event()

            def writer():
                writer_ready.wait(5)
                with sqlite3.connect(db) as c:
                    c.execute("UPDATE papers SET title='new paper' WHERE canonical_key='k'")
                    c.execute("UPDATE profile_versions SET profile_json='{\"name\":\"new\"}' WHERE version=1")
                    c.execute("INSERT INTO raw_feedback(content,content_hash) VALUES('new feedback','writer-feedback')")
                    raw_id = c.execute('SELECT id FROM raw_feedback WHERE content_hash=?', ('writer-feedback',)).fetchone()[0]
                    attempt = c.execute("INSERT INTO feedback_parse_attempts(raw_feedback_id,parser_name,parser_version,status) VALUES(?,?,?,?)", (raw_id, 'test', '1', 'succeeded')).lastrowid
                    c.execute("INSERT INTO structured_feedback(parse_attempt_id,decision) VALUES(?,?)", (attempt, 'saved'))
                    c.commit()
                writer_done.set()

            original = exchange._rows
            def rows(connection, table):
                result = original(connection, table)
                if table == 'papers': writer_ready.set(); self.assertTrue(writer_done.wait(5))
                return result
            thread = threading.Thread(target=writer); thread.start()
            try:
                with patch.object(exchange, '_rows', rows):
                    snapshot = exchange.build_routine_snapshot(db, root)
            finally:
                thread.join(5)
            self.assertEqual(snapshot['papers'][0]['title'], 'old paper')
            self.assertEqual(snapshot['profile']['profile_json'], '{"name":"old"}')
            self.assertEqual(snapshot['feedback_watermark'], 0)


if __name__ == '__main__':
    unittest.main()

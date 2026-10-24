import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from paper_agents.sync_backup import create_backup
from paper_agents.whole_cycle_exchange import pull


class RevisionlessPullTests(unittest.TestCase):
    def test_revisionless_backup_is_rejected_before_db_replacement(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            mini = root / "mini"
            (mini / "config").mkdir(parents=True)
            (mini / "data").mkdir()
            (mini / "data/profile.json").write_text("{}")
            (mini / "config/topics.yaml").write_text("topics: []")
            (mini / "config/industry.json").write_text("{}")
            db = mini / "data/paper_agent.db"
            with sqlite3.connect(db) as connection:
                connection.execute("CREATE TABLE marker(value TEXT)")
                connection.execute("INSERT INTO marker VALUES ('before')")
            before_bytes = db.read_bytes()
            backup = create_backup(db, mini / "config", root / "backups", root=mini)
            manifest_path = backup / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest.pop("snapshot_revision", None)
            manifest_path.write_text(json.dumps(manifest, sort_keys=True))
            state = mini / "data/sync-state"
            with self.assertRaisesRegex(ValueError, "canonical snapshot_revision"):
                pull(db, mini, state, backup)
            self.assertEqual(db.read_bytes(), before_bytes)
            with sqlite3.connect(db) as connection:
                self.assertEqual(connection.execute("SELECT value FROM marker").fetchone()[0], "before")
            self.assertFalse((state / "active-cycle.json").exists())
            self.assertFalse(list(state.glob("mini-before-*.db")) if state.exists() else False)


if __name__ == "__main__":
    unittest.main()

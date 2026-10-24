import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_agents.db import init_db
from paper_agents import sync_backup
from paper_agents.sync_backup import create_backup
from paper_agents.sync_exchange import build_input_snapshot, connect_db, register_backup_snapshot


class BackupRevisionRaceTests(unittest.TestCase):
    def test_revision_matches_frozen_archive_when_feedback_changes_during_copy(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            app = root / "app"
            (app / "config").mkdir(parents=True)
            (app / "data").mkdir()
            (app / "config/topics.yaml").write_text("topics: []")
            (app / "config/industry.json").write_text("{}")
            (app / "data/profile.json").write_text(
                '{"interests":[],"positive_signals":[],"negative_signals":[],"feedback_history":[]}'
            )
            db = app / "data/paper_agent.db"
            init_db(db)
            fired = False
            original_copy2 = sync_backup.shutil.copy2

            def copy2(source, target, *args, **kwargs):
                nonlocal fired
                result = original_copy2(source, target, *args, **kwargs)
                if not fired and Path(source).name == "topics.yaml":
                    fired = True
                    with connect_db(db) as connection:
                        connection.execute("INSERT INTO raw_feedback(content,content_hash) VALUES('during backup','during-backup-hash')")
                return result

            with patch.object(sync_backup.shutil, "copy2", side_effect=copy2):
                backup = create_backup(db, app / "config", root / "backups", root=app)

            self.assertTrue(fired)
            manifest = json.loads((backup / "manifest.json").read_text())
            archived_revision = build_input_snapshot(backup / "paper_agent.db", backup)["revision"]
            live_revision = build_input_snapshot(db, app)["revision"]
            self.assertEqual(manifest["snapshot_revision"], archived_revision)
            self.assertNotEqual(manifest["snapshot_revision"], live_revision)
            registered = register_backup_snapshot(db, backup)
            self.assertEqual(registered, manifest["snapshot_revision"])
            with sqlite3.connect(db) as connection:
                self.assertIsNotNone(connection.execute(
                    "SELECT 1 FROM sync_snapshot_revisions WHERE revision=?",
                    (manifest["snapshot_revision"],),
                ).fetchone())


if __name__ == "__main__":
    unittest.main()

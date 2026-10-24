import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path

from paper_agents.sync_backup import create_backup, publish_archive


class BackupGuardTests(unittest.TestCase):
    def test_sync_backup_rejects_database_without_canonical_schema(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "data").mkdir()
            (root / "config/topics.yaml").write_text("topics: []")
            (root / "config/industry.json").write_text("{}")
            (root / "data/profile.json").write_text("{}")
            db = root / "data/paper_agent.db"
            with sqlite3.connect(db) as connection:
                connection.execute("CREATE TABLE standalone_backup(value TEXT)")
            with self.assertRaisesRegex(ValueError, "canonical Project Paper schema"):
                create_backup(db, root / "config", root / "backups", root=root, require_canonical=True)
            self.assertFalse(list((root / "backups").glob("backup-*")))

    def test_archive_is_published_only_after_complete_write(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            backup = root / "backup"
            backup.mkdir()
            (backup / "manifest.json").write_text("{}")
            archive = root / "state.tar.gz"
            result = publish_archive(backup, archive)
            self.assertEqual(result, archive)
            self.assertTrue(archive.is_file())
            self.assertFalse((root / "state.tar.gz.partial").exists())
            with tarfile.open(archive, "r:gz") as stream:
                self.assertEqual(stream.getnames(), ["manifest.json"])


if __name__ == "__main__":
    unittest.main()

from contextlib import closing
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from qa.collect_prod_snapshot import collect


class SnapshotTests(unittest.TestCase):
    def test_snapshot_includes_committed_wal_rows_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "production.db"
            with closing(sqlite3.connect(source)) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE feedback (content TEXT)")
                connection.execute("INSERT INTO feedback VALUES ('saved feedback')")
                connection.commit()
                (root / "logs").mkdir()
                (root / "logs/pipeline-daily.log").write_text(
                    "".join(f"line {i}\n" for i in range(250)))
                (root / ".env").write_text("DO_NOT_COLLECT=secret")
                with patch("qa.collect_prod_snapshot.command_output", return_value="unavailable\n"):
                    archive = collect(root, source, root / "bundles")
                with tarfile.open(archive) as bundle:
                    names = bundle.getnames()
                    self.assertFalse(any(name.endswith(".env") for name in names))
                    log = next(name for name in names if name.endswith("pipeline-daily.log"))
                    self.assertEqual(bundle.extractfile(log).read().decode().splitlines(),
                                     [f"line {i}" for i in range(50, 250)])
                    missing = next(name for name in names if name.endswith("pipeline-openalex.log"))
                    self.assertIn(b"unavailable", bundle.extractfile(missing).read())
                snapshot = next((root / "bundles").glob("*/paper-agent-qa.db"))
                with closing(sqlite3.connect(snapshot)) as copied:
                    self.assertEqual(copied.execute("SELECT * FROM feedback").fetchall(),
                                     [("saved feedback",)])
                self.assertEqual(connection.execute("SELECT * FROM feedback").fetchall(),
                                 [("saved feedback",)])
                self.assertEqual(archive.stat().st_mode & 0o077, 0)

    def test_missing_database_fails_without_creating_database_or_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, "Database not found"):
                collect(root, root / "missing.db", root / "bundles")
            self.assertEqual(list(root.iterdir()), [])

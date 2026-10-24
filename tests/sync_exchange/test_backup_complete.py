import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from paper_agents.sync_backup import create_backup, restore_backup, validate_backup

class BackupCompleteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config'; self.config.mkdir()
        (self.config / 'topics.yaml').write_text('topics: []')
        (self.config / 'industry.json').write_text('{}')
        (self.root / 'data').mkdir()
        (self.root / 'data/profile.json').write_text('{}')
        self.db = self.root / 'data/paper_agent.db'
        with sqlite3.connect(self.db) as conn:
            conn.execute('CREATE TABLE artifacts (artifact_type TEXT,path TEXT)')
            conn.execute("INSERT INTO artifacts VALUES ('triage_summary','data/summary.json')")
        (self.root / 'data/summary.json').write_text('{"summary":"useful"}')
        (self.root / 'data/huge.pdf').write_bytes(b'pdf')
    def backup(self):
        return create_backup(self.db, self.config, self.root / 'backups')
    def test_round_trip_and_excludes_pdf(self):
        backup = self.backup()
        self.assertTrue(validate_backup(backup))
        self.assertFalse((backup / 'data/huge.pdf').exists())
        restore = restore_backup(backup, self.root / 'restored')
        self.assertTrue(validate_backup(restore))
        self.assertEqual((restore / 'data/summary.json').read_text(), '{"summary":"useful"}')
        with self.assertRaises(ValueError): restore_backup(backup, restore)
    def test_missing_inputs_and_escaping_artifacts_fail(self):
        (self.root / 'data/summary.json').unlink()
        with self.assertRaises(ValueError): self.backup()
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE artifacts SET path='../outside.json'")
        with self.assertRaises(ValueError): self.backup()
    def test_bad_manifest_missing_database_and_corruption(self):
        backup = self.backup()
        (backup / 'manifest.json').write_text('invalid')
        self.assertFalse(validate_backup(backup))
        backup = self.backup()
        manifest = json.loads((backup / 'manifest.json').read_text())
        manifest['files'].pop('paper_agent.db')
        (backup / 'manifest.json').write_text(json.dumps(manifest))
        self.assertFalse(validate_backup(backup))
        backup = self.backup()
        (backup / 'paper_agent.db').write_bytes(b'bad')
        self.assertFalse(validate_backup(backup))
    def test_invalid_json_even_with_updated_checksum(self):
        backup = self.backup()
        file = backup / 'config/industry.json'; file.write_text('bad')
        manifest = json.loads((backup / 'manifest.json').read_text())
        manifest['files']['config/industry.json'] = hashlib.sha256(file.read_bytes()).hexdigest()
        (backup / 'manifest.json').write_text(json.dumps(manifest))
        self.assertFalse(validate_backup(backup))
    def test_profile_reconstructed(self):
        (self.root / 'data/profile.json').unlink()
        with sqlite3.connect(self.db) as conn:
            conn.execute('CREATE TABLE profile_versions(version INTEGER,active INTEGER,profile_json TEXT)')
            conn.execute('INSERT INTO profile_versions VALUES (1,1,?)', ('{"interests":[]}',))
        backup = self.backup()
        self.assertEqual(json.loads((backup / 'data/profile.json').read_text()), {'interests': []})

    def test_snapshot_revision_is_derived_from_frozen_backup(self):
        backup = create_backup(self.db, self.config, self.root / 'backups')
        manifest = json.loads((backup / 'manifest.json').read_text())
        self.assertRegex(manifest['snapshot_revision'], r'^[0-9a-f]{64}$')
        self.assertTrue(validate_backup(backup))
        manifest['snapshot_revision'] = 'short'
        (backup / 'manifest.json').write_text(json.dumps(manifest))
        self.assertFalse(validate_backup(backup))
    def test_foreign_key_failure(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute('CREATE TABLE parent(id INTEGER PRIMARY KEY)')
            conn.execute('CREATE TABLE child(id INTEGER REFERENCES parent(id))')
            conn.execute('INSERT INTO child VALUES (99)')
        with self.assertRaises(ValueError): self.backup()

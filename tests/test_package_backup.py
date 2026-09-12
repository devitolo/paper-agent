import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
import tarfile
import io

spec = importlib.util.spec_from_file_location('archive', Path(__file__).resolve().parents[1] / 'scripts/package_backup/archive.py')
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)

class BackupTests(unittest.TestCase):
    def test_roundtrip_and_nonempty_refusal(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'source'
            (root / 'data').mkdir(parents=True)
            (root / 'config').mkdir()
            with sqlite3.connect(root / 'data/paper_agent.db') as db:
                db.execute('CREATE TABLE feedback(content TEXT)')
                db.execute("INSERT INTO feedback VALUES ('private notes')")
            (root / 'data/profile.json').write_text('{"interests": ["reliability"]}')
            (root / 'config/topics.yaml').write_text('topics:\n')
            (root / 'data/paper.pdf').write_bytes(b'pdf contents')
            output = Path(folder) / 'backup.tar.gz'
            archive.backup(root, output, 'image@sha256:test')
            saved = output.read_bytes()
            with self.assertRaises(ValueError):
                archive.backup(root, output, 'image@sha256:test')
            self.assertEqual(output.read_bytes(), saved)
            target = Path(folder) / 'target'
            archive.restore(target, output, 'image@sha256:test')
            for name in ('data/profile.json', 'config/topics.yaml', 'data/paper.pdf'):
                self.assertEqual((target / name).read_bytes(), (root / name).read_bytes())
            corrupt = Path(folder) / 'corrupt.tar.gz'
            corrupt.write_bytes(saved[:len(saved)//2])
            with self.assertRaises((EOFError, tarfile.TarError)):
                archive.restore(Path(folder) / 'corrupt-target', corrupt, 'image@sha256:test')
            self.assertFalse((Path(folder) / 'corrupt-target/data').exists())
            with sqlite3.connect(target / 'data/paper_agent.db') as db:
                self.assertEqual(db.execute('SELECT content FROM feedback').fetchone()[0], 'private notes')
            with self.assertRaises(ValueError):
                archive.restore(target, output, 'image@sha256:test')
            with self.assertRaises(ValueError):
                archive.restore(Path(folder) / 'wrong', output, 'different-image')

    def test_traversal_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bad = root / 'bad.tar.gz'
            with tarfile.open(bad, 'w:gz') as out:
                member = tarfile.TarInfo('../escaped')
                member.size = 1
                out.addfile(member, io.BytesIO(b'x'))
            with self.assertRaises(ValueError):
                archive.restore(root / 'target', bad, 'image')
            self.assertFalse((root / 'escaped').exists())

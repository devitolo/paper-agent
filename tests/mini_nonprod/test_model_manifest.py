import hashlib
from pathlib import Path
import tempfile
import unittest

from scripts.mini_nonprod_model_manifest import inspect


class ModelManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / 'onnx').mkdir()
        (self.root / 'onnx/model.onnx').write_bytes(b'synthetic-test-only')
        (self.root / 'tokenizer_config.json').write_text('{}')
        (self.root / 'tokenizer.json').write_text('{}')
        self.sha = hashlib.sha256(b'synthetic-test-only').hexdigest()

    def test_complete_manifest_does_not_change_files(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = inspect(self.root, self.sha)
        self.assertEqual(len(result['files']), 3)
        self.assertEqual(result['files']['onnx/model.onnx']['sha256'], self.sha)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_cli_default_rejects_synthetic_hash(self):
        with self.assertRaisesRegex(RuntimeError, 'ONNX identity'):
            inspect(self.root)

    def test_alias_rejected(self):
        (self.root / 'alias').symlink_to(self.root / 'tokenizer.json')
        with self.assertRaisesRegex(ValueError, 'alias'):
            inspect(self.root, self.sha)

    def test_oversized_tree_rejected_before_reading_content(self):
        with (self.root / 'oversized').open('wb') as stream:
            stream.truncate(1024 ** 3 + 1)
        with self.assertRaisesRegex(RuntimeError, 'size/count budget'):
            inspect(self.root, self.sha)


if __name__ == '__main__':
    unittest.main()

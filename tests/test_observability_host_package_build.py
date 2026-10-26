import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from scripts import build_observability_host_package as builder


class HostPackageBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'
        self.output = Path(self.temp.name) / 'output'
        for name in builder.FILES:
            path = self.root / name;path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'#!/bin/sh\n# host-package-pending-guard-v1\nexit 0\n')

    def test_manifest_binds_exact_normalized_archive(self):
        digest = builder.build(self.root, self.output, 'a'*40)
        manifest = json.loads((self.output/'manifest.json').read_bytes())
        self.assertEqual(manifest['target'], 'mini-production')
        self.assertEqual(manifest['source_sha'], 'a'*40)
        self.assertEqual(manifest['artifact_sha256'], digest)
        self.assertEqual(hashlib.sha256((self.output/'artifact.tar').read_bytes()).hexdigest(), digest)
        with tarfile.open(self.output/'artifact.tar') as archive:
            self.assertEqual(set(archive.getnames()), set(builder.FILES))
            for entry, member in zip(manifest['files'], archive):
                self.assertEqual((member.mode, member.uid, member.gid), (0o755, 0, 0))
                self.assertEqual(entry['sha256'], hashlib.sha256(archive.extractfile(member).read()).hexdigest())
        self.assertEqual(builder.build(self.root, self.output, 'b'*40), digest)

    def test_invalid_source_or_unguarded_content_rejected(self):
        with self.assertRaises(ValueError):builder.build(self.root, self.output, 'main')
        (self.root/builder.FILES[0]).write_bytes(b'unguarded')
        with self.assertRaises(ValueError):builder.build(self.root, self.output, 'a'*40)

    def test_symlink_source_rejected(self):
        path=self.root/builder.FILES[0];path.unlink();path.symlink_to(self.root/builder.FILES[1])
        with self.assertRaises(ValueError):builder.build(self.root, self.output, 'a'*40)

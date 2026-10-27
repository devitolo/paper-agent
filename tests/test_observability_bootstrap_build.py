"""Bootstrap prep never installs and cannot silently replace verifier trust."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts import build_observability_host_bootstrap as builder

class BootstrapBuildTests(unittest.TestCase):
    def test_hashes_bind_every_payload_and_repeat_output_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(__file__).resolve().parents[1];tmp=Path(directory)
            gh=tmp/'gh';gh.write_bytes(b'isolated fixture, not a verifier')
            with patch.object(builder,'GH_SHA256',hashlib.sha256(gh.read_bytes()).hexdigest()):
                result=builder.build(root,tmp/'bundle',gh)
                for name,digest in result['files'].items():
                    self.assertEqual(hashlib.sha256((tmp/'bundle'/name).read_bytes()).hexdigest(),digest)
                self.assertIn(result['files']['package-install.py'],(tmp/'bundle/bootstrap.py').read_text())
                with self.assertRaises(ValueError):builder.build(root,tmp/'bundle',gh)
    def test_wrong_binary_or_symlink_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp=Path(directory);gh=tmp/'gh';gh.write_bytes(b'untrusted')
            root=Path(__file__).resolve().parents[1]
            with self.assertRaises(ValueError):builder.build(root,tmp/'bundle',gh)
            self.assertFalse((tmp/'bundle').exists())
            linked=tmp/'linked';linked.symlink_to(gh)
            with self.assertRaises(ValueError):builder.build(root,tmp/'bundle',linked)

    def test_operator_command_hashes_the_same_isolated_code_it_executes(self):
        import shlex
        digest='a'*64
        command=builder.operator_command('/home/devitolo/observability-bootstrap-fixture',digest)
        argv=shlex.split(command)
        self.assertEqual(argv[:4],['sudo','/usr/bin/python3','-I','-c'])
        self.assertIn(digest,argv[4])
        self.assertIn("read(1048577)",argv[4])
        self.assertIn("exec(compile(b",argv[4])
        with self.assertRaises(ValueError):builder.operator_command('/tmp/elsewhere',digest)

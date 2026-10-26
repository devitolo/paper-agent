"""Root-package transaction checks; only disposable, explicitly opted-in containers."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

ISOLATED = (sys.platform == 'linux' and os.geteuid() == 0
            and os.environ.get('PAPER_OBSERVABILITY_QA_ISOLATED') == '1'
            and Path('/.dockerenv').exists()
            and Path('/qa-source/scripts/observability_host_package_install.py').is_file())


@unittest.skipUnless(ISOLATED, 'Only disposable root Linux QA container')
class HostPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('installer', '/qa-source/scripts/observability_host_package_install.py')
        cls.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.m)
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/qa-state')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        Path('/qa-state').chmod(0o755)
        self.control = self.root / self.m.CONTROL.lstrip('/')
        for folder in (self.control, self.root / 'run/lock', self.root / 'usr/local/sbin', self.root / 'etc/cron.d', self.root / self.m.INCOMING.lstrip('/'), (self.root / self.m.VERIFIER.lstrip('/')).parent):
            folder.mkdir(parents=True, exist_ok=True)
        self.previous = {}
        for destination in self.m.FILES.values():
            path = self.root / destination.lstrip('/')
            path.write_bytes(b'#!/bin/sh\necho previous\n');path.chmod(0o755)
            self.previous[path] = path.read_bytes()
        self.cron = self.root / self.m.CRON.lstrip('/')
        self.cron.write_text('*/15 * * * * root /usr/local/sbin/project-paper-observability-collect-publish\n')
        self.cron.chmod(0o644)
        self.previous[self.cron] = self.cron.read_bytes()
        verifier = self.root / self.m.VERIFIER.lstrip('/')
        verifier.write_text('#!'+sys.executable+'''
# Synthetic attestation boundary. Real gh crypto verification is release CI's job.
import hashlib,json,pathlib,sys
args=sys.argv[1:]; subject=pathlib.Path(args[2]);bundle=pathlib.Path(args[args.index('--bundle')+1])
claims=json.loads(bundle.read_bytes());digest=hashlib.sha256(subject.read_bytes()).hexdigest()
for flag,expected in [('--repo','devitolo/paper-agent'),('--source-ref','refs/tags/observability-mini'),('--source-digest','a'*40),('--signer-digest','a'*40),('--cert-oidc-issuer','https://token.actions.githubusercontent.com'),('--signer-workflow','devitolo/paper-agent/.github/workflows/observability-release.yml')]:
    assert args[args.index(flag)+1]==expected
assert '--deny-self-hosted-runners' in args
assert args[args.index('--cert-identity')+1]=='https://github.com/devitolo/paper-agent/.github/workflows/observability-release.yml@refs/tags/observability-mini'
assert args[args.index('--predicate-type')+1]=='https://slsa.dev/provenance/v1'
assert digest==claims[subject.name]
''');verifier.chmod(0o755)
        self.files = {name:b'#!/bin/sh\necho new\n' for name in self.m.FILES}
        self.now = time.time()

    def bundle(self, *, change=None, member_change=None):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w') as package:
            for name, body in self.files.items():
                item = tarfile.TarInfo(name);item.size=len(body);item.mode=0o755;item.uid=0;item.gid=0
                if member_change:member_change(item)
                package.addfile(item, io.BytesIO(body))
        archive = data.getvalue();digest=self.m.sha(archive)
        manifest={'schema':self.m.SCHEMA,'target':'mini-production','source_sha':'a'*40,'artifact_sha256':digest,'installer_version':1,'created_at':self.m.datetime.fromtimestamp(self.now,self.m.timezone.utc).isoformat(),'files':[{'path':name,'sha256':self.m.sha(body),'mode':'0755','owner':'root:root'} for name,body in self.files.items()]}
        if change:change(manifest)
        folder=self.root/self.m.INCOMING.lstrip('/')/digest
        folder.mkdir(exist_ok=True)
        (folder/'artifact.tar').write_bytes(archive)
        (folder/'manifest.json').write_text(json.dumps(manifest,sort_keys=True))
        (folder/'attestation.jsonl').write_text(json.dumps({'manifest.json':self.m.sha((folder/'manifest.json').read_bytes()),'artifact.tar':digest}))
        return digest,folder

    def install(self, digest, health=None):
        return self.m.install(self.root,digest,'a'*40,check_health=health or (lambda *_:None),now=self.now)

    def unchanged(self):
        for path,data in self.previous.items():self.assertEqual(path.read_bytes(),data)
        self.assertFalse((self.control/'HOST-PACKAGE-PENDING.json').exists())

    def test_signed_package_commits_with_receipt_and_keeps_exact_cron(self):
        digest,_=self.bundle();result=self.install(digest)
        self.assertEqual(result['result'],'ready')
        self.assertEqual(self.cron.read_bytes(),self.previous[self.cron])
        for name,destination in self.m.FILES.items():
            path=self.root/destination.lstrip('/')
            self.assertEqual(path.read_bytes(),self.files[name])
            self.assertEqual(path.stat().st_mode&0o777,0o755)
        self.assertTrue((self.control/'HOST-PACKAGE-RECEIPT.json').exists())
        self.assertFalse((self.control/'HOST-PACKAGE-PENDING.json').exists())

    def test_signature_tamper_rejected_before_mutation(self):
        digest,folder=self.bundle();(folder/'manifest.json').write_text('{}')
        with self.assertRaises(subprocess.CalledProcessError):self.install(digest)
        self.unchanged()

    def test_artifact_tamper_rejected_before_mutation(self):
        digest,folder=self.bundle();(folder/'artifact.tar').write_bytes(b'bad')
        with self.assertRaises(ValueError):self.install(digest)
        self.unchanged()

    def test_wrong_target_path_mode_owner_hash_and_schema_rejected(self):
        changes=[lambda m:m.update(target='aws-production'),lambda m:m.update(schema='other'),lambda m:m['files'][0].update(path='../../etc/passwd'),lambda m:m['files'][0].update(mode='04755'),lambda m:m['files'][0].update(owner='1000:1000'),lambda m:m['files'][0].update(sha256='b'*64)]
        for change in changes:
            digest,_=self.bundle(change=change)
            with self.assertRaises(ValueError):self.install(digest)
            self.unchanged()

    def test_archive_symlink_rejected(self):
        def change(member):member.type=tarfile.SYMTYPE;member.linkname='/etc/passwd';member.size=0
        digest,_=self.bundle(member_change=change)
        with self.assertRaises(ValueError):self.install(digest)
        self.unchanged()

    def test_archive_mode_rejected(self):
        digest,_=self.bundle(member_change=lambda m:setattr(m,'mode',0o777))
        with self.assertRaises(ValueError):self.install(digest)
        self.unchanged()

    def test_untrusted_verifier_rejected(self):
        digest,_=self.bundle()
        verifier=self.root/self.m.VERIFIER.lstrip('/')
        verifier.chmod(0o777)
        with self.assertRaises(ValueError):self.install(digest)
        self.unchanged()

    def test_future_and_expired_manifest_rejected(self):
        for offset in (301,-7*86400-1):
            digest,_=self.bundle(change=lambda m:m.update(created_at=self.m.datetime.fromtimestamp(self.now+offset,self.m.timezone.utc).isoformat()))
            with self.assertRaises(ValueError):self.install(digest)
            self.unchanged()

    def test_inbox_symlink_and_fifo_rejected(self):
        digest,folder=self.bundle();path=folder/'artifact.tar';path.unlink();path.symlink_to('/etc/passwd')
        with self.assertRaises(OSError):self.install(digest)
        path.unlink();os.mkfifo(path)
        with self.assertRaises(ValueError):self.install(digest)
        self.unchanged()

    def test_health_failure_restores_all_files(self):
        digest,_=self.bundle()
        def fail(*_):raise RuntimeError('qualification failed')
        with self.assertRaises(RuntimeError):self.install(digest,health=fail)
        self.unchanged()
        self.assertFalse((self.control/'HOST-PACKAGE-RECEIPT.json').exists())

    def test_interrupted_transaction_blocks_then_recovers(self):
        digest,_=self.bundle()
        def crash(*_):raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):self.install(digest,health=crash)
        with self.assertRaises(ValueError):self.install(digest)
        result=self.m.recover(self.root,check_health=lambda *_:None)
        self.assertEqual(result['result'],'recovered');self.unchanged()

    def test_corrupt_rollback_backup_refuses_recovery_and_keeps_pending(self):
        digest,_=self.bundle()
        def crash(*_):raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):self.install(digest,health=crash)
        pending=self.control/'HOST-PACKAGE-PENDING.json'
        audit=json.loads(pending.read_text())
        (self.control/audit['transaction']/'0.before').write_bytes(b'corrupt')
        with self.assertRaises(ValueError):self.m.recover(self.root,check_health=lambda *_:None)
        self.assertTrue(pending.exists())

    def test_older_release_rejected(self):
        digest,_=self.bundle();self.install(digest)
        digest,_=self.bundle(change=lambda m:m.update(created_at=self.m.datetime.fromtimestamp(self.now-1,self.m.timezone.utc).isoformat()))
        with self.assertRaises(ValueError):self.install(digest)

    def test_deploy_lock_prevents_any_changes(self):
        import fcntl
        digest,_=self.bundle()
        with (self.root/'run/lock/project-paper-observability-deploy.lock').open('w') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):self.install(digest)
        self.unchanged()

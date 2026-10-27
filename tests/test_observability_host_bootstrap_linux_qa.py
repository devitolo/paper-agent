"""Consolidated bootstrap tests, only in a disposable root Linux container."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ISOLATED=(os.environ.get('PAPER_OBSERVABILITY_QA_ISOLATED')=='1' and os.geteuid()==0
          and Path('/.dockerenv').exists() and Path('/qa-source/scripts/observability_host_bootstrap.py').exists())


@unittest.skipUnless(ISOLATED,'Disposable root Linux QA only')
class BootstrapTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('bootstrap','/qa-source/scripts/observability_host_bootstrap.py')
        self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m)
        Path('/qa-state').chmod(0o755)
        self.temp=tempfile.TemporaryDirectory(dir='/qa-state');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'root';self.root.mkdir()
        self.source=Path(self.temp.name)/'source';self.source.mkdir()
        self.control=self.root/self.m.CONTROL
        for path in (self.control,self.root/'run/lock',self.root/'usr/local/sbin',self.root/'etc/sudoers.d',self.root/'etc/cron.d'):
            path.mkdir(parents=True,exist_ok=True)
        self.hashes={};self.old={};self.before={}
        for name,(relative,mode) in self.m.FILES.items():
            data=('reviewed-new-'+name).encode();(self.source/name).write_bytes(data);self.hashes[name]=self.m.sha(data)
            if name in self.m.OLD_HASHES:
                destination=self.root/relative;destination.write_bytes(b'previous');destination.chmod(mode)
                self.old[name]=self.m.sha(b'previous');self.before[relative]=b'previous'
        self.m.SOURCE_HASHES=self.hashes;self.m.OLD_HASHES=self.old
        self.cron=self.root/'etc/cron.d/project-paper-observability-mini-production'
        self.cron.write_bytes(b'*/15 * * * * root /usr/local/sbin/project-paper-observability-collect-publish\n')
        self.cron_before=self.cron.read_bytes()

    def bootstrap(self):
        with patch.object(self.m.pwd,'getpwnam',return_value=type('Account',(),{'pw_gid':1000})()):
            return self.m.bootstrap(self.root,self.source,qualify=lambda *_:None)

    def assert_restored(self):
        for name,(relative,mode) in self.m.FILES.items():
            path=self.root/relative
            if relative in self.before:self.assertEqual(path.read_bytes(),self.before[relative])
            else:self.assertFalse(path.exists(),relative)
        self.assertEqual(self.cron.read_bytes(),self.cron_before)

    def test_complete_bootstrap_is_repeatable_and_keeps_cron(self):
        for _ in range(2):
            result=self.bootstrap();self.assertEqual(result['result'],'ready');self.assertFalse(result['jobs_run'])
            self.assertEqual(self.cron.read_bytes(),self.cron_before)
            inbox=self.root/self.m.INBOX
            self.assertEqual(inbox.stat().st_mode&0o7777,0o1770)
            self.assertEqual(inbox.stat().st_gid,1000)
            for name,(relative,mode) in self.m.FILES.items():
                path=self.root/relative;self.assertEqual(self.m.sha(path.read_bytes()),self.hashes[name]);self.assertEqual(path.stat().st_mode&0o777,mode)

    def test_tampered_source_fails_before_root_changes(self):
        (self.source/'gh').write_bytes(b'tamper')
        with self.assertRaises(ValueError):self.bootstrap()
        self.assert_restored()

    def test_changed_installed_helper_fails_before_replacement(self):
        helper=self.root/self.m.FILES['project-paper-observability-deploy'][0]
        helper.write_bytes(b'newer-unknown')
        with self.assertRaises(ValueError):self.bootstrap()
        self.assertEqual(helper.read_bytes(),b'newer-unknown')
        self.assertFalse((self.root/self.m.FILES['sudoers'][0]).exists())

    def test_install_error_rolls_back_every_file(self):
        original=self.m.atomic;called=False
        target=self.root/self.m.FILES['sudoers'][0]
        def fail(path,data,mode):
            nonlocal called
            original(path,data,mode)
            if path==target and not called:called=True;raise OSError('injected failure')
        with patch.object(self.m,'atomic',side_effect=fail):
            with self.assertRaises(OSError):self.bootstrap()
        self.assert_restored();self.assertFalse((self.control/'HOST-BOOTSTRAP-PENDING.json').exists())

    def test_crash_after_each_active_file_can_recover_and_complete(self):
        for name,(relative,_) in self.m.FILES.items():
            target=self.root/relative;original=self.m.atomic
            def crash(path,data,mode):
                original(path,data,mode)
                if path==target:raise KeyboardInterrupt()
            with patch.object(self.m,'atomic',side_effect=crash):
                with self.assertRaises(KeyboardInterrupt):self.bootstrap()
            self.assertTrue((self.control/'HOST-BOOTSTRAP-PENDING.json').exists())
            self.m.restore(self.root,self.control)
            self.assert_restored()
        self.assertEqual(self.bootstrap()['result'],'ready')

    def test_pending_package_and_symlink_sources_refused(self):
        marker=self.control/'HOST-PACKAGE-PENDING.json';marker.write_bytes(b'{}')
        with self.assertRaises(ValueError):self.bootstrap()
        marker.unlink();path=self.source/'gh';path.unlink();path.symlink_to('/etc/passwd')
        with self.assertRaises(OSError):self.bootstrap()
        self.assert_restored()

    def test_recovery_cannot_delete_a_required_previous_entrypoint(self):
        import json
        original=self.m.atomic
        target=self.root/self.m.FILES['sudoers'][0]
        def crash(path,data,mode):
            original(path,data,mode)
            if path==target:raise KeyboardInterrupt()
        with patch.object(self.m,'atomic',side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):self.bootstrap()
        pending=self.control/'HOST-BOOTSTRAP-PENDING.json'
        audit=json.loads(pending.read_text())
        relative=self.m.FILES['run-container.sh'][0]
        audit['before'][relative]={'mode':None,'sha256':None}
        pending.write_text(json.dumps(audit))
        with self.assertRaises(ValueError):self.m.restore(self.root,self.control)
        self.assertTrue((self.root/relative).exists())
        self.assertTrue(pending.exists())

    def test_runner_authority_installed_last(self):
        original=self.m.atomic;active=[]
        def record(path,data,mode):
            if path in [self.root/entry[0] for entry in self.m.FILES.values()]:active.append(path)
            original(path,data,mode)
        with patch.object(self.m,'atomic',side_effect=record):self.bootstrap()
        self.assertEqual(active[-1],self.root/self.m.FILES['sudoers'][0])

@unittest.skipUnless(ISOLATED,'Disposable root Linux QA only')
class BootstrapEntrypointCrashTests(BootstrapTests):
    def setUp(self):
        super().setUp()
        prefixes=('/usr/local/sbin','/var/lib/project-paper-observability','/etc/project-paper-observability','/run/lock')
        def remap(body):
            for prefix in prefixes:body=body.replace(prefix,str(self.root/prefix.lstrip('/')))
            return body.encode()
        for name in ('collect-publish.sh','project-paper-observability-deploy','run-container.sh'):
            old=Path('/qa-source/tests/fixtures/observability-bootstrap-old',name).read_bytes()
            # Fixtures are pinned to actual production versions, not reimplemented guards.
            self.assertEqual(self.m.sha(old),__import__('runpy').run_path('/qa-source/scripts/observability_host_bootstrap.py')['OLD_HASHES'][name])
            previous=remap(old.decode())
            destination=self.root/self.m.FILES[name][0];destination.write_bytes(previous)
            self.before[self.m.FILES[name][0]]=previous;self.old[name]=self.m.sha(previous)
            new=remap(Path('/qa-source/deploy/observability',name).read_text())
            (self.source/name).write_bytes(new);self.hashes[name]=self.m.sha(new)
        config=self.root/'etc/project-paper-observability/mini-production';config.mkdir(parents=True)
        state=self.root/'state';state.mkdir()
        (config/'mini-production.env').write_text('PROJECT_PAPER_RESOURCE_ENVIRONMENT=mini-production\nPROJECT_PAPER_OBSERVABILITY_IMAGE=ghcr.io/devitolo/paper-agent/observability:qa@sha256:'+'a'*64+'\nPROJECT_PAPER_RESOURCE_ROOT='+str(state)+'\nPROJECT_PAPER_RESOURCE_EXPORT_DIR='+str(state)+'\n')
        (config/'publisher.env').write_text('PAPER_OBSERVABILITY_PUBLISH_ENABLED=0\n')
        binary=self.root/'bin';binary.mkdir()
        (binary/'docker').write_text('#!/bin/sh\nif [ "$1" = run ]; then echo collect >> "$CALLS"; fi\nexit 0\n');(binary/'docker').chmod(0o755)
        self.calls=self.root/'calls'
        self.environment={'PATH':str(binary)+':'+os.environ['PATH'],'CALLS':str(self.calls)}

    def run_cron(self):
        import subprocess
        return subprocess.run(['/usr/bin/flock','-n',str(self.root/'run/lock/project-paper-observability.lock'),'/bin/bash',str(self.root/self.m.FILES['collect-publish.sh'][0])],env=self.environment,capture_output=True,text=True,timeout=5)

    def test_every_gate_and_payload_crash_has_only_old_or_blocked_pipeline(self):
        import subprocess
        targets={self.root/relative for relative,_ in self.m.FILES.values()}
        original=self.m.atomic
        for stop in range(1,9):
            count=0
            def crash(path,data,mode):
                nonlocal count
                original(path,data,mode)
                if path in targets:
                    count+=1
                    if count==stop:raise KeyboardInterrupt()
            self.calls.unlink(missing_ok=True)
            with patch.object(self.m,'atomic',side_effect=crash):
                with self.assertRaises(KeyboardInterrupt):self.bootstrap()
            cron=self.run_cron()
            if stop==1:
                self.assertEqual(cron.returncode,0,cron.stderr)
                self.assertEqual(self.calls.read_text().splitlines(),['collect'])
            else:
                self.assertEqual(cron.returncode,75,(stop,cron.stderr))
                self.assertFalse(self.calls.exists())
            helper=subprocess.run(['/bin/bash',str(self.root/self.m.FILES['project-paper-observability-deploy'][0]),'--image','ghcr.io/devitolo/paper-agent/observability:qa@sha256:'+'a'*64,'--target','mini-production'],env=self.environment,capture_output=True,text=True,timeout=5)
            self.assertEqual(helper.returncode,75,(stop,helper.stderr))
            self.m.restore(self.root,self.control);self.assert_restored()
            self.calls.unlink(missing_ok=True)
            self.assertEqual(self.run_cron().returncode,0)

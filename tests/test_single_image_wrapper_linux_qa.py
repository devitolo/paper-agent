"""Only run in a disposable root Linux container, never on a production host."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ISOLATED=(os.geteuid()==0 and os.environ.get('PAPER_OBSERVABILITY_QA_ISOLATED')=='1' and Path('/.dockerenv').exists() and Path('/qa-source/deploy/observability/install_single_image_wrapper.py').exists())


@unittest.skipUnless(ISOLATED,'Disposable root Linux container only')
class SingleImageWrapperTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('migrate','/qa-source/deploy/observability/install_single_image_wrapper.py');self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m)
        Path('/qa-state').chmod(0o755);self.temp=tempfile.TemporaryDirectory(dir='/qa-state');self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        for path in (self.root/self.m.CONTROL,self.root/'run/lock',(self.root/self.m.WRAPPER).parent,(self.root/self.m.ENV).parent,(self.root/self.m.CRON).parent):path.mkdir(parents=True,exist_ok=True)
        self.wrapper=self.root/self.m.WRAPPER;self.wrapper.write_bytes(b'old-wrapper');self.wrapper.chmod(0o755);self.m.OLD_SHA=self.m.digest(self.wrapper.read_bytes())
        self.source=self.root/'reviewed-wrapper';self.source.write_bytes(Path('/qa-source/deploy/observability/collect-publish.sh').read_bytes());self.assertEqual(self.m.digest(self.source.read_bytes()),self.m.NEW_SHA)
        self.env=self.root/self.m.ENV;self.env.write_text('PROJECT_PAPER_OBSERVABILITY_IMAGE=ghcr.io/devitolo/paper-agent/observability:qualified@sha256:'+'a'*64+'\n')
        self.cron=self.root/self.m.CRON;self.cron.write_text('*/15 * * * * root /usr/local/sbin/project-paper-observability-collect-publish\n')
        self.before_env=self.env.read_bytes();self.before_cron=self.cron.read_bytes()

    def install(self,check=None):return self.m.install(self.root,self.source,check_image=check or (lambda image:None))
    def preserved(self):self.assertEqual(self.env.read_bytes(),self.before_env);self.assertEqual(self.cron.read_bytes(),self.before_cron)

    def test_success_keeps_configuration_and_records_backup(self):
        result=self.install();self.assertEqual(result['result'],'ready');self.assertFalse(result['jobs_run']);self.preserved()
        self.assertEqual(self.wrapper.read_bytes(),self.source.read_bytes());self.assertEqual(Path(result['rollback_wrapper']).read_bytes(),b'old-wrapper')
        self.assertEqual(self.install()['result'],'already_ready')

    def test_unqualified_image_and_tampered_source_do_not_modify_wrapper(self):
        def fail(image):raise ValueError('publisher entrypoint missing')
        with self.assertRaises(ValueError):self.install(fail)
        self.assertEqual(self.wrapper.read_bytes(),b'old-wrapper');self.preserved()
        self.source.write_bytes(b'tamper')
        with self.assertRaises(ValueError):self.install()
        self.assertEqual(self.wrapper.read_bytes(),b'old-wrapper')

    def test_failed_verification_restores_single_file(self):
        original=self.m.atomic;failed=False
        def fail(path,data,mode):
            nonlocal failed
            original(path,data,mode)
            if path==self.wrapper and not failed:failed=True;raise OSError('injected')
        with patch.object(self.m,'atomic',side_effect=fail):
            with self.assertRaises(OSError):self.install()
        self.assertEqual(self.wrapper.read_bytes(),b'old-wrapper');self.preserved()

    def test_interruption_after_atomic_replace_leaves_complete_new_wrapper_and_backup(self):
        original=self.m.atomic
        def crash(path,data,mode):
            original(path,data,mode)
            if path==self.wrapper:raise KeyboardInterrupt()
        with patch.object(self.m,'atomic',side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):self.install()
        self.assertEqual(self.wrapper.read_bytes(),self.source.read_bytes());self.preserved()
        backups=list((self.root/self.m.CONTROL).glob('single-image-wrapper-*/wrapper.before'));self.assertEqual(len(backups),1);self.assertEqual(backups[0].read_bytes(),b'old-wrapper')
        self.assertEqual(self.install()['result'],'already_ready')

    def test_symlink_source_and_changed_existing_revision_refused(self):
        self.source.unlink();self.source.symlink_to(self.env)
        with self.assertRaises(OSError):self.install()
        self.source.unlink();self.source.write_bytes(Path('/qa-source/deploy/observability/collect-publish.sh').read_bytes())
        self.wrapper.write_bytes(b'unknown-new-version')
        with self.assertRaises(ValueError):self.install()
        self.assertEqual(self.wrapper.read_bytes(),b'unknown-new-version')

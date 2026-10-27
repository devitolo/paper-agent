"""Independent acceptance additions for the guarded Mini updater.

Requires a disposable root Linux container, read-only /qa-source and private
/qa-state tmpfs. Reuses only the historical fixture's setup/bundle generator;
the adversarial assertions below are independent. Synthetic verifier fixtures
do not qualify real cryptographic provenance or an effective sudo policy.
"""
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import unittest
from unittest.mock import patch


ISOLATED = (sys.platform == 'linux' and os.geteuid() == 0
    and os.environ.get('PAPER_OBSERVABILITY_QA_ISOLATED') == '1'
    and Path('/.dockerenv').exists()
    and Path('/qa-source/scripts/observability_host_package_install.py').is_file()
    and Path('/qa-state').is_dir())


@unittest.skipUnless(ISOLATED, 'Disposable root Linux container only')
class GuardedUpdaterAdversarialQA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_observability_host_package_linux_qa import HostPackageTests
        cls.fixture_type = HostPackageTests
        cls.fixture_type.setUpClass()

    def fixture(self):
        f = self.fixture_type('test_signed_package_commits_with_receipt_and_keeps_exact_cron')
        f.setUp()
        self.addCleanup(f.doCleanups)
        verifier = f.root/f.m.VERIFIER.lstrip('/')
        verifier.write_text(verifier.read_text().replace("'a'*40", "claims['source_sha']"))
        return f

    def release(self, f, *, source='a'*40, created=None, files=None):
        if files is not None:
            f.files = files
        created = f.now if created is None else created
        def change(manifest):
            manifest.update(source_sha=source,
                created_at=datetime.fromtimestamp(created, timezone.utc).isoformat())
        digest, folder = f.bundle(change=change)
        target = folder.with_name(digest+'-'+source)
        if target != folder:
            folder.rename(target)
        claims = json.loads((target/'attestation.jsonl').read_text())
        claims['source_sha'] = source
        (target/'attestation.jsonl').write_text(json.dumps(claims))
        return digest, target

    def install(self, f, digest, source='a'*40, health=None):
        import inspect
        options = {'now': f.now+100, 'check_health': health or (lambda *_: None)}
        # Parsing/transaction cases isolate the registry boundary. Separate v2
        # image tests below exercise preparation without pulling real images.
        if 'prepare' in inspect.signature(f.m.install).parameters:
            options['prepare'] = lambda *_: None
        return f.m.install(f.root, digest, source, **options)

    def resign_fixture(self, f, folder):
        claims = json.loads((folder/'attestation.jsonl').read_text())
        claims['manifest.json'] = f.m.sha((folder/'manifest.json').read_bytes())
        (folder/'attestation.jsonl').write_text(json.dumps(claims))

    def test_signed_archive_rejects_hardlinks_devices_fifos_and_noncanonical_paths(self):
        for kind in ('hardlink', 'character', 'block', 'fifo', 'absolute', 'parent'):
            with self.subTest(kind=kind):
                f = self.fixture()
                def mutate(member):
                    if kind == 'absolute': member.name = '/'+member.name
                    elif kind == 'parent': member.name = '../'+member.name
                    else:
                        member.type = {'hardlink':tarfile.LNKTYPE, 'character':tarfile.CHRTYPE,
                                       'block':tarfile.BLKTYPE, 'fifo':tarfile.FIFOTYPE}[kind]
                        member.linkname = '/etc/passwd'; member.size = 0
                digest, folder = f.bundle(member_change=mutate)
                claims = json.loads((folder/'attestation.jsonl').read_text())
                claims['source_sha'] = 'a'*40
                (folder/'attestation.jsonl').write_text(json.dumps(claims))
                with self.assertRaises(ValueError): self.install(f, digest)
                f.unchanged()

    def test_duplicate_signed_manifest_keys_rejected_before_mutation(self):
        f = self.fixture(); digest, folder = self.release(f)
        raw = (folder/'manifest.json').read_text()
        (folder/'manifest.json').write_text(raw.replace('{', '{"target":"mini-production",', 1))
        self.resign_fixture(f, folder)
        with self.assertRaises(ValueError): self.install(f, digest)
        f.unchanged()

    def test_archive_and_attestation_size_limits_fail_before_verifier(self):
        for name, size in (('artifact.tar', 1024*1024+1), ('attestation.jsonl', 524289), ('manifest.json',16385)):
            with self.subTest(name=name):
                f = self.fixture();digest,folder = self.release(f)
                (folder/name).write_bytes(b'x'*size)
                verifier = f.root/f.m.VERIFIER.lstrip('/')
                verifier.write_text('#!/bin/sh\ntouch '+str(f.root/'unexpected-verifier')+'\nexit 1\n')
                with self.assertRaises(ValueError): self.install(f, digest)
                self.assertFalse((f.root/'unexpected-verifier').exists())
                f.unchanged()

    def test_gzip_payload_is_not_decompressed_by_uncompressed_tar_contract(self):
        import gzip
        f = self.fixture();digest,folder = self.release(f)
        compressed = gzip.compress(b'0'*(4*1024*1024))
        new_digest = f.m.sha(compressed)
        new_folder = folder.with_name(new_digest+'-'+'a'*40);folder.rename(new_folder)
        (new_folder/'artifact.tar').write_bytes(compressed)
        manifest=json.loads((new_folder/'manifest.json').read_text());manifest['artifact_sha256']=new_digest
        (new_folder/'manifest.json').write_text(json.dumps(manifest))
        claims={'source_sha':'a'*40,'artifact.tar':new_digest,
                'manifest.json':f.m.sha((new_folder/'manifest.json').read_bytes())}
        (new_folder/'attestation.jsonl').write_text(json.dumps(claims))
        with self.assertRaises((ValueError, tarfile.ReadError)): self.install(f,new_digest)
        f.unchanged()

    def test_signed_different_package_with_equal_release_time_requires_explicit_recovery(self):
        f=self.fixture();first,_=self.release(f);self.install(f,first)
        previous={name:(f.root/dest.lstrip('/')).read_bytes() for name,dest in f.m.FILES.items()}
        changed={name:body+b'# different approved release\n' for name,body in f.files.items()}
        if hasattr(f,'new_image'):
            f.new_image=f.new_image.rsplit(':',1)[0]+':'+'d'*64
        second,_=self.release(f,source='b'*40,files=changed)
        with self.assertRaises(ValueError): self.install(f,second,'b'*40)
        for name,dest in f.m.FILES.items():self.assertEqual((f.root/dest.lstrip('/')).read_bytes(),previous[name])

    def test_noop_newer_approved_release_advances_replay_protection(self):
        f=self.fixture();old_files=dict(f.files)
        first,_=self.release(f);self.install(f,first)
        new_files={name:body+b'# version two\n' for name,body in old_files.items()}
        if hasattr(f,'new_image'):
            f.new_image=f.new_image.rsplit(':',1)[0]+':'+'d'*64
        second,staged=self.release(f,source='b'*40,created=f.now+10,files=new_files);self.install(f,second,'b'*40)
        # Successful CI removes its own incoming bundle. Reaffirm B through a
        # later approved run of the SAME source/image, keeping OCI revision
        # binding valid instead of reusing one image for different commits.
        import shutil
        shutil.rmtree(staged)
        reaffirm,_=self.release(f,source='b'*40,created=f.now+20,files=new_files)
        self.install(f,reaffirm,'b'*40,health=lambda *_:self.fail('Unchanged scripts/image must not force qualification'))
        if hasattr(f,'new_image'):
            f.new_image=f.new_image.rsplit(':',1)[0]+':'+'e'*64
        stale,_=self.release(f,source='d'*40,created=f.now+15,files=old_files)
        with self.assertRaises(ValueError):self.install(f,stale,'d'*40)

    def test_publisher_pending_and_business_state_survive_install_failure_and_recovery(self):
        for mode in ('success','failure','interruption'):
            with self.subTest(mode=mode):
                f=self.fixture();digest,_=self.release(f)
                protected=[]
                for relative in ('var/lib/project-paper-observability/mini-production-publisher/pending.json',
                                 'var/lib/project-paper-observability/mini-production-publisher/receipt.json',
                                 'etc/project-paper-observability/mini-production/transport.key',
                                 'research/data/paper_agent.db','research/config/profile.json'):
                    path=f.root/relative;path.parent.mkdir(parents=True,exist_ok=True)
                    path.write_bytes(b'QA synthetic protected bytes');path.chmod(0o600)
                    protected.append((path,path.read_bytes(),path.stat().st_mode))
                def health(*_):
                    if mode=='failure':raise RuntimeError('qualification failed')
                    if mode=='interruption':raise KeyboardInterrupt()
                if mode=='success':self.install(f,digest,health=health)
                else:
                    with self.assertRaises(RuntimeError if mode=='failure' else KeyboardInterrupt):self.install(f,digest,health=health)
                    if mode=='interruption':f.m.recover(f.root,check_health=lambda *_:None)
                for path,data,permissions in protected:
                    self.assertEqual(path.read_bytes(),data)
                    self.assertEqual(path.stat().st_mode,permissions)

    def test_verifier_timeout_leaves_installed_files_and_pending_untouched(self):
        import subprocess
        f=self.fixture();digest,_=self.release(f)
        with patch.object(f.m.subprocess,'run',side_effect=subprocess.TimeoutExpired('synthetic verifier',45)):
            with self.assertRaises(subprocess.TimeoutExpired):self.install(f,digest)
        f.unchanged()

    def test_verifier_receives_no_runner_credentials_proxy_or_python_configuration(self):
        f=self.fixture();digest,_=self.release(f)
        verifier=f.root/f.m.VERIFIER.lstrip('/')
        check="import os\nassert not any(os.environ.get(k) for k in ('GH_TOKEN','GITHUB_TOKEN','HTTPS_PROXY','HTTP_PROXY','PYTHONPATH','SSL_CERT_FILE'))\n"
        verifier.write_text(verifier.read_text().replace('args=sys.argv[1:]',check+'args=sys.argv[1:]',1))
        with patch.dict(os.environ,{'GH_TOKEN':'QA-SYNTHETIC','GITHUB_TOKEN':'QA-SYNTHETIC',
             'HTTPS_PROXY':'http://qa.invalid','HTTP_PROXY':'http://qa.invalid',
             'PYTHONPATH':'/qa-untrusted','SSL_CERT_FILE':'/qa-untrusted'}):
            self.assertEqual(self.install(f,digest)['result'],'ready')

    def test_corrupt_late_backup_is_detected_before_any_recovery_replacement(self):
        f=self.fixture();digest,_=self.release(f)
        def crash(*_):raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):self.install(f,digest,health=crash)
        pending=f.control/'HOST-PACKAGE-PENDING.json'
        before_pending=pending.read_bytes();audit=json.loads(before_pending)
        (f.control/audit['transaction']/'3.before').write_bytes(b'QA corrupt late backup')
        before={path:path.read_bytes() for path in f.previous}
        with self.assertRaises(ValueError):f.m.recover(f.root,check_health=lambda *_:None)
        self.assertEqual(pending.read_bytes(),before_pending)
        for path,data in before.items():self.assertEqual(path.read_bytes(),data)

    def v2_fixture(self):
        f=self.fixture()
        if not hasattr(f.m,'ENV_FILE'):self.skipTest('Requires paired script/image v2 candidate')
        # Model the post-bootstrap authority accurately. Direct root unit calls
        # alone do not create the fixed installer executable on the fixture host.
        installer=f.root/'usr/local/sbin/project-paper-observability-package-install'
        installer.write_bytes(Path('/qa-source/scripts/observability_host_package_install.py').read_bytes())
        installer.chmod(0o755)
        return f

    def test_v2_hostile_existing_environment_is_not_executed_or_replaced(self):
        suffixes=(b'UNEXPECTED=value\n',b'PROJECT_PAPER_RESOURCE_HOST=mini-production\n',
                  b'touch /qa-state/must-not-run\n',b'\xff\n')
        for suffix in suffixes:
            with self.subTest(suffix_type=repr(suffix[:20])):
                f=self.v2_fixture();digest,_=self.release(f)
                env=f.root/f.m.ENV_FILE.lstrip('/');env.write_bytes(env.read_bytes()+suffix)
                before={path:path.read_bytes() for path in f.previous}
                with self.assertRaises(ValueError):self.install(f,digest)
                for path,data in before.items():self.assertEqual(path.read_bytes(),data)
                self.assertFalse(Path('/qa-state/must-not-run').exists())

    def test_v2_failed_new_image_health_restores_old_image_and_all_scripts(self):
        f=self.v2_fixture();digest,_=self.release(f);calls=[]
        env=f.root/f.m.ENV_FILE.lstrip('/')
        def health(*_):
            calls.append(env.read_bytes())
            if f.new_image.encode() in calls[-1]:
                for name,path in f.m.FILES.items():self.assertEqual((f.root/path.lstrip('/')).read_bytes(),f.files[name])
                raise RuntimeError('new image qualification failed')
            self.assertIn(f.old_image.encode(),calls[-1])
            for path,data in f.previous.items():self.assertEqual(path.read_bytes(),data)
        with self.assertRaises(RuntimeError):self.install(f,digest,health=health)
        self.assertEqual(len(calls),2)
        f.unchanged()

    def test_v2_crash_after_image_pin_rename_recovers_scripts_and_image_together(self):
        f=self.v2_fixture();digest,_=self.release(f);env=f.root/f.m.ENV_FILE.lstrip('/')
        original=f.m.atomic
        def crash(path,data,mode):
            original(path,data,mode)
            if path==env:raise KeyboardInterrupt()
        with patch.object(f.m,'atomic',side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):self.install(f,digest)
        self.assertTrue((f.control/'HOST-PACKAGE-PENDING.json').exists())
        self.assertIn(f.new_image.encode(),env.read_bytes())
        f.m.recover(f.root,check_health=lambda *_:None)
        f.unchanged()

    def test_v2_image_metadata_wrong_source_architecture_os_or_size_fails_before_mutation(self):
        for changed in ({},{'revision':'b'*40},{'architecture':'arm64'},{'os':'windows'},
                        {'size':True},{'size':0},{'size':512*1024**2+1}):
            with self.subTest(changed=changed):
                f=self.v2_fixture();digest,_=self.release(f)
                metadata={'architecture':'amd64','os':'linux','revision':'a'*40,'size':10*1024**2,**changed}
                calls=[]
                def docker(args,timeout):
                    calls.append(args)
                    if args[0]=='info':return str(f.root/'docker-storage').encode()
                    if args[0]=='pull':return b''
                    if args[-1]==f.old_image:
                        return json.dumps({'architecture':'amd64','os':'linux'}).encode()
                    return json.dumps(metadata).encode()
                with patch.object(f.m,'docker_command',side_effect=docker),patch.object(f.m,'resources'):
                    if changed:
                        with self.assertRaises(ValueError):
                            f.m.install(f.root,digest,'a'*40,check_health=lambda *_:self.fail('must not qualify rejected image'),now=f.now)
                    else:
                        self.assertEqual(f.m.install(f.root,digest,'a'*40,check_health=lambda *_:None,now=f.now)['result'],'ready')
                self.assertTrue(any(args[0]=='image' and args[-1]==f.new_image for args in calls),
                                'Negative metadata cases must reach inspection of the NEW image')
                if changed:f.unchanged()

    def test_v2_legacy_image_helper_cannot_bypass_enabled_package_authority(self):
        import subprocess
        f=self.v2_fixture()
        # Run actual candidate entrypoint bodies, remapping only fixed host
        # locations into this fixture. A fake Docker boundary writes public
        # synthetic health output; no real Docker or production job is called.
        for name in f.files:
            body=(Path('/qa-source')/name).read_text()
            for prefix in ('/usr/local/sbin','/var/lib/project-paper-observability',
                           '/etc/project-paper-observability','/run/lock','/etc/cron.d'):
                body=body.replace(prefix,str(f.root/prefix.lstrip('/')))
            f.files[name]=body.encode()
        digest,_=self.release(f);self.install(f,digest)
        env=f.root/f.m.ENV_FILE.lstrip('/');before=env.read_bytes()
        binary=f.root/'bin';binary.mkdir();capture=f.root/'unexpected-legacy-job'
        docker=binary/'docker'
        docker.write_text('#!'+sys.executable+'\n'+'''import sys,json
from pathlib import Path
from datetime import datetime,timezone
args=sys.argv[1:]
if args[0]=='run':
 Path('''+repr(str(capture))+''').write_text('qualification was run')
 export=next(value.split(':')[0] for value in args if value.endswith(':/export:rw'))
 Path(export,'status.json').write_text(json.dumps({'schema':'project-paper.resource-status.v1','environment':'mini-production','scope':'host','observed_at':datetime.now(timezone.utc).isoformat()}))
''');docker.chmod(0o755)
        image='ghcr.io/devitolo/paper-agent/observability:unapproved@sha256:'+'e'*64
        helper=f.root/f.m.FILES['deploy/observability/project-paper-observability-deploy'].lstrip('/')
        result=subprocess.run(['bash',str(helper),'--image',image,'--target','mini-production'],
            env={**os.environ,'PATH':str(binary)+':'+os.environ['PATH']},capture_output=True,text=True,timeout=15)
        self.assertNotEqual(result.returncode,0,'Legacy image helper accepted an unverified release after package authority was enabled')
        self.assertRegex(result.stderr.lower(),r'package',
                         'An unrelated fixture/tool/permission error is not a trust-policy rejection')
        self.assertFalse(capture.exists(),'Legacy path ran an unverified image qualification')
        self.assertEqual(env.read_bytes(),before)


if __name__ == '__main__':
    unittest.main()

"""Real bootstrap/sudo process acceptance, ONLY inside disposable Linux.

The bootstrap uses the official pinned GH binary. After checking those bytes,
QA substitutes a root-owned synthetic verifier and Docker executable. Therefore
this proves effective sudo/CLI/transaction behavior, not cryptographic provenance
or real collector images. No host Docker socket or writable host mount is used.
"""
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import time
import unittest


ISOLATED = (sys.platform == 'linux' and os.geteuid() == 0
    and os.environ.get('PAPER_OBSERVABILITY_QA_REAL_SUDO') == '1'
    and Path('/.dockerenv').exists()
    and Path('/qa-source/qa-bootstrap/bootstrap.py').is_file()
    and not Path('/var/run/docker.sock').exists())


@unittest.skipUnless(ISOLATED, 'Explicit disposable real-sudo Linux container only')
class RealSudoUpdaterQA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Path('/usr/bin/python3').exists():
            # python:3.12 QA image supplies Python in /usr/local. Production's
            # interpreter path is preserved; no product shebang is rewritten.
            Path('/usr/bin/python3').symlink_to(sys.executable)
        subprocess.run(['/usr/sbin/useradd', '-m', '-u', '1000', '-s', '/bin/sh', 'devitolo'], check=True)
        subprocess.run(['/usr/sbin/useradd', '-m', '-u', '1001', '-s', '/bin/sh', 'paper-deploy'], check=True)
        cls.account = pwd.getpwnam('devitolo')
        cls.legacy_account = pwd.getpwnam('paper-deploy')
        legacy_policy=Path('/etc/sudoers.d/project-paper-observability-legacy')
        legacy_policy.write_bytes(Path('/qa-source/deploy/observability/sudoers.project-paper-observability').read_bytes())
        legacy_policy.chmod(0o440)
        cls.control = Path('/var/lib/project-paper-observability/mini-production-control')
        cls.control.mkdir(parents=True)
        Path('/run/lock').mkdir(parents=True, exist_ok=True)
        Path('/usr/local/sbin').mkdir(parents=True, exist_ok=True)
        cls.config = Path('/etc/project-paper-observability/mini-production/mini-production.env')
        cls.config.parent.mkdir(parents=True)
        cls.cron = Path('/etc/cron.d/project-paper-observability-mini-production')
        cls.cron.parent.mkdir(parents=True, exist_ok=True)
        cls.cron.write_text('*/15 * * * * root /usr/local/sbin/project-paper-observability-collect-publish\n')
        cls.cron.chmod(0o644)
        cls.cron_before = cls.cron.read_bytes()
        cls.installer = '/usr/local/sbin/project-paper-observability-package-install'
        mapping = {'collect-publish.sh': 'project-paper-observability-collect-publish',
                   'run-container.sh': 'project-paper-observability-container',
                   'project-paper-observability-deploy': 'project-paper-observability-deploy'}
        cls.managed = [Path('/usr/local/sbin', dest) for dest in mapping.values()]
        for source, dest in mapping.items():
            path = Path('/usr/local/sbin', dest)
            path.write_bytes(Path('/qa-source/tests/fixtures/observability-bootstrap-old', source).read_bytes())
            path.chmod(0o755)
        cls.old_image = cls.image('b')
        cls.config.write_text('PROJECT_PAPER_OBSERVABILITY_IMAGE='+cls.old_image+'\n'
            'PROJECT_PAPER_RESOURCE_ROOT=/var/lib/project-paper-observability/mini-production\n'
            'PROJECT_PAPER_RESOURCE_EXPORT_DIR=/var/lib/project-paper-observability/mini-production-export\n'
            'PROJECT_PAPER_RESOURCE_ENVIRONMENT=mini-production\n'
            'PROJECT_PAPER_RESOURCE_HOST=mini-production\n'
            'PROJECT_PAPER_RESOURCE_ENV_FILE='+str(cls.config)+'\n')
        cls.config.chmod(0o640)
        cls.protected = []
        for relative in ('var/lib/project-paper-observability/mini-production-publisher/pending.json',
                         'var/lib/project-paper-observability/mini-production-publisher/receipt.json',
                         'etc/project-paper-observability/mini-production/transport.key',
                         'research/paper_agent.db', 'research/profile.json'):
            path=Path('/')/relative;path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'QA synthetic private state');path.chmod(0o600)
            cls.protected.append((path,path.read_bytes(),path.stat().st_mode))
        cls.bootstrap = subprocess.run(['/usr/bin/python3','-I','/qa-source/qa-bootstrap/bootstrap.py'],
                                       capture_output=True,text=True,timeout=30)
        if cls.bootstrap.returncode:
            raise AssertionError('Actual bootstrap failed: '+cls.bootstrap.stdout+cls.bootstrap.stderr)
        cls.verifier=Path('/usr/local/libexec/project-paper-observability/gh')
        cls.official_verifier_sha=hashlib.sha256(cls.verifier.read_bytes()).hexdigest()
        # Only this trust boundary is synthetic. Product CLI and interpreter,
        # fixed paths, sudo policy, locks, rename, health and recovery stay real.
        cls.verifier.write_text('''#!/usr/bin/python3 -I
import hashlib,json,os,pathlib,sys
assert not any(os.environ.get(k) for k in ('GH_TOKEN','GITHUB_TOKEN','PYTHONPATH','HTTP_PROXY','HTTPS_PROXY','DOCKER_HOST'))
a=sys.argv[1:];p=pathlib.Path(a[2]);c=json.loads(pathlib.Path(a[a.index('--bundle')+1]).read_bytes())
assert hashlib.sha256(p.read_bytes()).hexdigest()==c[p.name]
for flag in ('--source-digest','--signer-digest'):assert a[a.index(flag)+1]==c['source_sha']
assert a[a.index('--repo')+1]=='devitolo/paper-agent'
assert a[a.index('--source-ref')+1]=='refs/tags/observability-mini'
''');cls.verifier.chmod(0o755)
        cls.docker_control=Path('/qa-state/docker-control.json')
        cls.docker_control.parent.mkdir(parents=True, exist_ok=True)
        cls.docker_control.write_text(json.dumps({'sources':{cls.old_image:'0'*40}}))
        Path('/usr/bin/docker').write_text('''#!/usr/bin/python3 -I
import json,os,signal,sys
from pathlib import Path
from datetime import datetime,timezone
a=sys.argv[1:]
if a[:2]==['--host','unix:///var/run/docker.sock']:a=a[2:]
c=json.loads(Path('/qa-state/docker-control.json').read_text())
with Path('/qa-state/docker-calls.jsonl').open('a') as out:out.write(json.dumps({'uid':os.geteuid(),'args':a})+'\\n')
if a[0]=='info':print('/')
elif a[:2]==['image','inspect']:
 image=a[-1];assert image in c['sources']
 m={'architecture':'amd64','os':'linux'}
 if 'revision' in a[a.index('--format')+1]:m.update(revision=c['sources'][image],size=10485760)
 print(json.dumps(m))
elif a[0]=='pull':
 assert a[-1] in c['sources']
 if c.get('fail_pull')==a[-1]:sys.exit(44)
elif a[0]=='run':
 image=a[-1]
 if c.get('crash_image')==image:
  pid=os.getppid()
  for _ in range(8):
   cmd=Path('/proc',str(pid),'cmdline').read_bytes()
   if b'project-paper-observability-package-install' in cmd:
    os.kill(pid,signal.SIGKILL);break
   pid=int(next(x.split()[1] for x in Path('/proc',str(pid),'status').read_text().splitlines() if x.startswith('PPid:')))
  else:raise AssertionError('installer ancestor not found')
  sys.exit(45)
 if c.get('fail_image')==image:sys.exit(46)
 export=next(x.rsplit(':',2)[0] for x in a if x.endswith(':/export:rw'))
 p=Path(export);p.mkdir(parents=True,exist_ok=True)
 (p/'status.json').write_text(json.dumps({'schema':'project-paper.resource-status.v1','environment':'mini-production','scope':'host','observed_at':datetime.now(timezone.utc).isoformat()}))
elif a[:2]!=['rm','-f']:raise AssertionError(a)
''');Path('/usr/bin/docker').chmod(0o755)
        spec=importlib.util.spec_from_file_location('builder','/qa-source/scripts/build_observability_host_package.py')
        cls.builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.builder)
        cls.clock=time.time()

    @staticmethod
    def image(char):
        return 'ghcr.io/devitolo/paper-agent/observability:observability-mini@sha256:'+char*64

    def runner(self,args,env=None,account=None):
        account=account or self.account
        def drop():
            os.setgroups([account.pw_gid]);os.setgid(account.pw_gid);os.setuid(account.pw_uid)
        return subprocess.run(['/usr/bin/sudo','-n',*args],preexec_fn=drop,
            env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','HOME':account.pw_dir,'USER':'devitolo',**(env or {})},
            capture_output=True,text=True,timeout=30)

    def state(self):
        paths=self.managed+[self.config,self.cron,self.control/'HOST-PACKAGE-RECEIPT.json',self.control/'INSTALL-RECEIPT']
        return {path:(path.read_bytes(),path.stat().st_mode,path.stat().st_uid) for path in paths if path.exists()}

    def protected_unchanged(self):
        self.assertEqual(self.cron.read_bytes(),self.cron_before)
        for path,data,mode in self.protected:
            self.assertEqual(path.read_bytes(),data);self.assertEqual(path.stat().st_mode,mode)

    def bundle(self,source,image_char):
        source_root=Path('/qa-state/source-'+source)
        for name in self.builder.FILES:
            path=source_root/name;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes((Path('/qa-source')/name).read_bytes()+b'\n# QA release '+source.encode()+b'\n')
        output=Path('/qa-state/bundle-'+source)
        digest=self.builder.build(source_root,output,source*40,self.image(image_char))
        manifest=json.loads((output/'manifest.json').read_bytes())
        type(self).clock+=1
        manifest['created_at']=datetime.fromtimestamp(self.clock,timezone.utc).isoformat()
        (output/'manifest.json').write_text(json.dumps(manifest))
        (output/'attestation.jsonl').write_text(json.dumps({'source_sha':source*40,
            'artifact.tar':digest,'manifest.json':hashlib.sha256((output/'manifest.json').read_bytes()).hexdigest()}))
        destination=Path('/var/lib/project-paper-observability/mini-production-incoming')/(digest+'-'+source*40)
        shutil.copytree(output,destination)
        for path in [destination,*destination.iterdir()]:os.chown(path,self.account.pw_uid,self.account.pw_gid)
        control=json.loads(self.docker_control.read_text());control['sources'][self.image(image_char)]=source*40
        self.docker_control.write_text(json.dumps(control))
        return [self.installer,'--target','mini-production','--bundle',digest,'--source-sha',source*40]

    def test_01_actual_pinned_bootstrap_installs_narrow_effective_authority_without_jobs(self):
        self.assertIn('jobs_run=no',self.bootstrap.stdout)
        self.assertEqual(self.official_verifier_sha,'7469124f706944133d6a169691dd1c6c3511b12e85878d255e044e2948df4c9b')
        self.assertEqual(Path(self.installer).read_bytes(),Path('/qa-source/scripts/observability_host_package_install.py').read_bytes())
        policy=Path('/etc/sudoers.d/project-paper-observability-package')
        self.assertEqual(policy.read_bytes(),Path('/qa-source/deploy/observability/sudoers.host-package.example').read_bytes())
        subprocess.run(['/usr/sbin/visudo','-cf','/etc/sudoers'],check=True,capture_output=True)
        self.assertFalse(Path('/qa-state/docker-calls.jsonl').exists())
        self.protected_unchanged()

    def test_02_unprivileged_fixed_sudo_installs_two_distinct_releases_without_rebootstrap(self):
        bootstrap_before=(self.control/'HOST-BOOTSTRAP-RECEIPT.json').read_bytes()
        for source,char in (('a','c'),('b','d')):
            args=self.bundle(source,char);result=self.runner(args)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual(json.loads(result.stdout)['result'],'ready')
            self.assertIn(self.image(char),self.config.read_text())
            for path in self.managed:
                self.assertIn(b'# QA release '+source.encode(),path.read_bytes())
                self.assertEqual(path.stat().st_uid,0)
            receipt=json.loads((self.control/'HOST-PACKAGE-RECEIPT.json').read_bytes())
            self.assertEqual(receipt['source_sha'],source*40)
            self.assertFalse((self.control/'HOST-PACKAGE-PENDING.json').exists())
        type(self).second_release_args=args
        self.assertEqual((self.control/'HOST-BOOTSTRAP-RECEIPT.json').read_bytes(),bootstrap_before)
        calls=[json.loads(line) for line in Path('/qa-state/docker-calls.jsonl').read_text().splitlines()]
        self.assertTrue(any(x['args'][0]=='run' for x in calls))
        self.assertTrue(all(x['uid']==0 for x in calls))
        self.protected_unchanged()

    def test_03_wrong_target_extra_flags_env_and_arbitrary_commands_cannot_mutate(self):
        before=self.state()
        base=[self.installer,'--target','mini-production','--bundle','a'*64,'--source-sha','a'*40]
        cases=[['/bin/sh','-c','id'],[self.installer,'--target','aws-production','--recover'],
               base+['--root','/qa-state'],base+['--recover'],base[:-1]+['../bad'],
               [self.installer,'--target','mini-production','--recover','--root','/'],
               ['-E',*base],['PROJECT_PAPER_HOST_PACKAGE_QUALIFY=1',*base]]
        for args in cases:
            with self.subTest(args=args):
                result=self.runner(args);self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertEqual(self.state(),before)
        self.protected_unchanged()

    def test_04_failed_new_image_rolls_back_exact_scripts_image_and_receipts(self):
        before=self.state();args=self.bundle('c','e')
        control=json.loads(self.docker_control.read_text());control['fail_image']=self.image('e')
        self.docker_control.write_text(json.dumps(control))
        result=self.runner(args)
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.state(),before)
        self.assertFalse((self.control/'HOST-PACKAGE-PENDING.json').exists())
        self.protected_unchanged()

    def test_06_existing_legacy_sudo_grant_cannot_bypass_package_verification(self):
        before=self.state()
        calls=Path('/qa-state/docker-calls.jsonl').read_bytes()
        result=self.runner(['/usr/local/sbin/project-paper-observability-deploy','--image',
            'ghcr.io/devitolo/paper-agent/observability:unapproved@sha256:'+'e'*64,
            '--target','mini-production'],account=self.legacy_account)
        self.assertEqual(result.returncode,75,result.stdout+result.stderr)
        self.assertIn('guarded package installer',result.stderr)
        self.assertEqual(self.state(),before)
        self.assertEqual(Path('/qa-state/docker-calls.jsonl').read_bytes(),calls)
        self.protected_unchanged()

    def test_07_poisoned_runner_environment_cannot_change_fixed_installer_execution(self):
        poison=Path('/home/devitolo/poison');poison.mkdir()
        marker=Path('/home/devitolo/unexpected-python-startup')
        (poison/'sitecustomize.py').write_text('open('+repr(str(marker))+',"w").write("executed")\n')
        before=self.state()
        result=self.runner(self.second_release_args,env={'PYTHONPATH':str(poison),'GH_TOKEN':'QA-SYNTHETIC',
            'GITHUB_TOKEN':'QA-SYNTHETIC','DOCKER_HOST':'tcp://qa.invalid:2375',
            'PROJECT_PAPER_HOST_PACKAGE_QUALIFY':'1','HTTPS_PROXY':'http://qa.invalid'})
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(json.loads(result.stdout)['result'],'already_ready')
        self.assertFalse(marker.exists())
        self.assertEqual(self.state(),before)
        self.protected_unchanged()

    def test_05_killed_installer_leaves_pending_and_real_sudo_recovery_restores_release_two(self):
        before=self.state();args=self.bundle('d','f')
        control=json.loads(self.docker_control.read_text());control.pop('fail_image',None);control['crash_image']=self.image('f')
        self.docker_control.write_text(json.dumps(control))
        result=self.runner(args);self.assertNotEqual(result.returncode,0)
        self.assertTrue((self.control/'HOST-PACKAGE-PENDING.json').exists())
        self.assertIn(self.image('f'),self.config.read_text())
        control.pop('crash_image');self.docker_control.write_text(json.dumps(control))
        result=self.runner([self.installer,'--target','mini-production','--recover'])
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(json.loads(result.stdout)['result'],'recovered')
        self.assertEqual(self.state(),before)
        self.assertFalse((self.control/'HOST-PACKAGE-PENDING.json').exists())
        self.protected_unchanged()


if __name__=='__main__':unittest.main(verbosity=2)

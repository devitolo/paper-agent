"""AWS release script transaction checks only in an isolated root QA container."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ISOLATED=(os.geteuid()==0 and os.environ.get('PAPER_OBSERVABILITY_QA_ISOLATED')=='1' and Path('/.dockerenv').exists() and Path('/qa-source/scripts/aws_production_update.sh').exists())


@unittest.skipUnless(ISOLATED,'Disposable root Linux container only')
class AWSReleaseTransactionTests(unittest.TestCase):
    def run_release(self, fail=False):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);project=root/'project';project.mkdir();binary=root/'bin';binary.mkdir()
            old='ghcr.io/devitolo/paper-agent@sha256:'+'b'*64;new='ghcr.io/devitolo/paper-agent@sha256:'+'a'*64
            envfile=project/'.env';before='PAPER_APP_IMAGE='+old+'\nKEEP=retained\n';envfile.write_text(before)
            compose=project/'compose.yml';compose.write_text(json.dumps({'services':{'app':{'image':'${PAPER_APP_IMAGE}','environment':{'KEEP':'retained'},'volumes':[]}}}));base=compose.read_bytes()
            export=Path('/var/lib/project-paper-observability/aws-production-export');export.mkdir(parents=True,exist_ok=True);(export/'status.json').write_text('{}')
            docker=binary/'docker';docker.write_text('#!'+sys.executable+'\n'+'''import json,os,pathlib,sys
args=sys.argv[1:];root=pathlib.Path(os.environ['AWS_PROJECT_DIR'])
with (root/'docker-calls').open('a') as f:f.write(json.dumps(args)+'\\n')
if args[0]=='inspect':print('unhealthy' if os.environ['QA_FAIL']=='1' else 'healthy')
if args[0]=='compose' and '--format' in args:print((root/'compose.yml').read_text())
''');docker.chmod(0o755)
            for name in ('curl','sleep'):
                path=binary/name;path.write_text('#!/bin/sh\nexit 0\n');path.chmod(0o755)
            env=dict(os.environ,PATH=str(binary)+':'+os.environ['PATH'],AWS_PROJECT_DIR=str(project),AWS_HEALTH_TIMEOUT_SECONDS='1',GITHUB_RUN_ID='qa',QA_FAIL='1' if fail else '0')
            result=subprocess.run(['bash','/qa-source/scripts/aws_production_update.sh',new],env=env,capture_output=True,text=True,timeout=10)
            self.assertEqual(compose.read_bytes(),base)
            releases=list((project/'production-releases').iterdir());self.assertEqual(len(releases),1)
            self.assertTrue((releases[0]/'backup-evidence').exists(), result.stdout+result.stderr)
            calls=[json.loads(line) for line in (project/'docker-calls').read_text().splitlines()]
            self.assertTrue(any(call[0]=='exec' and 'src.backup(dst)' in ' '.join(call) for call in calls))
            override=project/'compose.aws-resources.json'
            if fail:
                self.assertNotEqual(result.returncode,0,result.stderr);self.assertEqual(envfile.read_text(),before);self.assertFalse(override.exists())
                self.assertEqual((releases[0]/'result').read_text(),'rollback=completed\n')
            else:
                self.assertEqual(result.returncode,0,result.stderr);self.assertIn(new,envfile.read_text());self.assertIn('KEEP=retained',envfile.read_text())
                value=json.loads(override.read_text());self.assertTrue(value['services']['app']['volumes'][0]['read_only'])
                self.assertIn('health=healthy',(releases[0]/'result').read_text())

    def test_success_keeps_base_state_and_adds_readonly_export(self):self.run_release()
    def test_failed_health_restores_env_and_removes_new_override(self):self.run_release(fail=True)

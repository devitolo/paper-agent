import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC=importlib.util.spec_from_file_location('mount',Path(__file__).parents[1]/'scripts/aws_resource_status_mount.py')
mount=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(mount)


class AWSResourceMountTests(unittest.TestCase):
    def setUp(self):
        self.compose={'services':{'app':{'image':'${PAPER_APP_IMAGE}','environment':{'KEEP_SECRET':'unchanged'},'volumes':['current-data:/app/data','existing-config:/app/config',{'type':'bind','source':'/existing-key','target':'/run/key','read_only':True}]},'other':{'image':'unchanged'}},'volumes':{'current-data':{'external':True}}}

    def test_readonly_override_does_not_copy_existing_state(self):
        before=copy.deepcopy(self.compose);result=mount.configure(self.compose)
        self.assertEqual(self.compose,before)
        app=result['services']['app']
        self.assertEqual(len(app['volumes']),1)
        self.assertTrue(app['volumes'][0]['read_only'])
        self.assertEqual(app['environment'],{'PAPER_AGENT_AWS_RESOURCE_STATUS_FILE':mount.STATUS})
        self.assertNotIn('KEEP_SECRET',json.dumps(result))
        self.assertNotIn('volumes',result)
        self.assertNotIn('other',result['services'])

    def test_conflicting_mount_or_path_is_refused(self):
        self.compose['services']['app']['volumes'].append({'type':'bind','source':'/other','target':mount.EXPORT,'read_only':False})
        with self.assertRaises(ValueError):mount.configure(self.compose)
        self.compose['services']['app']['volumes'].pop()
        self.compose['services']['app']['environment']['PAPER_AGENT_AWS_RESOURCE_STATUS_FILE']='/other'
        with self.assertRaises(ValueError):mount.configure(self.compose)

    def test_atomic_file_mode_idempotency_and_unknown_override_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'override.json';mount.update(path,self.compose)
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            before=path.read_bytes();mount.update(path,self.compose);self.assertEqual(path.read_bytes(),before)
            self.assertEqual(list(path.parent.iterdir()),[path])
            path.write_text('{}')
            with self.assertRaises(ValueError):mount.update(path,self.compose)

    def test_real_compose_merge_keeps_existing_volumes_environment_and_image_variable(self):
        import os, shutil, subprocess
        if not shutil.which('docker'):self.skipTest('Docker Compose CLI required')
        base=copy.deepcopy(self.compose)
        base['volumes']['existing-config']={'external':True}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);original=root/'compose.json';override=root/'resource.json'
            original.write_text(json.dumps(base));mount.update(override,base)
            result=subprocess.run(['docker','compose','--project-name','paper-resource-qa','-f',str(original),'-f',str(override),'config','--format','json'],env=dict(os.environ,PAPER_APP_IMAGE='ghcr.io/devitolo/paper-agent@sha256:'+'a'*64),capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            merged=json.loads(result.stdout)['services']['app']
            self.assertEqual(merged['environment']['KEEP_SECRET'],'unchanged')
            self.assertEqual(merged['environment']['PAPER_AGENT_AWS_RESOURCE_STATUS_FILE'],mount.STATUS)
            self.assertEqual(merged['image'],'ghcr.io/devitolo/paper-agent@sha256:'+'a'*64)
            self.assertTrue(any(v['target']=='/app/data' and v['source']=='current-data' for v in merged['volumes']))
            self.assertTrue(any(v['target']=='/run/key' and v['source']=='/existing-key' and v['read_only'] for v in merged['volumes']))
            self.assertTrue(any(v['target']==mount.EXPORT and v['read_only'] for v in merged['volumes']))
            self.assertEqual(json.loads(original.read_bytes()),base)

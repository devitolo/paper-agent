"""Exercise cron wrapper -> fixed Docker queries -> stdin exporter -> loader."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from paper_agents import aws_container_status as c

ROOT=Path(__file__).resolve().parents[1]


class AWSContainerWrapperTests(unittest.TestCase):
    def pipeline(self,target='aws-production',collector_exit=0,export_exit=0):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);binary=root/'bin';binary.mkdir();export=root/'export';export.mkdir()
            state=root/'state';state.mkdir();events=root/'events'
            docker=binary/'docker'
            docker.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
from paper_agents import aws_container_status as c
args=sys.argv[1:]
if args[0]=='run':
 if 'paper_agents.aws_container_status' in args:
  with open(os.environ['QA_EVENTS'],'a') as f:f.write('export\\n')
  if int(os.environ['QA_EXPORT_EXIT']):sys.exit(int(os.environ['QA_EXPORT_EXIT']))
  value=c.from_stream(sys.stdin.buffer);c.publish(Path(os.environ['QA_EXPORT'])/'container-status.json',value)
 else:
  with open(os.environ['QA_EVENTS'],'a') as f:f.write('host\\n')
  sys.exit(int(os.environ['QA_COLLECTOR_EXIT']))
elif args[0]=='inspect':
 if args[-1]!='project-paper-prod-app-1':sys.exit(1)
 if args[args.index('--format')+1]=='{{.Image}}':print('sha256:'+'a'*64)
 else:print(json.dumps({'status':'running','health':'healthy','image_id':'sha256:'+'a'*64}))
elif args[:2]==['image','inspect']:
 print(json.dumps({'image_id':'sha256:'+'a'*64,'digests':['example@sha256:'+'b'*64]}))
elif args[0]=='stats':
 print(json.dumps({'cpu':'0%','memory':'5MiB / 64MiB'}))
''');docker.chmod(0o755)
            timeout=binary/'timeout';timeout.write_text('#!/bin/sh\nwhile [ "$1" != docker ]; do shift; done\nexec "$@"\n');timeout.chmod(0o755)
            script=(ROOT/'deploy/observability/run-container.sh').read_text().replace('/usr/bin/timeout',str(timeout))
            env=dict(os.environ,PATH=str(binary)+':'+os.environ['PATH'],PYTHONPATH=str(ROOT),PROJECT_PAPER_RESOURCE_ENV_FILE=str(root/'absent.env'),PROJECT_PAPER_OBSERVABILITY_IMAGE='example@sha256:'+'a'*64,PROJECT_PAPER_RESOURCE_ROOT=str(state),PROJECT_PAPER_RESOURCE_EXPORT_DIR=str(export),PROJECT_PAPER_RESOURCE_ENVIRONMENT=target,QA_EVENTS=str(events),QA_EXPORT=str(export),QA_COLLECTOR_EXIT=str(collector_exit),QA_EXPORT_EXIT=str(export_exit))
            result=subprocess.run(['sh','-c',script],env=env,capture_output=True,text=True,timeout=15)
            self.assertTrue(events.exists(),result.stderr)
            path=export/'container-status.json'
            return result.returncode,events.read_text().splitlines(),c.load(path),path.exists()

    def test_aws_host_collection_then_export_roundtrip(self):
        code,events,data,exists=self.pipeline()
        self.assertEqual(code,0);self.assertEqual(events,['host','export']);self.assertTrue(exists)
        self.assertEqual(data['freshness'],'fresh');self.assertEqual(data['items'][0]['cpu_percent'],0)
        self.assertEqual(data['items'][1]['status'],'unavailable')
        self.assertIsNone(data['items'][1]['memory_bytes'])

    def test_mini_path_is_unchanged(self):
        code,events,data,exists=self.pipeline(target='mini-production')
        self.assertEqual(code,0);self.assertEqual(events,['host']);self.assertFalse(exists)

    def test_host_failure_prevents_export_and_export_failure_does_not_fail_host(self):
        code,events,data,exists=self.pipeline(collector_exit=42)
        self.assertEqual(code,42);self.assertEqual(events,['host']);self.assertFalse(exists)
        code,events,data,exists=self.pipeline(export_exit=7)
        self.assertEqual(code,0);self.assertEqual(events,['host','export']);self.assertFalse(exists)


if __name__=='__main__':unittest.main()

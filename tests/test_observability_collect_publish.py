from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class CollectPublishTests(unittest.TestCase):
    def run_pipeline(self, collector_exit=0, publisher_exit=0, enabled='1', image_digest='a'*64):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);calls=root/'calls';config=root/'config';config.mkdir();binary=root/'bin';binary.mkdir()
            (config/'mini-production.env').write_text('PROJECT_PAPER_RESOURCE_ENVIRONMENT=mini-production\nPROJECT_PAPER_OBSERVABILITY_IMAGE=ghcr.io/devitolo/paper-agent/observability:qa@sha256:'+image_digest+'\n')
            (config/'publisher.env').write_text('PAPER_OBSERVABILITY_PUBLISH_ENABLED='+enabled+'\nPROJECT_PAPER_PUBLISHER_IMAGE=ghcr.io/devitolo/paper-agent/observability-publisher@sha256:'+'a'*64+'\nPROJECT_PAPER_PUBLISHER_URL=https://paper-prod.tail5fc461.ts.net/observability/v1/mini-status\nPROJECT_PAPER_PUBLISHER_DNS=100.100.100.100\n')
            collector=root/'collector';collector.write_text('#!/bin/sh\necho collect >> "$CALLS"\nexit '+str(collector_exit)+'\n');collector.chmod(0o755)
            docker=binary/'docker';docker.write_text('''#!/bin/sh
case "$1" in
container) exit 1;;
run) echo "publish $*" >> "$CALLS"; exit "${PUBLISHER_EXIT}";;
rm) exit 0;;
esac
''');docker.chmod(0o755)
            timeout=binary/'timeout';timeout.write_text('#!/bin/sh\nwhile [ "$1" != docker ]; do shift; done\nexec "$@"\n');timeout.chmod(0o755)
            script=(ROOT/'deploy/observability/collect-publish.sh').read_text().replace('/etc/project-paper-observability/mini-production',str(config)).replace('/usr/local/sbin/project-paper-observability-container',str(collector)).replace('/usr/bin/timeout',str(timeout))
            env=dict(os.environ,PATH=str(binary)+':'+os.environ['PATH'],CALLS=str(calls),PUBLISHER_EXIT=str(publisher_exit))
            result=subprocess.run(['bash','-c',script],env=env,capture_output=True,text=True,timeout=5)
            return result.returncode,calls.read_text().splitlines() if calls.exists() else []

    def test_collection_finishes_before_single_publisher(self):
        code,calls=self.run_pipeline()
        self.assertEqual(code,0)
        self.assertEqual(calls[0],'collect')
        self.assertEqual(len(calls),2)
        self.assertIn('--dns=100.100.100.100',calls[1])
        self.assertIn('--user=10002:10002',calls[1])
        self.assertIn('--pull=never',calls[1])
        self.assertIn('--entrypoint=/opt/project-paper-observability/publish.sh', calls[1])
        self.assertIn('ghcr.io/devitolo/paper-agent/observability:qa@sha256:', calls[1])
        self.assertNotIn('observability-publisher@', calls[1])
        self.assertNotIn('docker.sock',calls[1])
        self.assertNotIn('/host/proc',calls[1])
        self.assertNotIn('/host/sys',calls[1])
        self.assertEqual(calls[1].count('--mount '), 3)

    def test_configured_image_update_and_rollback_select_same_publisher_digest(self):
        for digest in ('a'*64, 'b'*64, 'a'*64):
            code,calls=self.run_pipeline(image_digest=digest)
            self.assertEqual(code,0)
            self.assertIn('observability:qa@sha256:'+digest,calls[1])
            self.assertNotIn('observability-publisher@',calls[1])

    def test_collection_failure_prevents_publication(self):
        code,calls=self.run_pipeline(collector_exit=42)
        self.assertEqual(code,42);self.assertEqual(calls,['collect'])

    def test_delivery_failure_does_not_rerun_collection(self):
        code,calls=self.run_pipeline(publisher_exit=43)
        self.assertEqual(code,43);self.assertEqual(sum(c=='collect' for c in calls),1)

    def test_flag_off_still_collects_without_publication(self):
        code,calls=self.run_pipeline(enabled='0')
        self.assertEqual(code,0);self.assertEqual(calls,['collect'])


if __name__=='__main__':unittest.main()

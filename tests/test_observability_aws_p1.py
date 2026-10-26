import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
import subprocess
from unittest.mock import patch

from paper_agents.resource_status import SCHEMA, load_resource_status
from paper_agents import web
from scripts import host_resource_collector as collector


class ObservabilityAwsP1Tests(unittest.TestCase):
    def test_corrupt_cpu_baseline_is_explicitly_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'last-cpu.json').write_text('{"total":"bad"}')
            with patch.object(collector, 'read_cpu', return_value=(200, 100)), \
                 patch.object(collector, 'now', return_value=datetime.now(timezone.utc)):
                sample = collector.sample(root)
            self.assertEqual(sample['cpu']['state'], 'baseline_invalid')
            self.assertNotIn('utilization_percent', sample['cpu'])

    def test_infinite_cpu_baseline_is_explicitly_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'last-cpu.json').write_text('{"total":"Infinity","idle":1}')
            with patch.object(collector, 'read_cpu', return_value=(200, 100)):
                sample = collector.sample(root)
            self.assertEqual(sample['cpu']['state'], 'baseline_invalid')

    def test_resource_status_rejects_malformed_future_and_sensitive_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            current = datetime(2026, 10, 25, tzinfo=timezone.utc)
            for payload in (
                {'schema': SCHEMA, 'observed_at': current.isoformat(), 'cpu': []},
                {'schema': SCHEMA, 'observed_at': (current + timedelta(hours=1)).isoformat()},
                {'schema': SCHEMA, 'observed_at': current.isoformat(), 'latest': {'api_token': 'redact'}},
                {'schema': SCHEMA, 'observed_at': current.isoformat(), 'latest': {'environment_dump': {'headers': {'Authorization': 'sentinel'}}}},
                {'schema': SCHEMA, 'observed_at': current.isoformat(), 'cpu': {'average_percent': 10 ** 400}},
                {'schema': SCHEMA, 'observed_at': current.isoformat(), 'latest': {'disk': 'bad'}},
                {'schema': SCHEMA, 'observed_at': current.isoformat(), 'latest': {'disk': [1, 2]}},
            ):
                path.write_text(json.dumps(payload))
                self.assertEqual(load_resource_status(path, now=current)['state'], 'malformed')

    def test_warming_snapshot_with_nullable_aggregates_is_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            path.write_text(json.dumps({'schema': SCHEMA, 'state': 'warming', 'observed_at': '2026-10-25T00:00:00+00:00', 'cpu': {'average_percent': None}, 'memory': {'available_average_bytes': None}, 'disk': None, 'latest': {'disk': None}}))
            self.assertEqual(load_resource_status(path, now=datetime(2026, 10, 25, tzinfo=timezone.utc))['state'], 'warming')

    def test_health_reads_disk_from_latest_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'aws.json'
            path.write_text(json.dumps({'schema': SCHEMA, 'observed_at': '2026-10-25T00:00:00+00:00', 'state': 'observed', 'cpu': {'average_percent': 12}, 'memory': {'available_average_bytes': 100}, 'latest': {'disk': {'used_percent': 10}}}))
            with patch.dict(os.environ, {'PAPER_AGENT_AWS_RESOURCE_STATUS_FILE': str(path), 'PAPER_AGENT_MINI_RESOURCE_STATUS_FILE': str(path)}), \
                 patch.object(web, 'sync_health_snapshot', return_value={'state': 'ok', 'time': 'now', 'revision': 'r'}):
                html = web.render_health_infrastructure({'db': {'integrity': 'ok', 'path': 'db'}})
            self.assertIn('Aws Disk / Service', html)
            self.assertIn('observed · see collector snapshot', html)

    def test_timeout_cleanup_is_present_and_aws_release_is_targeted(self):
        launcher = Path('deploy/observability/run-container.sh').read_text()
        workflow = Path('.github/workflows/observability-release.yml').read_text()
        self.assertIn('trap cleanup EXIT', launcher)
        self.assertIn('docker rm -f "$container_name"', launcher)
        self.assertIn('observability-aws-v*', workflow)
        self.assertIn('project-paper-observability-aws-deploy', workflow)
        self.assertIn('target=aws-production', workflow)

    def test_cron_launcher_to_container_export_to_health_reader(self):
        if not Path('/usr/bin/timeout').exists():
            self.skipTest('Linux timeout utility required for host launcher integration')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); state = root / 'state'; export = root / 'export'; bin_dir = root / 'bin'
            state.mkdir(); export.mkdir(); bin_dir.mkdir()
            env_file = root / 'aws-production.env'
            env_file.write_text(f'PROJECT_PAPER_OBSERVABILITY_IMAGE=registry.invalid/collector@sha256:{"a" * 64}\nPROJECT_PAPER_RESOURCE_ROOT={state}\nPROJECT_PAPER_RESOURCE_EXPORT_DIR={export}\nPROJECT_PAPER_RESOURCE_ENVIRONMENT=aws-production\nPROJECT_PAPER_RESOURCE_ENV_FILE={env_file}\n')
            docker = bin_dir / 'docker'
            docker.write_text('#!/bin/sh\ncat > "$QA_EXPORT/status.json" <<EOF\n{"schema":"project-paper.resource-status.v1","observed_at":"2026-10-25T00:00:00+00:00","state":"observed","environment":"aws-production","scope":"host","cpu":{"average_percent":12},"memory":{"available_average_bytes":100},"latest":{"disk":{"used_percent":10}}}\nEOF\nexit 0\n')
            docker.chmod(0o755)
            env = dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}', PROJECT_PAPER_RESOURCE_ENV_FILE=str(env_file), QA_EXPORT=str(export))
            result = subprocess.run(['sh', 'deploy/observability/run-container.sh'], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(load_resource_status(export / 'status.json', now=datetime(2026, 10, 25, tzinfo=timezone.utc))['state'], 'observed')


if __name__ == '__main__':
    unittest.main()

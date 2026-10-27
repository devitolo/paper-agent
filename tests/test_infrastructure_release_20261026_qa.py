"""Independent acceptance probes for the October 26 infrastructure releases.

Run against the frozen AWS app source. QA_MINI_SOURCE and QA_AWS_SOURCE point
to the separately frozen collector releases; all mutations use temporary files.
"""
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from paper_agents import aws_container_status as containers
from paper_agents import observability_transport as transport
from paper_agents import resource_status, web
from tests.test_aws_container_status import facts
from tests.test_observability_transport import fixture


class InfrastructureReleaseAcceptance(unittest.TestCase):
    def collector(self, source, name):
        root = Path(os.environ[source])
        spec = importlib.util.spec_from_file_location(name, root/'scripts/host_resource_collector.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @unittest.skipUnless(os.environ.get('QA_AWS_SOURCE') and os.environ.get('QA_MINI_SOURCE'),
                         'Requires both frozen collector sources')
    def test_each_host_attributes_cpu_delta_to_the_window_it_measures(self):
        end = datetime(2026, 10, 26, 12, 15, tzinfo=timezone.utc)
        for source in ('QA_AWS_SOURCE', 'QA_MINI_SOURCE'):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                collector = self.collector(source, 'qa_window_'+source)
                root = Path(directory)
                rows = [{'observed_at': stamp.isoformat(), 'cpu': {'utilization_percent': cpu, 'interval_seconds': 900},
                         'memory': {'available_bytes': 1000}}
                        for stamp, cpu in ((end-timedelta(minutes=15), 10), (end, 90))]
                (root/'samples.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
                value = collector.summarize(root, reference=end)
                self.assertEqual(value['cpu']['average_percent'], 90,
                    'The interval ending at 12:15 measures 12:00–12:15, not the previous interval')
                self.assertIsNone(value['cpu']['max_one_minute_percent'],
                    'A fifteen-minute observation cannot establish a one-minute maximum')

    def test_pacific_fall_back_times_remain_distinguishable(self):
        self.assertIn('01:30:00 AM PDT', web.telemetry_pacific_time('2026-11-01T08:30:00Z'))
        self.assertIn('01:30:00 AM PST', web.telemetry_pacific_time('2026-11-01T09:30:00Z'))
        self.assertIn('01:59:59 AM PST', web.telemetry_pacific_time('2026-03-08T09:59:59Z'))
        self.assertIn('03:00:00 AM PDT', web.telemetry_pacific_time('2026-03-08T10:00:00Z'))

    def test_container_freshness_exact_boundaries(self):
        value = containers.from_stream(io.BytesIO(facts()))
        now = datetime.now(timezone.utc)
        for seconds, expected in ((-2700, 'fresh'), (-2701, 'stale'), (300, 'fresh')):
            with self.subTest(seconds=seconds):
                value['observed_at'] = (now + timedelta(seconds=seconds)).isoformat()
                self.assertEqual(containers.validate(value, now=now)['freshness'], expected)
        value['observed_at'] = (now + timedelta(seconds=301)).isoformat()
        with self.assertRaises(ValueError):
            containers.validate(value, now=now)

    def test_age_style_changes_after_45_minutes(self):
        stamp = '2026-10-26T12:00:00Z'
        clock = datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
        self.assertNotIn('telemetry-age-stale', web.render_telemetry_timestamp(stamp, clock=clock+2700))
        self.assertIn('telemetry-age-stale', web.render_telemetry_timestamp(stamp, clock=clock+2701))

    def test_all_container_states_preserve_explicit_unhealthy_and_hide_stopped_metrics(self):
        for state in ('created', 'paused', 'restarting', 'removing', 'exited', 'dead', 'unavailable'):
            with self.subTest(state=state):
                rows = facts().splitlines()
                rows[0] = json.dumps({'status': state, 'health': 'unhealthy'}).encode()
                value = containers.from_stream(io.BytesIO(b'\n'.join(rows)))
                item = value['items'][0]
                self.assertIsNone(item['cpu_percent'])
                self.assertIsNone(item['memory_bytes'])
                page = web.render_aws_container_health(containers.validate(value))
                self.assertIn('<td>unhealthy</td>', page)
                self.assertIn('<td>N/A</td><td>Unavailable</td>', page)

    def test_failed_atomic_container_replace_preserves_bytes_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'container-status.json'
            value = containers.from_stream(io.BytesIO(facts()))
            containers.publish(path, value)
            before = path.read_bytes()
            with patch.object(containers.os, 'replace', side_effect=OSError('injected')):
                with self.assertRaises(OSError):
                    containers.publish(path, {**value, 'observed_at': 'changed'})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_corrupt_one_host_does_not_hide_other_host(self):
        mini = transport.health_status('/nonexistent-qa-mini-file')
        aws = containers.validate(containers.from_stream(io.BytesIO(facts())))
        with patch.object(web, 'mini_telemetry_health', return_value=mini), \
             patch.object(web, 'load_aws_resource_status', return_value={'freshness': 'unavailable'}), \
             patch.object(web, 'load_aws_container_status', return_value=aws):
            page = web.render_health_infrastructure({})
        self.assertIn('No fresh Mini container observations', page)
        self.assertIn('5.5 / 64.0 MiB', page)

    def test_window_peak_and_average_are_not_daily_peaks(self):
        status = fixture()['status']
        status.update(freshness='fresh')
        status['cpu'].update(average_percent=12, max_observed_percent=63)
        status['memory'].update(available_average_bytes=2*1024**3, available_min_bytes=1024**3)
        page = web.render_host_resources(status, host='Mini')
        for text in ('12.00', 'Peak 63.00%', 'Lowest 1.00 GiB', '15-min average'):
            self.assertIn(text, page)
        self.assertNotIn('daily', page.lower())

    def test_container_panel_exposes_its_own_observation_time(self):
        value = containers.from_stream(io.BytesIO(facts()))
        page = web.render_aws_container_health(containers.validate(value))
        self.assertTrue(value['observed_at'] in page or web.telemetry_pacific_time(value['observed_at']) in page,
            'A pre-recreation snapshot must be distinguishable from current container state')

    def test_fresh_host_must_not_make_old_mini_disk_current(self):
        now = datetime.now(timezone.utc)
        source = fixture(now.timestamp())['status']
        source['latest']['observation_times'] = {'disk': (now-timedelta(hours=2)).isoformat()}
        source['latest']['disk']['used_percent'] = 17.89
        payload = transport.project(source, clock=now.timestamp())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'received.json'
            transport.ingest(path, transport.encoded(payload), str(uuid.uuid4()), clock=now.timestamp())
            page = web.render_host_resources(transport.health_status(path, clock=now.timestamp()), host='Mini')
        self.assertNotIn('17.89', page, 'Mini disk timestamp was lost during projection')

    @unittest.skipUnless(os.environ.get('QA_MINI_SOURCE'), 'Requires frozen Mini producer source')
    def test_separate_mini_producer_payload_is_accepted_by_deployed_aws_reader(self):
        root = os.environ.get('QA_MINI_SOURCE')
        self.assertTrue(root, 'QA_MINI_SOURCE must identify the frozen Mini release')
        script = '''import io,json,sys
from paper_agents import container_telemetry as c, observability_transport as t
from tests.test_mini_container_telemetry import facts
from tests.test_observability_transport import fixture
s=fixture()['status'];s['latest']['containers']=c.read_container_health(io.BytesIO(facts()))
sys.stdout.buffer.write(t.encoded(t.project(s)))
'''
        body = subprocess.check_output([sys.executable, '-B', '-c', script], cwd=root,
            env={**os.environ, 'PYTHONPATH': root}, timeout=10)
        identity = str(uuid.uuid4())
        clock = datetime.now(timezone.utc).timestamp()
        timestamp = str(int(clock))
        key = b'qa-synthetic-key-never-production'
        headers = {'X-Paper-Observability-Timestamp': timestamp, 'X-Paper-Observability-Id': identity,
                   'X-Paper-Observability-Signature': transport.signature(key, body, timestamp, identity)}
        self.assertEqual(transport.authenticate(headers, body, key, clock=clock), identity)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'received.json'
            transport.ingest(path, body, identity, clock=clock)
            before = path.read_bytes()
            page = web.render_mini_container_health(transport.health_status(path, clock=clock))
            self.assertIn('5.5 / 256.0 MiB', page)
            self.assertEqual(page.count('<details'), 6)
            self.assertEqual(path.read_bytes(), before)

    @unittest.skipUnless(os.environ.get('QA_AWS_SOURCE'), 'Requires frozen AWS collector source')
    def test_delayed_aws_collector_export_disk_time_and_value_reach_health(self):
        collector = self.collector('QA_AWS_SOURCE', 'qa_exact_aws_collector')
        now = datetime(2026, 10, 26, 12, 46, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)/'state'; export = Path(directory)/'export'
            with patch.dict(os.environ, {'PROJECT_PAPER_RESOURCE_ENVIRONMENT': 'aws-production',
                 'PROJECT_PAPER_RESOURCE_HOST': 'aws-production', 'PROJECT_PAPER_RESOURCE_EXPORT_DIR': str(export)}), \
                 patch.object(collector, 'now', return_value=now), \
                 patch.object(collector, 'filesystem', return_value={'capacity_bytes': 1000, 'available_bytes': 678, 'used_percent': 32.2}):
                collector.collect(state)
            raw = json.loads((export/'status.json').read_bytes())
            self.assertEqual(raw['latest']['observation_times']['disk'], now.isoformat())
            loaded = resource_status.load_aws_resource_status(export/'status.json', now=now)
            self.assertEqual(loaded['disk']['used_percent'], 32.2)
            self.assertIn('32.20', web.render_host_resources(loaded, host='AWS'))


if __name__ == '__main__':
    unittest.main()

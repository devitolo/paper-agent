import copy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from paper_agents import container_telemetry as c, observability_transport as t
from tests.test_observability_transport import fixture


def facts():
    lines = []
    for service in t.CONTAINER_SERVICES:
        lines.extend((json.dumps({'status': 'running', 'health': 'healthy', 'image_id': 'sha256:'+'a'*64}),
                      json.dumps({'image_id':'sha256:'+'a'*64,'digests':['ghcr.io/devitolo/paper-agent@sha256:'+'b'*64]}),
                      json.dumps({'cpu': '0.25%', 'memory': '5.5MiB / 256MiB'})))
    return ('\n'.join(lines)+'\n').encode()


class MiniContainerTelemetryTests(unittest.TestCase):
    def test_projection_ingest_and_reader_preserve_four_fields(self):
        current = time.time()
        snapshot = c.read_container_health(io.BytesIO(facts()))
        self.assertEqual(snapshot['items'][0]['memory_bytes'], 5767168)
        self.assertEqual(snapshot['items'][0]['image_digest'], 'sha256:'+'b'*64)
        source = fixture(current)['status']; source['latest']['containers'] = snapshot
        payload = t.project(source, clock=current)
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)/'received.json'
            t.ingest(state, t.encoded(payload), str(uuid.uuid4()), clock=current)
            received = t.health_status(state, clock=current)
            self.assertEqual(received['latest']['containers'], snapshot)

    def test_scheduled_and_stopped_containers_have_no_live_resource_claim(self):
        lines = facts().splitlines()
        lines[-6:] = [b'{}', b'{}', b'{}'] * 2
        lines[0] = json.dumps({'status': 'exited', 'health': 'unavailable', 'image_id': 'sha256:'+'a'*64}).encode()
        lines[2] = b'{}'
        value = c.read_container_health(io.BytesIO(b'\n'.join(lines)))
        self.assertEqual(value['items'][-1]['status'], 'unavailable')
        self.assertIsNone(value['items'][-1]['cpu_percent'])
        self.assertEqual(value['items'][0]['status'], 'exited')
        self.assertIsNone(value['items'][0]['memory_bytes'])

    def test_bad_sensitive_future_and_stale_fields_rejected(self):
        base = c.read_container_health(io.BytesIO(facts()))
        for field, value in (('cpu_percent', float('nan')), ('cpu_percent', -1), ('cpu_percent', True),
                             ('image_digest', '<script>secret</script>'), ('status', {}),
                             ('service', 'nonprod'), ('memory_bytes', 2**80)):
            with self.subTest(field=field):
                altered = copy.deepcopy(base); altered['items'][0][field] = value
                with self.assertRaises(t.TransportError): t.validate_container_health(altered)
        altered = copy.deepcopy(base); altered['items'][0]['API_KEY'] = 'secret'
        with self.assertRaises(t.TransportError): t.validate_container_health(altered)
        for seconds in (-2701, 301):
            altered = copy.deepcopy(base)
            altered['observed_at'] = datetime.fromtimestamp(time.time()+seconds, timezone.utc).isoformat()
            with self.assertRaises(t.TransportError): t.validate_container_health(altered)
        with self.assertRaises(ValueError): c.read_container_health(io.BytesIO(b'x'*16385))
        with self.assertRaises(ValueError): c.read_container_health(io.BytesIO(b'{}\n'))

    def test_wrapper_is_fixed_allowlist_without_socket_or_config_access(self):
        text=(Path(__file__).resolve().parents[1]/'deploy/observability/collect-publish.sh').read_text()
        self.assertNotIn('docker.sock',text);self.assertNotIn('{{json .}}',text)
        self.assertNotIn('paper-mini-nonprod',text)
        self.assertIn('--container-health-stdin',text);self.assertIn('--no-stream',text)


if __name__ == '__main__': unittest.main()

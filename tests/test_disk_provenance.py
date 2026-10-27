"""Field provenance is independent from the host receipt's freshness."""
from datetime import datetime, timezone
import copy
import tempfile
from pathlib import Path
import unittest
import uuid
from paper_agents import observability_transport as t
from tests.test_observability_transport import fixture

class DiskProvenanceTests(unittest.TestCase):
    def test_disk_field_boundaries_and_invalid_provenance(self):
        clock = 1793060000
        for age, visible in ((2700, True), (2701, False), (-300, True), (-301, False)):
            source = fixture(clock)['status']
            source['latest']['observation_times'] = {'disk': datetime.fromtimestamp(clock-age, timezone.utc).isoformat()}
            projected = t.project(source, clock=clock)
            self.assertEqual(projected['status']['latest']['disk'] is not None, visible)
        source = fixture(clock)['status']
        source['latest'].pop('disk_observed_at')
        self.assertIsNone(t.project(source, clock=clock)['status']['latest']['disk'])
        source['latest']['observation_times'] = {'disk':'SECRET-invalid-time'}
        projected = t.project(source, clock=clock)
        self.assertIsNone(projected['status']['latest']['disk'])
        self.assertNotIn('SECRET', t.encoded(projected).decode())

    def test_disk_expires_independently_after_delivery_and_legacy_is_unknown(self):
        clock = 1793060000
        source = fixture(clock)['status']
        source['latest']['observation_times'] = {'disk': datetime.fromtimestamp(clock-2690, timezone.utc).isoformat()}
        payload = t.project(source, clock=clock)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            t.ingest(path,t.encoded(payload),str(uuid.uuid4()),clock=clock)
            self.assertIsNotNone(t.health_status(path,clock=clock)['latest']['disk'])
            self.assertIsNone(t.health_status(path,clock=clock+11)['latest']['disk'])
            legacy = copy.deepcopy(payload)
            legacy['status']['latest'].pop('disk_observed_at')
            t.validate(legacy,clock=clock)
            other = Path(directory)/'legacy.json'
            t.ingest(other,t.encoded(legacy),str(uuid.uuid4()),clock=clock)
            self.assertIsNone(t.health_status(other,clock=clock)['latest']['disk'])

    def test_future_wire_provenance_is_rejected(self):
        clock = 1793060000
        payload = fixture(clock)
        payload['status']['latest']['disk_observed_at'] = datetime.fromtimestamp(clock+301,timezone.utc).isoformat()
        with self.assertRaises(t.TransportError):t.validate(payload,clock=clock)

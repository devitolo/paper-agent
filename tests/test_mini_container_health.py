import copy
from datetime import datetime, timezone
import tempfile
from pathlib import Path
import time
import unittest
import uuid

from paper_agents import observability_transport as t, web
from tests.test_observability_transport import fixture


def snapshot():
    return {'state':'observed','observed_at':datetime.now(timezone.utc).isoformat(), 'items':[
        {'service': service, 'status':'running','health':'healthy','image_digest':'sha256:'+'b'*64,
         'image_id':'sha256:'+'a'*64,'cpu_percent':0.25,'memory_bytes':5767168,'memory_limit_bytes':268435456}
        for service in t.CONTAINER_SERVICES]}


class MiniContainerHealthTests(unittest.TestCase):
    def test_delivery_to_health_renders_four_fields_without_touching_state(self):
        clock=time.time();value=fixture(clock);value['status']['latest']['containers']=snapshot()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json'
            t.ingest(path,t.encoded(value),str(uuid.uuid4()),clock=clock)
            before=path.read_bytes()
            html=web.render_mini_container_health(t.health_status(path,clock=clock))
            for expected in ('Production app','Observability publisher','healthy','sha256:'+'b'*64,'<th>CPU</th><th>Memory</th>','5.5 / 256.0 MiB'):
                self.assertIn(expected,html)
            self.assertNotIn('<th>Image Digest</th>',html)
            self.assertEqual(html.count('<details class="container-details">'),6)
            self.assertNotIn('<details open',html)
            self.assertEqual(path.read_bytes(),before)

    def test_old_container_observation_hidden_despite_fresh_host(self):
        value=snapshot();value['observed_at']='2026-01-01T00:00:00+00:00'
        html=web.render_mini_container_health({'freshness':'fresh','latest':{'containers':value}})
        self.assertIn('No fresh Mini container observations',html)
        self.assertNotIn('sha256:',html)

    def test_no_healthcheck_and_scheduled_job_are_not_claimed_healthy(self):
        value=snapshot();value['items'][2]['health']='no-healthcheck'
        item=value['items'][-1];item.update(status='unavailable',health='unavailable',image_digest=None,image_id=None,cpu_percent=None,memory_bytes=None,memory_limit_bytes=None)
        html=web.render_mini_container_health({'freshness':'fresh','latest':{'containers':value}})
        self.assertIn('No health check configured',html)
        self.assertIn('Scheduled',html)
        self.assertIn('<td>N/A</td><td>Unavailable</td>',html)

    def test_scheduled_job_checks_use_accepted_data_and_preserve_explicit_failures(self):
        value=snapshot()
        for item in value['items'][-2:]:
            item['status']='unavailable';item['health']='unavailable'
        html=web.render_mini_container_health({'freshness':'fresh','received_at':value['observed_at'],'latest':{'containers':value}})
        self.assertIn('Last collection OK',html)
        self.assertIn('Last delivery accepted',html)
        html=web.render_mini_container_health({'freshness':'stale','latest':{'containers':value}})
        self.assertNotIn('Last collection OK',html)
        self.assertNotIn('Last delivery accepted',html)
        for item in value['items'][-2:]:item['health']='unhealthy'
        html=web.render_mini_container_health({'freshness':'fresh','received_at':value['observed_at'],'latest':{'containers':value}})
        self.assertNotIn('Last collection OK',html)
        self.assertIn('unhealthy',html)

    def test_old_payload_stays_accepted_and_bad_new_fields_fail_closed(self):
        value=fixture(time.time());t.validate(value)
        value['status']['latest']['containers']=snapshot()
        value['status']['latest']['containers']['items'][0]['config']={'secret':'sentinel'}
        with self.assertRaises(t.TransportError):t.validate(value)


if __name__=='__main__':unittest.main()

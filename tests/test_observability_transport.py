import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import uuid

from paper_agents import observability_transport as t
from paper_agents.observability_publisher import publish_once


def fixture(clock=None):
    now = datetime.fromtimestamp(clock or time.time(), timezone.utc)
    end = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0)
    status = {
        'schema': t.STATUS_SCHEMA, 'host': 'mini-production', 'environment': 'mini-production', 'scope': 'host', 'state': 'observed', 'collector_version': '4', 'observed_at': now.isoformat(),
        'window': {'start_at': (end-timedelta(seconds=900)).isoformat(), 'end_at': end.isoformat(), 'interval_seconds': 900, 'complete': True, 'sample_gaps': 0},
        'sample_count': 2, 'coverage_seconds': 900,
        'cpu': {'average_percent': 0, 'max_observed_percent': 0, 'peak_sustained_5m_percent': None, 'max_one_minute_percent': None},
        'memory': {'available_average_bytes': 0, 'available_max_bytes': 0, 'available_min_bytes': 0},
        'latest': {'observed_at': now.isoformat(), 'disk': {'capacity_bytes': 1000, 'available_bytes': 500, 'used_percent': 50, 'inode_used_percent': 5}, 'containers': {'state': 'unconfigured'}, 'temperature': {'state': 'unavailable'}, 'cgroup': {'state': 'unavailable'}},
    }
    return {'schema': t.SCHEMA, 'producer': 'mini-production', 'status': status, 'events': []}


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root/'mini-state.json'
        self.payload = fixture()
        self.body = t.encoded(self.payload)
        self.id = str(uuid.uuid4())
        self.key = b'x'*32

    def test_allowlist_projection_removes_paths_and_credential_dump(self):
        source = copy.deepcopy(self.payload['status'])
        source['secrets'] = {'password': 'never transmit'}
        source['latest']['disk']['path'] = '/private'
        projected = t.project(source)
        self.assertNotIn('password', t.encoded(projected).decode())
        self.assertNotIn('/private', t.encoded(projected).decode())
        self.assertEqual(projected, self.payload)

    def test_strict_types_unknown_fields_nonfinite_and_oversize(self):
        for key, value in [('average_percent', True), ('average_percent', 101), ('average_percent', 10**1000), ('max_observed_percent', [])]:
            with self.subTest(key=key, value=type(value).__name__):
                p = copy.deepcopy(self.payload);p['status']['cpu'][key] = value
                with self.assertRaises(t.TransportError):t.validate(p)
        for p in ({**self.payload, 'secrets': {}}, {**self.payload, 'producer': 'aws-production'}):
            with self.assertRaises(t.TransportError):t.validate(p)
        for raw in (b'{"a":1,"a":2}', b'{"x":NaN}', b'x'*(t.MAX_BYTES+1), b'['*10000):
            with self.assertRaises(t.TransportError):t.decode(raw)

    def test_timestamp_window_state_and_event_controls(self):
        cases = []
        for delta in (-2701, 301):
            p = copy.deepcopy(self.payload);p['status']['observed_at'] = datetime.fromtimestamp(time.time()+delta, timezone.utc).isoformat();cases.append(p)
        p = copy.deepcopy(self.payload);p['status']['window']['sample_gaps'] = 1;cases.append(p)
        p = copy.deepcopy(self.payload);p['events'] = [{'id': self.id, 'observed_at': p['status']['observed_at'], 'source': 'collector', 'state': 'failed', 'code': 'timeout', 'message': 'secret'}];cases.append(p)
        p = copy.deepcopy(self.payload);p['status']['latest']['containers']['state'] = [];cases.append(p)
        for p in cases:
            with self.assertRaises(t.TransportError):t.validate(p)
        p = copy.deepcopy(self.payload);p['events'] = [{'id': self.id, 'observed_at': p['status']['observed_at'], 'source': 'collector', 'state': 'failed', 'code': 'timeout'}]
        self.assertEqual(t.validate(p), p)

    def test_authentication_exact_bytes_key_id_and_time(self):
        clock = int(time.time());stamp = str(clock)
        headers = {'X-Paper-Observability-Timestamp': stamp, 'X-Paper-Observability-Id': self.id, 'X-Paper-Observability-Signature': t.signature(self.key, self.body, stamp, self.id)}
        self.assertEqual(t.authenticate(headers, self.body, self.key, clock=clock), self.id)
        for body, key, when in ((self.body+b' ', self.key, clock), (self.body, b'y'*32, clock), (self.body, self.key, clock+301)):
            with self.assertRaises(t.TransportError):t.authenticate(headers, body, key, clock=when)

    def test_replay_conflicts_and_older_status_cannot_replace_latest(self):
        self.assertFalse(t.ingest(self.path, self.body, self.id)['duplicate'])
        before = self.path.read_bytes()
        self.assertTrue(t.ingest(self.path, self.body, self.id)['duplicate'])
        self.assertEqual(before, self.path.read_bytes())
        p = copy.deepcopy(self.payload);p['status']['cpu']['max_observed_percent'] = 5
        for body, identity in ((t.encoded(p), self.id), (t.encoded(p), str(uuid.uuid4())), (t.encoded(fixture(time.time()-60)), str(uuid.uuid4()))):
            with self.assertRaises(t.TransportError) as error:t.ingest(self.path, body, identity)
            self.assertEqual(error.exception.code, 409)
        self.assertEqual(before, self.path.read_bytes())

    def test_concurrent_ingest_keeps_newest_status(self):
        payloads = [t.encoded(fixture(time.time()-index)) for index in range(20)]
        def submit(body):
            try:return t.ingest(self.path, body, str(uuid.uuid4()))
            except t.TransportError as error:self.assertEqual(error.code, 409)
        with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(submit, payloads))
        self.assertEqual(t.read_state(self.path)['payload'], t.decode(payloads[0]))

    def test_duplicate_new_id_cannot_later_be_reused_with_other_content(self):
        t.ingest(self.path, self.body, self.id)
        other = str(uuid.uuid4())
        self.assertTrue(t.ingest(self.path, self.body, other)['duplicate'])
        with self.assertRaises(t.TransportError) as error:
            t.ingest(self.path, t.encoded(fixture(time.time()+1)), other)
        self.assertEqual(error.exception.code, 409)

    def test_receipt_retention_is_bounded(self):
        for _ in range(260):t.ingest(self.path, self.body, str(uuid.uuid4()))
        self.assertEqual(len(t.read_state(self.path)['receipts']), 256)
        self.assertLess(self.path.stat().st_size, 131072)

    def test_atomic_replace_failure_preserves_prior_and_retry_succeeds(self):
        t.ingest(self.path, self.body, self.id);before = self.path.read_bytes()
        body = t.encoded(fixture(time.time()+1))
        identity = str(uuid.uuid4())
        with patch.object(t.os, 'replace', side_effect=OSError('injected')):
            with self.assertRaises(OSError):t.ingest(self.path, body, identity)
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(list(self.root.glob('.telemetry-*')))
        self.assertFalse(t.ingest(self.path, body, identity)['duplicate'])

    def test_corrupt_persisted_state_fails_closed(self):
        self.path.write_text('{broken')
        with self.assertRaises(t.TransportError):t.ingest(self.path, self.body, self.id)
        self.assertEqual(self.path.read_text(), '{broken')
        self.assertEqual(t.health_status(self.path)['state'], 'unavailable')

    def test_health_uses_resource_time_and_is_read_only(self):
        t.ingest(self.path, self.body, self.id)
        before = self.path.read_bytes()
        clock = t.stamp(self.payload['status']['observed_at'])
        self.assertEqual(t.health_status(self.path, clock=clock+2701)['state'], 'stale')
        self.assertEqual(t.health_status(self.path, clock=clock)['cpu']['average_percent'], 0)
        self.assertEqual(before, self.path.read_bytes())

    def test_publisher_disabled_touches_no_file_or_network(self):
        with patch.dict(os.environ, {'PAPER_OBSERVABILITY_PUBLISH_ENABLED': '0'}):
            self.assertEqual(publish_once('/missing', '/missing', 'bad', '/missing'), {'state': 'disabled'})

    def test_failed_delivery_persists_exact_retry_body_and_id(self):
        status = self.root/'status.json';status.write_bytes(t.encoded(self.payload['status']))
        key = self.root/'transport.key';key.write_text('aa'*32)
        pending = self.root/'pending.json'
        with patch.dict(os.environ, {'PAPER_OBSERVABILITY_PUBLISH_ENABLED': '1'}), patch('urllib.request.OpenerDirector.open', side_effect=OSError('offline')), patch('time.sleep'):
            for _ in range(2):
                with self.assertRaises(t.TransportError):publish_once(status,pending,'https://localhost'+t.ENDPOINT,key)
                if _ == 0:before = pending.read_bytes()
                else:self.assertEqual(before,pending.read_bytes())
            with self.assertRaises(t.TransportError):publish_once(status,pending,'http://localhost'+t.ENDPOINT,key)


class HTTPSIntegrationTests(unittest.TestCase):
    def test_real_https_publisher_ingest_atomic_store_and_disable(self):
        # Test-only self-signed certificate; no production secrets or endpoints.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);cert=root/'cert.pem';private=root/'key.pem'
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,IP:127.0.0.1','-keyout',str(private),'-out',str(cert)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            key=root/'transport.key';key.write_text('ab'*32)
            state=root/'mini-state.json';status=root/'status.json';status.write_bytes(t.encoded(fixture()['status']))
            from paper_agents.web import make_handler
            class Handler(make_handler(root/'paper-must-not-exist.db')):
                def log_message(self,*args):pass
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,private)
            server.socket=context.wrap_socket(server.socket,server_side=True)
            worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
            url='https://127.0.0.1:'+str(server.server_port)+t.ENDPOINT
            client_context=ssl.create_default_context(cafile=str(cert))
            settings={'PAPER_OBSERVABILITY_PUBLISH_ENABLED':'1','PAPER_OBSERVABILITY_INGEST_ENABLED':'1','PAPER_OBSERVABILITY_INGEST_KEY_FILE':str(key),'PAPER_OBSERVABILITY_STATE_FILE':str(state)}
            try:
                with patch.dict(os.environ,settings),patch('paper_agents.observability_publisher.ssl.create_default_context',return_value=client_context):
                    result=publish_once(status,root/'pending.json',url,key)
                    self.assertEqual(result['state'],'delivered')
                    self.assertFalse((root/'pending.json').exists())
                    accepted=t.health_status(state)
                    self.assertEqual(accepted['cpu']['average_percent'],0)
                    self.assertEqual(accepted['latest']['disk']['used_percent'],50)
                    before=state.read_bytes()
                    # Test the actual serving route over TLS, with no paper DB.
                    for extra, expected in (({'X-Paper-Observability-Signature':'0'*64},401), ({'Content-Encoding':'gzip'},400), ({'Content-Length':str(t.MAX_BYTES+1)},413), ({'Content-Type':'text/plain'},415)):
                        identity=str(uuid.uuid4());timestamp=str(int(time.time()));body=t.encoded(fixture())
                        headers={'Content-Type':'application/json','X-Paper-Observability-Id':identity,'X-Paper-Observability-Timestamp':timestamp,'X-Paper-Observability-Signature':t.signature(bytes.fromhex('ab'*32),body,timestamp,identity),**extra}
                        request=urllib.request.Request(url,data=body,headers=headers,method='POST')
                        with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(request,context=client_context,timeout=5)
                        self.assertEqual(error.exception.code,expected);error.exception.close()
                    self.assertFalse((root/'paper-must-not-exist.db').exists())
                    with patch.dict(os.environ,{'PAPER_OBSERVABILITY_INGEST_ENABLED':'0'}):
                        with self.assertRaises(t.TransportError) as error:publish_once(status,root/'pending.json',url,key)
                        self.assertEqual(error.exception.code,404)
                    self.assertEqual(before,state.read_bytes())
            finally:
                server.shutdown();server.server_close();worker.join(5)


if __name__ == '__main__':unittest.main()

import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from paper_agents import aws_container_status as c


def facts():
    rows=[]
    for service in c.SERVICES:
        rows += [json.dumps({'status':'running','health':'healthy','image_id':'sha256:'+'a'*64}),
                 json.dumps({'image_id':'sha256:'+'a'*64,'digests':['ghcr.io/devitolo/paper-agent@sha256:'+'b'*64]}),
                 json.dumps({'cpu':'0%','memory':'5.5MiB / 64MiB'})]
    return ('\n'.join(rows)+'\n').encode()


class AWSContainerStatusTests(unittest.TestCase):
    def test_roundtrip_is_read_only_and_preserves_zero_metrics(self):
        value=c.from_stream(io.BytesIO(facts()))
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'container-status.json';c.publish(path,value)
            before=path.read_bytes();result=c.load(path)
            self.assertEqual(before,path.read_bytes())
            self.assertEqual(path.stat().st_mode&0o777,0o644)
            self.assertEqual(result['freshness'],'fresh')
            self.assertEqual(result['items'][0]['cpu_percent'],0)
            self.assertEqual(result['items'][0]['memory_bytes'],5767168)
            self.assertEqual(result['items'][0]['image_digest'],'sha256:'+'b'*64)

    def test_stale_future_wrong_target_and_sensitive_keys_fail_closed(self):
        base=c.from_stream(io.BytesIO(facts()));now=datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'container-status.json'
            for changed in ({**base,'environment':'mini-production'},{**base,'schema':'other'},{**base,'config':{'API_KEY':'secret'}}):
                c.publish(path,changed);self.assertEqual(c.load(path)['freshness'],'unavailable')
            for offset,state in ((-2701,'stale'),(301,'unavailable')):
                changed={**base,'observed_at':(now+timedelta(seconds=offset)).isoformat()}
                c.publish(path,changed);result=c.load(path,now=now)
                self.assertEqual(result['freshness'],state);self.assertIsNone(result['items'])

    def test_bad_numbers_and_service_inventory_are_rejected(self):
        base=c.from_stream(io.BytesIO(facts()))
        for key,value in (('cpu_percent',True),('cpu_percent',float('nan')),('memory_bytes',2**80),('image_digest','<script>secret</script>'),('service','minilm'),('status',{})):
            changed=copy.deepcopy(base);changed['items'][0][key]=value
            with self.subTest(key=key):
                with self.assertRaises(ValueError):c.validate(changed)
        changed=copy.deepcopy(base);changed['items'][1]=changed['items'][0]
        with self.assertRaises(ValueError):c.validate(changed)

    def test_missing_file_is_read_only_and_oversize_or_deep_files_are_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'status.json';self.assertEqual(c.load(path)['freshness'],'unavailable')
            self.assertFalse(path.exists())
            for raw in ('x'*8193,'['*1500+']'*1500,'{bad'):
                path.write_text(raw);self.assertEqual(c.load(path)['freshness'],'unavailable')

    def test_stopped_and_missing_containers_never_claim_live_resource_metrics(self):
        rows=facts().splitlines();rows[0]=b'{"status":"exited","health":"unavailable"}';rows[3:]=[b'{}',b'{}',b'{}']
        value=c.from_stream(io.BytesIO(b'\n'.join(rows)))
        for item in value['items']:self.assertIsNone(item['cpu_percent']);self.assertIsNone(item['memory_bytes'])
        self.assertEqual(value['items'][1]['status'],'unavailable')

    def test_image_lookup_race_does_not_attach_wrong_digest(self):
        rows=facts().splitlines();rows[1]=json.dumps({'image_id':'sha256:'+'c'*64,'digests':['example@sha256:'+'d'*64]}).encode()
        value=c.from_stream(io.BytesIO(b'\n'.join(rows)))
        self.assertIsNone(value['items'][0]['image_digest'])
        self.assertEqual(value['items'][0]['image_id'],'sha256:'+'a'*64)

    def test_bad_or_incomplete_input_does_not_replace_existing_export(self):
        for raw in (b'x'*8193,b'{}\n'):
            with self.assertRaises(ValueError):c.from_stream(io.BytesIO(raw))


if __name__=='__main__':unittest.main()

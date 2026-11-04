"""Rejected identity batches are evidence, never successful/retryable deliveries."""
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from paper_agents import whole_cycle_exchange as exchange
from paper_agents.db import init_db
from paper_agents.sync_exchange import connect_db

MESSAGE = 'source alias belongs to a different paper'


def rejection(code=422, message=MESSAGE):
    return HTTPError('https://aws/sync/pipeline-results', code, 'Unprocessable Entity', {},
                     io.BytesIO(json.dumps({'error': message}).encode()))


class IdentityHoldTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root/'state'; self.state.mkdir()
        self.db = self.root/'paper_agent.db'; init_db(self.db)
        self.raw = b'{"batch_id":"held33","input_revision":"revision","run_sequence":33}'
        (self.state/'pipeline-outbox.json').write_bytes(self.raw)
        (self.state/'active-cycle.json').write_text('{"batch_id":"held33"}')
        (self.state/'sequence.json').write_text('{"sequence":32}')

    def hold(self, error=None):
        return exchange.hold_identity_outbox(self.db,self.root,self.state,error or rejection())

    def test_preserves_exact_payload_database_artifact_and_ack(self):
        artifact = self.root/'summary.json'; artifact.write_bytes(b'{"summary":"retained"}')
        with connect_db(self.db) as c:
            pid=c.execute("INSERT INTO papers(canonical_key,title) VALUES('held','Retained')").lastrowid
            c.execute("INSERT INTO artifacts(paper_id,artifact_type,path) VALUES(?,'triage_summary',?)",(pid,str(artifact)))
        result=self.hold(); capsule=self.state/result['capsule']
        self.assertEqual(result['status'],'held-needs-review')
        self.assertEqual((capsule/'pipeline-outbox.json').read_bytes(),self.raw)
        manifest=json.loads((capsule/'manifest.json').read_text())
        self.assertFalse(manifest['automatic_replay'])
        self.assertEqual((capsule/manifest['artifacts'][0]['file']).read_bytes(),artifact.read_bytes())
        with sqlite3.connect(capsule/'paper_agent.db') as c:
            self.assertEqual(c.execute('SELECT title FROM papers').fetchone()[0],'Retained')
        self.assertEqual(json.loads((self.state/'sequence.json').read_text())['sequence'],32)
        self.assertFalse((self.state/'pipeline-outbox.json').exists())
        self.assertEqual(exchange.next_sequence(self.state),34)
        self.assertEqual(exchange.next_sequence(self.state),35)
        self.assertEqual(exchange.resume_identity_hold(self.state)['batch_id'],'held33')

    def test_only_exact_identity_422_is_classified(self):
        for code, message in [(500,MESSAGE),(401,MESSAGE),(422,'unknown input revision'),
                              (422,'prefix '+MESSAGE),(422,'stale result sequence')]:
            with self.subTest(code=code,message=message):
                self.assertIsNone(self.hold(rejection(code,message)))
                self.assertEqual((self.state/'pipeline-outbox.json').read_bytes(),self.raw)

    def test_real_http_html_error_is_classified(self):
        error=HTTPError('https://aws',422,'Unprocessable Entity',{},io.BytesIO(
            ('<html><p>Message: '+MESSAGE+'.</p></html>').encode()))
        self.assertEqual(self.hold(error)['status'],'held-needs-review')

    def test_failed_backup_never_clears_pending_slot(self):
        with patch.object(exchange.sqlite3,'connect',side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.hold()
        self.assertEqual((self.state/'pipeline-outbox.json').read_bytes(),self.raw)
        self.assertFalse((self.state/'identity-restore-required.json').exists())

    def test_crash_after_capsule_commit_retries_idempotently(self):
        original=exchange.atomic_json
        def fail(path,value):
            if Path(path).name=='identity-restore-required.json': raise OSError('crash')
            original(path,value)
        with patch.object(exchange,'atomic_json',side_effect=fail):
            with self.assertRaises(OSError): self.hold()
        self.assertEqual((self.state/'pipeline-outbox.json').read_bytes(),self.raw)
        self.hold()
        self.assertEqual(len(list((self.state/'held').iterdir())),1)

    def test_crash_before_slot_clear_resumes_without_network(self):
        with patch.object(exchange,'resume_identity_hold',side_effect=OSError('crash')):
            with self.assertRaises(OSError): self.hold()
        self.assertTrue((self.state/'pipeline-outbox.json').exists())
        exchange.resume_identity_hold(self.state)
        self.assertFalse((self.state/'pipeline-outbox.json').exists())

    def test_direct_delivery_cannot_send_durably_held_pending_payload(self):
        with patch.object(exchange,'resume_identity_hold',side_effect=OSError('crash')):
            with self.assertRaises(OSError): self.hold()
        with patch.object(exchange,'request') as transport:
            with self.assertRaisesRegex(ValueError,'fresh authoritative input'):
                exchange.deliver_outbox(self.state,'https://aws',self.state/'pipeline-outbox.json',
                                        clear_active=True,transport=transport)
            transport.assert_not_called()
        self.assertEqual((self.state/'pipeline-outbox.json').read_bytes(),self.raw)
        self.assertEqual(json.loads((self.state/'sequence.json').read_text())['sequence'],32)

    def test_evidence_corruption_blocks_slot_clear(self):
        with patch.object(exchange,'resume_identity_hold',side_effect=OSError('crash')):
            with self.assertRaises(OSError): self.hold()
        capsule=next((self.state/'held').iterdir()); (capsule/'pipeline-outbox.json').write_text('bad')
        with self.assertRaisesRegex(ValueError,'integrity'): exchange.resume_identity_hold(self.state)
        self.assertEqual((self.state/'pipeline-outbox.json').read_bytes(),self.raw)

    def test_failed_restore_stays_required(self):
        self.hold()
        with patch.object(exchange,'download_backup',side_effect=OSError('offline')):
            with self.assertRaises(OSError): exchange.pull_after_identity_hold(self.db,self.root,self.state,'https://aws')
        self.assertTrue((self.state/'identity-restore-required.json').exists())

    def test_health_cannot_publish_held_history_before_restore(self):
        self.hold()
        with self.assertRaisesRegex(ValueError,'blocks operational'):
            exchange.publish_health(self.db,self.root,self.state,'https://aws')

    def test_invalid_sequence_fails_closed(self):
        (self.state/'reserved-sequence.json').write_text('{"sequence":true}')
        with self.assertRaisesRegex(ValueError,'invalid sequence'): exchange.next_sequence(self.state)

    def test_partial_artifact_copy_failure_preserves_original(self):
        artifact=self.root/'summary'; artifact.write_bytes(b'keep')
        with connect_db(self.db) as c:
            pid=c.execute("INSERT INTO papers(canonical_key,title) VALUES('paper','Paper')").lastrowid
            c.execute("INSERT INTO artifacts(paper_id,artifact_type,path) VALUES(?,'triage_summary',?)",(pid,str(artifact)))
        with patch.object(exchange.shutil,'copyfile',side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.hold()
        self.assertEqual((self.state/'pipeline-outbox.json').read_bytes(),self.raw)
        self.assertFalse((self.state/'identity-restore-required.json').exists())

    def test_uncertain_delivery_is_not_held_or_reallocated(self):
        def lost(*args): raise OSError('ACK lost')
        with patch.dict(os.environ,{'PAPER_AGENT_SYNC_SHARED_SECRET':'test'}):
            with self.assertRaises(OSError):
                exchange.push(self.db,self.root,self.state,'https://aws',transport=lost)
        self.assertEqual((self.state/'pipeline-outbox.json').read_bytes(),self.raw)
        self.assertFalse((self.state/'reserved-sequence.json').exists())
        self.assertFalse((self.state/'held').exists())

    def test_restarted_sequence_reservation_never_reuses_number(self):
        self.hold()
        self.assertEqual(exchange.next_sequence(self.state),34)
        # A crash before writing the new outbox may leave a harmless gap.
        self.assertEqual(exchange.next_sequence(self.state),35)
        self.assertEqual(json.loads((self.state/'sequence.json').read_text())['sequence'],32)

    def test_mismatched_active_cycle_keeps_original_pending(self):
        (self.state/'active-cycle.json').write_text('{"batch_id":"another"}')
        with self.assertRaisesRegex(ValueError,'another cycle'): self.hold()
        self.assertEqual((self.state/'pipeline-outbox.json').read_bytes(),self.raw)


    def test_health_allocator_also_skips_held_sequence(self):
        self.hold()
        (self.state/'identity-restore-required.json').unlink()
        sent=[]
        def transport(url,method,body,secret,path,key):
            sent.append(json.loads(body))
            return {'status':'applied','batch_key':key,'operational_version':1}
        with patch.dict(os.environ,{'PAPER_AGENT_SYNC_SHARED_SECRET':'test'}), \
             patch.object(exchange,'download_routine_snapshot',return_value={'revision':'fresh'}):
            exchange.publish_health(self.db,self.root,self.state,'https://aws',transport=transport)
        self.assertEqual(sent[0]['run_sequence'],34)
        self.assertEqual(json.loads((self.state/'sequence.json').read_text())['sequence'],34)

    def test_restore_failure_after_download_keeps_required_marker(self):
        self.hold()
        with patch.object(exchange,'download_backup',return_value=self.root/'backup'), \
             patch.object(exchange,'pull',side_effect=ValueError('invalid snapshot')):
            with self.assertRaises(ValueError):
                exchange.pull_after_identity_hold(self.db,self.root,self.state,'https://aws')
        self.assertTrue((self.state/'identity-restore-required.json').exists())


class IdentityHoldHttpsTests(unittest.TestCase):
    # Reuse the real TLS receiver/shell fixture without inheriting its test cases.
    from tests.sync_exchange.test_whole_cycle import WholeCycleTests as Fixture
    setUp=Fixture.setUp
    close=Fixture.close
    shell=Fixture.shell
    stages=Fixture.stages

    def test_real_rejection_restore_next_source_receipt(self):
        self.shell('pull')
        with connect_db(self.db) as c:
            c.execute("INSERT INTO paper_sources(paper_id,source,source_id) VALUES(?,'core','same')",(self.pid,))
        with connect_db(self.mdb) as c:
            pid=c.execute("INSERT INTO papers(canonical_key,title) VALUES('doi:new','Duplicate')").lastrowid
            c.execute("INSERT INTO paper_sources(paper_id,source,source_id) VALUES(?,'core','same')",(pid,))
        cycle=json.loads((self.state/'active-cycle.json').read_text())
        payload={'schema':1,'batch_id':cycle['batch_id'],'run_sequence':33,
                 'input_revision':cycle['input_revision'],'producer_id':'mini-production','environment':'production',
                 'results':[], 'normalized':{key:[] for key in exchange.SECTIONS}}
        payload['normalized']['papers']=[{'canonical_key':'doi:new','title':'Duplicate'}]
        payload['normalized']['paper_sources']=[{'canonical_key':'doi:new','source':'core','source_id':'same'}]
        exchange.atomic_json(self.state/'pipeline-outbox.json',payload)
        exchange.atomic_json(self.state/'sequence.json',{'sequence':32})
        original=(self.state/'pipeline-outbox.json').read_bytes()
        result=self.shell('retry')
        self.assertIn('held-needs-review',result.stdout)
        capsule=next((self.state/'held').iterdir())
        self.assertEqual((capsule/'pipeline-outbox.json').read_bytes(),original)
        preserved=self.mini/'data/local.pdf'; preserved.write_bytes(b'local PDF')
        secret=self.mini/'private.env'; secret.write_bytes(b'private identity')
        self.cli_env['PAPER_AGENT_ROUTINE_SYNC']='1'
        helper=self.root/'stage.py'
        helper.write_text("""import os,sys
from pathlib import Path
from types import SimpleNamespace
from tests.sync_exchange.test_whole_cycle import WholeCycleTests
mini=Path(os.environ['MINI_ROOT'])
WholeCycleTests.stages(SimpleNamespace(mini=mini,mdb=mini/'data/paper_agent.db'),(sys.argv[1],))
""")
        self.cli_env['STAGE_HELPER']=str(helper)
        from tests.sync_exchange.test_whole_cycle import REPO
        self.cli_env['PYTHONPATH']=str(REPO)
        result=subprocess.run(['bash',str(REPO/'scripts/mini_container_job.sh'),'arxiv'],
                              cwd=REPO,env=self.cli_env,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('pulling AWS input before arxiv',result.stdout)

        self.assertFalse((self.state/'identity-restore-required.json').exists())
        self.assertEqual(preserved.read_bytes(),b'local PDF')
        self.assertEqual(secret.read_bytes(),b'private identity')
        with sqlite3.connect(capsule/'paper_agent.db') as held_db:
            self.assertIsNotNone(held_db.execute("SELECT 1 FROM papers WHERE canonical_key='doi:new'").fetchone())
        with connect_db(self.mdb) as c:
            self.assertIsNone(c.execute("SELECT 1 FROM papers WHERE canonical_key='doi:new'").fetchone())
            self.assertEqual(c.execute("SELECT value_json FROM openalex_search_state WHERE key='cursor'").fetchone()[0],'{"next":42}')
        self.assertEqual(json.loads((self.state/'sequence.json').read_text())['sequence'],34)
        self.assertEqual((capsule/'pipeline-outbox.json').read_bytes(),original)
        with connect_db(self.db) as c:
            self.assertIsNone(c.execute('SELECT 1 FROM sync_result_batches WHERE batch_key=?',(cycle['batch_id'],)).fetchone())
            self.assertEqual(c.execute('SELECT MAX(sequence) FROM sync_producers').fetchone()[0],34)

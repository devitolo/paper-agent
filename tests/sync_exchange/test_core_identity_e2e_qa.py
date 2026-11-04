"""Exact Mini producer -> separately frozen AWS HTTPS receiver identity checks."""
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
import urllib.error

from paper_agents import db
from paper_agents.scout import CoreSource
from paper_agents.scout_agent import ScoutAgent, ScoutConfig
from paper_agents.whole_cycle_exchange import push
from tests import test_core_progress as core_fixture
from tests.sync_exchange import test_health_operational_e2e_qa as health_fixture


class CoreIdentityCrossRoleQA(unittest.TestCase):
    def setUp(self):
        source=os.environ.get('QA_HEALTH_AWS_SOURCE')
        if not source:raise RuntimeError('QA_HEALTH_AWS_SOURCE must name the separately frozen AWS receiver')
        self.h=health_fixture.OperationalHealthE2EQA()
        self.h.aws_source=Path(source).resolve()
        self.h.setUp();self.addCleanup(self.h.doCleanups)
        self.t=self.h.t
        # Finish the bootstrap fixture pull, then seed authoritative cloud data
        # and obtain the real registered snapshot used by the test cycle.
        self.t.shell('recover')
        with db.connect_db(self.t.db) as connection:
            self.cloud_id=connection.execute("INSERT INTO papers(canonical_key,title,abstract,authors_json) VALUES('core:1','Rich retained title','Retained abstract','[\"Retained author\"]')").lastrowid
            connection.execute("INSERT INTO paper_sources(paper_id,source,source_id,metadata_json) VALUES(?,'core','1','{\"rich\":true}')",(self.cloud_id,))
            connection.execute("INSERT INTO raw_feedback(paper_id,content,content_hash) VALUES(?,'Preserved identity feedback','core-history')",(self.cloud_id,))
            connection.execute('INSERT INTO saved_papers(paper_id) VALUES(?)',(self.cloud_id,))
        self.t.shell('pull')
        # Routine snapshots intentionally omit raw feedback. Preserve an existing
        # Mini-local history row rather than asserting unsupported raw transport.
        with db.connect_db(self.t.mdb) as connection:
            mini_id=connection.execute("SELECT id FROM papers WHERE canonical_key='core:1'").fetchone()[0]
            connection.execute("INSERT INTO raw_feedback(paper_id,content,content_hash) VALUES(?,'Mini retained feedback','mini-history')",(mini_id,))

    def test_enrichment_only_export_applies_on_exact_aws_then_next_signed_pull_keeps_identity(self):
        with db.connect_db(self.t.mdb) as connection:
            mini_id=connection.execute("SELECT id FROM papers WHERE canonical_key='core:1'").fetchone()[0]
            cycle=db.create_workflow_cycle(connection,mode='core',max_scout_attempts=1)
            source=CoreSource(api_key='offline-fixture',request_delay=0,verbose=False)
            source.request_delay=0
            with patch.object(source,'fetch_page',return_value=core_fixture.page([
                    core_fixture.work(1,doi='10.999/cross-role')],next_offset=None)):
                result=ScoutAgent(source).run(connection,workflow_cycle_id=cycle,attempt_number=1,
                    config=ScoutConfig(topics=['incident'],core_progress_enabled=True,max_candidates=1))
            self.assertEqual((result['stored_count'],result['eligible_count']),(0,0))
        self.h.write_event('core','completed')
        self.t.shell('push')
        payload=json.loads(next(self.t.state.glob('published-*.json')).read_text())
        self.assertEqual(payload['results'],[])
        self.assertEqual([p['canonical_key'] for p in payload['normalized']['papers']],['core:1'])
        self.assertEqual(payload['normalized']['artifacts'],[])
        self.assertEqual(payload['normalized']['recommendations'],[])
        with db.connect_db(self.t.db) as connection:
            self.assertEqual(connection.execute("SELECT id,canonical_key,doi,title,abstract,authors_json FROM papers WHERE id=?",(self.cloud_id,)).fetchone(),
                (self.cloud_id,'core:1','10.999/cross-role','Rich retained title','Retained abstract','[\"Retained author\"]'))
            self.assertEqual(connection.execute("SELECT paper_id FROM paper_sources WHERE source='core' AND source_id='1'").fetchone()[0],self.cloud_id)
            self.assertEqual(connection.execute("SELECT paper_id,content FROM raw_feedback WHERE content_hash='core-history'").fetchone(),(self.cloud_id,'Preserved identity feedback'))
            self.assertEqual(connection.execute('SELECT status FROM sync_result_batches').fetchone()[0],'applied')
            self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(),[])
        self.t.shell('pull')
        with db.connect_db(self.t.mdb) as connection:
            self.assertEqual(connection.execute("SELECT id,canonical_key,doi FROM papers WHERE canonical_key='core:1'").fetchone(),
                             (mini_id,'core:1','10.999/cross-role'))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM papers WHERE doi='10.999/cross-role'").fetchone()[0],1)
            self.assertEqual(connection.execute("SELECT paper_id FROM raw_feedback WHERE content_hash='mini-history'").fetchone()[0],mini_id)
            self.assertIsNotNone(connection.execute('SELECT 1 FROM saved_papers WHERE paper_id=?',(mini_id,)).fetchone())

    def test_legacy_conflicting_outbox_gets_actual_422_and_preserves_file_sequence_and_both_databases(self):
        cycle=json.loads((self.t.state/'active-cycle.json').read_text())
        key='doi:10.999/legacy-conflict'
        payload={'schema':1,'producer_id':'mini-production','environment':'production','run_sequence':1,
            'input_revision':cycle['input_revision'],'batch_id':'legacy-conflicting-outbox','results':[],
            'normalized':{'papers':[{'canonical_key':key,'title':'Old pending identity','doi':'10.999/legacy-conflict'}],
                'paper_sources':[{'canonical_key':key,'source':'core','source_id':'1'}],
                'artifacts':[],'evaluations':[],'recommendations':[]}}
        pending=self.t.state/'pipeline-outbox.json';pending.write_text(json.dumps(payload,indent=2))
        sequence=self.t.state/'sequence.json';sequence.write_text('{"sequence":0}\n')
        original=pending.read_bytes();seq=sequence.read_bytes()
        aws_before=self.h.database_dump();mini_before=self.h.mini_database_dump()
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            push(self.t.mdb,self.t.mini,self.t.state,self.t.url)
        self.assertEqual(rejected.exception.code,422)
        rejected.exception.close()
        self.assertEqual(pending.read_bytes(),original)
        self.assertEqual(sequence.read_bytes(),seq)
        self.assertEqual(self.h.database_dump(),aws_before)
        self.assertEqual(self.h.mini_database_dump(),mini_before)
        self.assertEqual(list(self.t.state.glob('published-*.json')),[])


if __name__=='__main__':unittest.main()

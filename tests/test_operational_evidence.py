"""Result-batch operational projection reaches the actual Health renderer."""
from datetime import datetime,timezone,timedelta
from email.message import Message
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_agents import db
from paper_agents.operational_evidence import collect,validate
from paper_agents.sync_exchange import build_input_snapshot,receive_result_batch,encoded,connect_db
from paper_agents.web import render_health_page,daily_funnel_rows,render_daily_trend_svg


class OperationalEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'data').mkdir();(self.root/'config').mkdir()
        (self.root/'config/topics.yaml').write_text('topics: []\n')
        (self.root/'config/industry.json').write_text('{}')
        (self.root/'data/profile.json').write_text('{"interests":[],"positive_signals":[],"negative_signals":[],"feedback_history":[]}')
        self.path=self.root/'data/paper_agent.db';db.init_db(self.path)
        self.env=patch.dict(os.environ,{'PAPER_AGENT_SYNC_PRODUCER':'mini-production',
            'PAPER_AGENT_SYNC_ENVIRONMENT':'production','PAPER_AGENT_SYNC_SHARED_SECRET':'s'*32,
            'PAPER_AGENT_ORCHESTRATOR_STATUS_FILE':str(self.root/'missing-status')})
        self.env.start();self.addCleanup(self.env.stop)
        self.now=datetime.now(timezone.utc).isoformat()
        self.facts={'version':1,'events':[{'recorded_at':self.now,'job':'arxiv','status':'completed'}],
            'scout_runs':[{'run_id':91,'source':'arxiv','started_at':self.now,'completed_at':self.now,
                           'candidate_count':7,'eligible_count':3,'excluded_count':4}]}
        self.revision=build_input_snapshot(self.path,self.root)['revision']

    def receive(self,facts='default',key='fixture'):
        payload={'schema':1,'producer_id':'mini-production','environment':'production','run_sequence':1,
                 'input_revision':self.revision,'results':[]}
        if facts=='default':facts=self.facts
        if facts is not None:payload['operational']=facts
        headers=Message();headers['X-Paper-Sync-Idempotency']=key
        return receive_result_batch(self.path,encoded(payload),headers,self.root)

    def summary(self,**kwargs):return db.health_summary(self.path,**kwargs)

    def test_batch_to_health_with_no_business_results(self):
        receipt=self.receive()
        self.assertEqual(receipt['operational_version'],1)
        s=self.summary();self.assertEqual(s['daily']['scout'][0]['candidate_count'],7)
        self.assertEqual(s['job_events'][0]['delivery'],'accepted')
        self.assertEqual(s['top']['latest_run_time'],self.now)
        self.assertFalse(any('No workflow cycle has been recorded' in w['message'] for w in s['warnings']))
        html=render_health_page(self.path,tab='jobs')
        self.assertIn('arxiv',html);self.assertIn('accepted',html)
        self.assertNotIn('AWS sync status is unavailable',html)
        self.assertIn(self.revision,html)
        self.assertNotIn('No rows in this range.</td></tr></tbody></table>',html.split('Operational job history')[1].split('</section>')[0])

    def test_identical_replay_does_not_double_count(self):
        self.receive();self.assertTrue(self.receive()['replayed'])
        self.assertEqual(self.summary()['daily']['scout'][0]['candidate_count'],7)
        self.assertEqual(len(self.summary()['job_events']),1)

    def test_observed_zero_is_zero_not_missing(self):
        facts=copy.deepcopy(self.facts)
        facts['scout_runs'][0].update(candidate_count=0,eligible_count=0,excluded_count=0)
        self.receive(facts)
        self.assertEqual(self.summary()['daily']['scout'][0]['candidate_count'],0)
        self.assertIn('>0<',render_health_page(self.path,tab='analytics'))

    def test_older_batch_without_facts_still_has_accepted_receipt(self):
        self.receive(None)
        s=self.summary();self.assertFalse(s['operational_evidence_available'])
        self.assertEqual(s['job_events'],[])
        self.assertNotIn('AWS sync status is unavailable',render_health_page(self.path,tab='jobs'))

    def test_missing_scout_counts_are_na_not_zero(self):
        summary={'daily':{'scout':[],'curator':[{'day':'2026-10-29','recommendation_count':2}]}}
        rows=daily_funnel_rows(summary)
        self.assertIsNone(rows[0]['candidate_count'])
        self.assertIn('N/A',render_daily_trend_svg(rows))
        self.assertNotIn('Candidates 2026-10-29: 0',render_daily_trend_svg(rows))

    def test_source_and_range_filtering(self):
        self.receive()
        self.assertEqual(self.summary(source='openalex')['job_events'],[])
        old=copy.deepcopy(self.facts)
        oldtime=(datetime.now(timezone.utc)-timedelta(days=10)).isoformat()
        old['events'][0]['recorded_at']=oldtime
        old['scout_runs'][0].update(started_at=oldtime,completed_at=oldtime)
        # Direct validation verifies retained older history remains a valid payload.
        validate(old)
        with connect_db(self.path) as c:
            c.execute("UPDATE sync_result_batches SET payload_json=json_set(payload_json,'$.operational',json(?))",(json.dumps(old),))
        self.assertEqual(self.summary(days=7)['job_events'],[])
        self.assertEqual(len(self.summary(days=21)['job_events']),1)

    def test_unknown_sensitive_unbounded_and_future_fields_rejected_atomically(self):
        variants=[]
        p=copy.deepcopy(self.facts);p['events'][0]['message']='secret';variants.append(p)
        p=copy.deepcopy(self.facts);p['scout_runs'][0]['eligible_count']=99;variants.append(p)
        p=copy.deepcopy(self.facts);p['scout_runs'][0]['candidate_count']=True;variants.append(p)
        p=copy.deepcopy(self.facts);p['events'][0]['recorded_at']=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat();variants.append(p)
        p=copy.deepcopy(self.facts);p['events']*=201;variants.append(p)
        for bad in variants:
            with self.subTest(bad=bad),self.assertRaises(ValueError):self.receive(bad)
        with connect_db(self.path) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM sync_result_batches').fetchone()[0],0)

    def test_collector_reads_counts_and_strips_raw_event_message(self):
        with connect_db(self.path) as c:
            cycle=c.execute("INSERT INTO workflow_cycles(state) VALUES('completed')").lastrowid
            c.execute('INSERT INTO scout_runs(workflow_cycle_id,source,target_candidates,max_candidates,freshness_months,completed_at) VALUES(?,?,?,?,?,?)',
                (cycle,'core',20,30,24,self.now))
        (self.root/'data/job-events.jsonl').write_text(json.dumps({'recorded_at':self.now,'job':'core','status':'completed','message':'private fixture secret'})+'\n')
        with connect_db(self.path) as c:
            c.row_factory=__import__('sqlite3').Row
            facts=collect(c,self.root,{'started_at':self.now,'baseline':{'workflow_cycles':0}})
        self.assertEqual(facts['scout_runs'][0]['candidate_count'],0)
        self.assertNotIn('secret',json.dumps(facts))
        self.assertEqual(len(facts['events']),1)

    def test_job_without_scout_is_selectable_and_not_claimed_as_processing_success(self):
        facts={'version':1,'events':[{'recorded_at':self.now,'job':'core','status':'failed'}],'scout_runs':[]}
        self.receive(facts)
        summary=self.summary(source='core')
        self.assertIn('core',summary['available_sources'])
        self.assertEqual(summary['job_events'][0]['status'],'failed')
        html=render_health_page(self.path,source_value='core',tab='jobs')
        self.assertIn('value="core" selected',html)
        self.assertIn('failed',html)
        self.assertIn('accepted',html)

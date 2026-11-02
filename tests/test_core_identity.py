import json
from datetime import datetime, timedelta, timezone
from email.message import Message
import os
from pathlib import Path
import sqlite3
from unittest.mock import patch
import unittest
import urllib.error

from paper_agents import db
from paper_agents.curator_agent import CuratorAgent, CuratorConfig
from paper_agents.scout import core_work_to_candidate
from paper_agents.sync_exchange import build_routine_snapshot, apply_routine_snapshot, register_routine_revision, receive_result_batch, encoded, _source
from paper_agents.whole_cycle_exchange import export_delta, push
from tests import test_core_progress as fixture


class CoreIdentityTests(unittest.TestCase):
    setUp = fixture.CoreProgressTests.setUp
    tearDown = fixture.CoreProgressTests.tearDown
    progress = fixture.CoreProgressTests.progress
    fetch = fixture.CoreProgressTests.fetch
    save = fixture.CoreProgressTests.save
    run_scout = fixture.CoreProgressTests.run_scout

    def known(self, **changes):
        paper_id, _ = db.upsert_paper(self.connection, core_work_to_candidate(fixture.work(1, **changes)).as_dict())
        self.connection.commit()
        return paper_id

    def test_imported_core_id_enrichment_keeps_identity_and_history_without_new_recommendation(self):
        root=Path(self.temp.name); cloud=root/'aws';(cloud/'data').mkdir(parents=True);(cloud/'config').mkdir()
        (cloud/'config/topics.yaml').write_text('topics: []\n');(cloud/'config/industry.json').write_text('{}')
        (cloud/'data/profile.json').write_text('{}')
        cloud_db=cloud/'data/paper_agent.db';db.init_db(cloud_db)
        with sqlite3.connect(cloud_db) as con:
            db.upsert_paper(con,core_work_to_candidate(fixture.work(1)).as_dict())
        snapshot=build_routine_snapshot(cloud_db,cloud)
        register_routine_revision(cloud_db,snapshot)
        apply_routine_snapshot(self.path,snapshot,root)
        paper_id,key=self.connection.execute('SELECT id,canonical_key FROM papers').fetchone()
        old_cycle=db.create_workflow_cycle(self.connection,mode='history',max_scout_attempts=1)
        curator=self.connection.execute('INSERT INTO curator_runs(workflow_cycle_id) VALUES(?)',(old_cycle,)).lastrowid
        rec=self.connection.execute("INSERT INTO recommendations(curator_run_id,paper_id,recommendation_order,rationale) VALUES(?,?,1,'retained')",(curator,paper_id)).lastrowid
        self.connection.execute("INSERT INTO raw_feedback(paper_id,recommendation_id,content,content_hash) VALUES(?,?,'retained feedback','history')",(paper_id,rec))
        self.connection.execute("INSERT INTO feedback(paper_id,status,notes) VALUES(?,'reviewed','retained review')",(paper_id,))
        self.connection.execute("INSERT INTO artifacts(paper_id,artifact_type,path) VALUES(?,'triage_summary','retained-summary.json')",(paper_id,))
        self.connection.commit()
        tables=('recommendations','raw_feedback','feedback','artifacts')
        history={t:self.connection.execute('SELECT * FROM '+t).fetchall() for t in tables}
        result, fetched=self.run_scout([fixture.page([fixture.work(1,doi='https://doi.org/10.999/ENRICHED')],next_offset=1),fixture.page([],offset=1,next_offset=None)])
        self.assertEqual(fetched.call_count,2)
        self.assertEqual((result['stored_count'],result['eligible_count']),(0,0))
        self.assertEqual(self.connection.execute('SELECT id,canonical_key,doi FROM papers').fetchall(),[(paper_id,key,'10.999/enriched')])
        cycle=self.connection.execute('SELECT MAX(id) FROM workflow_cycles').fetchone()[0]
        candidates=db.eligible_candidates_for_cycle(self.connection,cycle)
        reviewed=CuratorAgent().run(self.connection,workflow_cycle_id=cycle,candidates=candidates,
            profile_version=db.current_profile_version(self.connection),scout_attempt_count=1,
            config=CuratorConfig(evidence_enabled=False,interest_fit_enabled=False))
        self.assertEqual(reviewed['recommendations'],[])
        self.connection.commit()
        self.assertEqual({t:self.connection.execute('SELECT * FROM '+t).fetchall() for t in tables},history)
        owner=self.connection.execute("SELECT paper_id FROM paper_sources WHERE source='core' AND source_id='1'").fetchone()[0]
        self.assertEqual(owner,paper_id)
        state=root/'sync';state.mkdir()
        (state/'active-cycle.json').write_text(json.dumps({'stage':'running','batch_id':'identity-fixture',
            'baseline':{'workflow_cycles':old_cycle},'started_at':(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()}))
        payload=export_delta(self.path,root,state)
        self.assertEqual([p['canonical_key'] for p in payload['normalized']['papers']],[key])
        self.assertEqual(payload['normalized']['artifacts'],[])  # No historical summary read/re-export.
        self.assertEqual(payload['normalized']['recommendations'],[])
        headers=Message();headers['X-Paper-Sync-Idempotency']='identity-fixture'
        payload.update(input_revision=snapshot['revision'],producer_id='mini-production',environment='production',run_sequence=1)
        with patch.dict(os.environ,{'PAPER_AGENT_SYNC_PRODUCER':'mini-production','PAPER_AGENT_SYNC_ENVIRONMENT':'production'}):
            receipt=receive_result_batch(cloud_db,encoded(payload),headers,cloud)
        self.assertEqual(receipt['status'],'applied')
        with sqlite3.connect(cloud_db) as con:
            self.assertEqual(con.execute('SELECT canonical_key,doi FROM papers').fetchall(),[(key,'10.999/enriched')])
            self.assertEqual(con.execute('SELECT COUNT(*) FROM recommendations').fetchone()[0],0)
        apply_routine_snapshot(self.path,build_routine_snapshot(cloud_db,cloud),root)
        self.assertEqual(self.connection.execute('SELECT id,canonical_key,doi FROM papers').fetchall(),[(paper_id,key,'10.999/enriched')])
        self.assertEqual({t:self.connection.execute('SELECT * FROM '+t).fetchall() for t in tables},history)

    def test_direct_upsert_preserves_legacy_key_and_cross_source_doi_identity(self):
        old=self.known()
        enriched=core_work_to_candidate(fixture.work(1,doi='10.999/shared')).as_dict()
        self.assertEqual(db.upsert_paper(self.connection,enriched),(old,False))
        self.assertEqual(db.upsert_paper(self.connection,{'source':'openalex','source_id':'W1','title':'Shared paper','doi':'https://doi.org/10.999/SHARED'}),(old,False))
        self.assertEqual(self.connection.execute('SELECT canonical_key FROM papers').fetchone()[0],'core:1')
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM papers').fetchone()[0],1)
        self.assertEqual({r[0] for r in self.connection.execute('SELECT paper_id FROM paper_sources')},{old})

    def test_doi_enriched_duplicate_refills_with_new_work_within_existing_budget(self):
        old=self.known()
        result,fetch=self.run_scout([fixture.page([fixture.work(1,doi='10.999/enriched')],next_offset=1),
            fixture.page([fixture.work(2)],offset=1,next_offset=2)], max_candidates=1)
        self.assertEqual(fetch.call_count,2)
        self.assertEqual(result['eligible_count'],1)
        self.assertEqual(self.connection.execute('SELECT canonical_key,doi FROM papers WHERE id=?',(old,)).fetchone(),('core:1','10.999/enriched'))
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM papers').fetchone()[0],2)

    def test_cross_source_lookup_accepts_legacy_doi_format_without_changing_key(self):
        old=self.known()
        self.connection.execute("UPDATE papers SET doi='https://doi.org/10.999/LEGACY' WHERE id=?",(old,))
        self.assertEqual(db.upsert_paper(self.connection,{'source':'openalex','source_id':'legacy','title':'Known','doi':'10.999/legacy'}),(old,False))
        self.assertEqual(self.connection.execute('SELECT canonical_key FROM papers WHERE id=?',(old,)).fetchone()[0],'core:1')

    def test_conflicting_stored_claims_never_retarget_alias_or_merge_rows(self):
        old=self.known()
        other,_=db.upsert_paper(self.connection,{'source':'openalex','source_id':'W2','title':'Different owner','doi':'10.999/conflict'})
        self.connection.commit()
        before='\n'.join(self.connection.iterdump())
        with self.assertRaises(db.PaperIdentityConflict):
            db.upsert_paper(self.connection,core_work_to_candidate(fixture.work(1,doi='10.999/conflict')).as_dict())
        with self.assertRaises(db.PaperIdentityConflict):
            db.upsert_paper_source(self.connection,other,{'source':'core','source_id':'1'})
        self.assertEqual('\n'.join(self.connection.iterdump()),before)
        result,_=self.run_scout([fixture.page([fixture.work(1,doi='10.999/conflict')],next_offset=None)])
        self.assertEqual(result['eligible_count'],0)
        ledger=json.loads(self.connection.execute('SELECT page_json FROM core_page_dispositions').fetchone()[0])
        self.assertEqual(ledger['dispositions'][0]['reason'],'identity_conflict')
        self.assertEqual(self.connection.execute("SELECT paper_id FROM paper_sources WHERE source='core'").fetchone()[0],old)

    def test_changed_doi_for_existing_alias_is_conflict_even_without_other_owner(self):
        self.known(doi='10.999/first')
        with self.assertRaises(db.PaperIdentityConflict):
            db.upsert_paper(self.connection,core_work_to_candidate(fixture.work(1,doi='10.999/second')).as_dict())
        self.assertEqual(self.connection.execute('SELECT doi FROM papers').fetchone()[0],'10.999/first')

    def test_duplicate_refresh_is_outside_http_writes_and_rolls_back_with_checkpoint(self):
        self.known()
        def response(*args, **kwargs):
            self.assertFalse(self.connection.in_transaction)
            with sqlite3.connect(self.path,timeout=0) as writer:
                writer.execute('BEGIN IMMEDIATE');writer.rollback()
            return fixture.page([fixture.work(1,doi='10.999/later')],next_offset=None)
        from paper_agents.core_progress import CoreProgress
        original=CoreProgress.checkpoint
        def fail_after_write(progress,run):
            original(progress,run)
            raise RuntimeError('checkpoint write failure')
        with patch.object(CoreProgress,'checkpoint',fail_after_write), self.assertRaisesRegex(RuntimeError,'checkpoint write failure'):
            self.run_scout(response)
        self.assertIsNone(self.connection.execute('SELECT doi FROM papers').fetchone()[0])
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM core_search_state').fetchone()[0],0)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM core_page_dispositions').fetchone()[0],0)

    def test_doi_enrichment_during_same_fetch_does_not_duplicate_source(self):
        progress=self.progress()
        results,_=self.fetch(progress,[fixture.page([fixture.work(1),fixture.work(1,doi='10.999/new')],next_offset=None)])
        self.assertEqual(len(results),1)
        self.assertEqual(results[0].doi,'10.999/new')

    def test_contradictory_claims_in_one_fetch_withhold_checkpoint(self):
        self.known()
        with self.assertRaisesRegex(RuntimeError,'contradictory identifiers'):
            self.run_scout([fixture.page([fixture.work(1,doi='10.999/a'),fixture.work(1,doi='10.999/b')],next_offset=None)])
        self.assertIsNone(self.connection.execute('SELECT doi FROM papers').fetchone()[0])
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM core_search_state').fetchone()[0],0)

    def test_cloud_alias_safety_guard_stays_strict(self):
        old=self.known()
        other,_=db.upsert_paper(self.connection,{'source':'arxiv','source_id':'other','title':'Other'})
        with self.assertRaisesRegex(ValueError,'source alias belongs to a different paper'):
            _source(self.connection,other,{'source':'core','source_id':'1'})
        self.assertEqual(self.connection.execute("SELECT paper_id FROM paper_sources WHERE source='core'").fetchone()[0],old)

    def test_existing_pending_batch_is_not_rekeyed_or_rewritten(self):
        root=Path(self.temp.name);state=root/'pending';state.mkdir()
        payload={'batch_id':'original-pending','canonical_key':'doi:10.999/original','run_sequence':33}
        pending=state/'pipeline-outbox.json';pending.write_text(json.dumps(payload,indent=2))
        original=pending.read_bytes();sent=[]
        def rejected(*args):
            sent.append(args[2]);raise urllib.error.HTTPError('fixture',422,'rejected',{},None)
        with patch.dict(os.environ,{'PAPER_AGENT_SYNC_SHARED_SECRET':'fixture-secret'*3}),self.assertRaises(urllib.error.HTTPError):
            push(self.path,root,state,'https://unused.invalid',transport=rejected)
        self.assertEqual(pending.read_bytes(),original)
        self.assertEqual(sent,[encoded(payload)])
        self.assertFalse((state/'sequence.json').exists())

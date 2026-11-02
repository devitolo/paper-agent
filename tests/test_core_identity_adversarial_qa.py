"""Identity-owner conflicts and enrichment transaction boundaries, offline only."""
import json
import sqlite3
import unittest

from paper_agents import db
from paper_agents.scout import core_work_to_candidate
from tests import test_core_progress as fixture


class CoreIdentityAdversarialQA(unittest.TestCase):
    setUp=fixture.CoreProgressTests.setUp
    tearDown=fixture.CoreProgressTests.tearDown
    progress=fixture.CoreProgressTests.progress
    fetch=fixture.CoreProgressTests.fetch
    save=fixture.CoreProgressTests.save
    run_scout=fixture.CoreProgressTests.run_scout

    def seed(self,candidate):
        result=db.upsert_paper(self.connection,candidate)
        self.connection.commit()
        return result[0]

    def test_cross_source_duplicate_gains_core_alias_without_overwriting_rich_metadata(self):
        owner=self.seed({'source':'openalex','source_id':'W-existing','doi':'10.999/shared',
            'title':'Retained title','abstract':'Retained abstract','authors':['Retained author'],
            'categories':['Retained category'],'metadata':{'rich':'retained'}})
        before=self.connection.execute('SELECT * FROM papers WHERE id=?',(owner,)).fetchone()
        result,_=self.run_scout([fixture.page([fixture.work(55,doi='https://doi.org/10.999/SHARED')],next_offset=None)])
        self.assertEqual((result['stored_count'],result['eligible_count']),(0,0))
        self.assertEqual(self.connection.execute('SELECT * FROM papers WHERE id=?',(owner,)).fetchone(),before)
        self.assertEqual(self.connection.execute("SELECT paper_id FROM paper_sources WHERE source='core' AND source_id='55'").fetchone()[0],owner)
        self.assertEqual(json.loads(self.connection.execute("SELECT metadata_json FROM paper_sources WHERE source='openalex'").fetchone()[0]),{'rich':'retained'})

    def test_doi_and_arxiv_claims_owned_by_different_papers_reject_without_writes(self):
        self.seed({'source':'openalex','source_id':'doi-owner','title':'A','doi':'10.999/a'})
        self.seed({'source':'arxiv','source_id':'2601.00001','title':'B'})
        before='\n'.join(self.connection.iterdump())
        with self.assertRaises(db.PaperIdentityConflict):
            db.upsert_paper(self.connection,{'source':'core','source_id':'incoming','title':'Conflict',
                'doi':'10.999/a','arxiv_id':'2601.00001'})
        self.assertEqual('\n'.join(self.connection.iterdump()),before)

    def test_identity_claimed_after_fetch_causes_checkpoint_conflict_without_advancement(self):
        owner=self.seed(core_work_to_candidate(fixture.work(1)).as_dict())
        progress=self.progress()
        self.fetch(progress,[fixture.page([fixture.work(1,doi='10.999/claimed-later')],next_offset=None)])
        other=self.seed({'source':'openalex','source_id':'new-owner','title':'Concurrent owner','doi':'10.999/claimed-later'})
        with self.assertRaises(db.PaperIdentityConflict):self.save(progress)
        self.assertIsNone(self.connection.execute('SELECT doi FROM papers WHERE id=?',(owner,)).fetchone()[0])
        self.assertEqual(self.connection.execute("SELECT paper_id FROM paper_sources WHERE source='core' AND source_id='1'").fetchone()[0],owner)
        self.assertEqual(self.connection.execute('SELECT doi FROM papers WHERE id=?',(other,)).fetchone()[0],'10.999/claimed-later')
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM core_search_state').fetchone()[0],0)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM core_page_dispositions').fetchone()[0],0)

    def test_same_fetch_contradictory_arxiv_claims_withhold_checkpoint(self):
        with self.assertRaisesRegex(RuntimeError,'contradictory identifiers'):
            self.run_scout([fixture.page([fixture.work(1,arxiv_id='2601.00001'),
                fixture.work(1,arxiv_id='2601.00002')],next_offset=None)])
        for table in ('papers','paper_sources','core_search_state','core_page_dispositions'):
            with self.subTest(table=table):
                self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)

    def test_same_fetch_doi_enrichment_stores_one_candidate_and_one_source_owner(self):
        result,_=self.run_scout([fixture.page([fixture.work(1),fixture.work(1,doi='10.999/same-fetch')],next_offset=None)])
        self.assertEqual((result['stored_count'],result['eligible_count']),(1,1))
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM papers').fetchone()[0],1)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM paper_sources').fetchone()[0],1)
        self.assertEqual(self.connection.execute('SELECT doi FROM papers').fetchone()[0],'10.999/same-fetch')
        self.assertEqual(self.connection.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_read_only_resolution_does_not_mutate_rows_or_acquire_writer_lock(self):
        owner=self.seed(core_work_to_candidate(fixture.work(1)).as_dict())
        before='\n'.join(self.connection.iterdump())
        resolved=db.resolve_existing_paper(self.connection,core_work_to_candidate(fixture.work(1,doi='10.999/add')).as_dict())
        self.assertEqual(resolved['id'],owner)
        self.assertFalse(self.connection.in_transaction)
        other=sqlite3.connect(self.path,timeout=0)
        try:other.execute('BEGIN IMMEDIATE');other.rollback()
        finally:other.close()
        self.assertEqual('\n'.join(self.connection.iterdump()),before)


if __name__=='__main__':unittest.main()

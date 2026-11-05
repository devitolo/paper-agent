import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

from paper_agents import core_identity_repair as repair
from paper_agents.db import init_db
from paper_agents.sync_exchange import build_routine_snapshot,apply_routine_snapshot
from paper_agents.whole_cycle_exchange import _file_hash


class CoreIdentityRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'data').mkdir();(self.root/'config').mkdir()
        (self.root/'config/topics.yaml').write_text('topics: []\n');(self.root/'config/industry.json').write_text('{}');(self.root/'data/profile.json').write_text('{}')
        self.db=self.root/'data/paper_agent.db';init_db(self.db)
        self.c=sqlite3.connect(self.db);self.addCleanup(self.c.close);self.c.execute('PRAGMA foreign_keys=ON')
        self.keep=self.c.execute("INSERT INTO papers(canonical_key,title) VALUES('core:313009506','Same Paper')").lastrowid
        self.duplicate=self.c.execute("INSERT INTO papers(canonical_key,title,doi) VALUES('doi:10.5281/test','Same Paper','10.5281/test')").lastrowid
        self.c.execute("INSERT INTO paper_sources(paper_id,source,source_id) VALUES(?,'core','313009506')",(self.duplicate,))
        self.c.execute("INSERT INTO raw_feedback(paper_id,content,content_hash) VALUES(?,'retain feedback','feedback')",(self.duplicate,))
        self.c.execute("INSERT INTO openalex_search_state VALUES('retained','cursor')")
        for pid in (self.keep,self.duplicate):
            cycle=self.c.execute("INSERT INTO workflow_cycles(state) VALUES('completed')").lastrowid
            run=self.c.execute('INSERT INTO curator_runs(workflow_cycle_id) VALUES(?)',(cycle,)).lastrowid
            self.c.execute("INSERT INTO curator_evaluations(curator_run_id,paper_id,score,rationale) VALUES(?,?,80,'retain evaluation')",(run,pid))
            self.c.execute("INSERT INTO recommendations(curator_run_id,paper_id,recommendation_order,rationale) VALUES(?,?,1,'retain recommendation')",(run,pid))
            self.c.execute("INSERT INTO artifacts(paper_id,artifact_type,path) VALUES(?,'triage_summary',?)",(pid,str(pid)+'.json'))
        self.c.commit()
        self.payload={'batch_id':repair.BATCH,'run_sequence':33,'normalized':{'papers':[{'canonical_key':'doi:10.5281/test','doi':'10.5281/test','title':'Same Paper'}],'paper_sources':[{'canonical_key':'doi:10.5281/test','source':'core','source_id':'313009506'}]}}

    def test_merge_preserves_all_history_and_database_inode(self):
        inode=self.db.stat().st_ino
        result=repair.merge_identity(self.c,self.payload,self.keep,self.duplicate)
        self.assertEqual(self.db.stat().st_ino,inode)
        self.assertEqual(result['reference_rows_moved']['recommendations.paper_id'],1)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM recommendations').fetchone()[0],2)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM curator_evaluations').fetchone()[0],2)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0],2)
        self.assertEqual(tuple(self.c.execute('SELECT paper_id,content FROM raw_feedback').fetchone()),(self.keep,'retain feedback'))
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM papers').fetchone()[0],1)
        self.assertEqual(self.c.execute('SELECT paper_id FROM paper_sources').fetchone()[0],self.keep)
        self.assertEqual(self.c.execute('PRAGMA foreign_key_check').fetchall(),[])
        self.assertEqual(self.c.execute('SELECT value_json FROM openalex_search_state').fetchone()[0],'cursor')

    def test_same_path_artifacts_keep_both_ids_and_original_files(self):
        original=self.root/'summary.json';original.write_text('{"summary":"preserved"}')
        held=self.root/'held-summary.json';held.write_bytes(original.read_bytes())
        self.c.execute('UPDATE artifacts SET path=?',(str(original),));self.c.commit()
        ids=[r[0] for r in self.c.execute('SELECT id FROM artifacts ORDER BY id')]
        result=repair.merge_identity(self.c,self.payload,self.keep,self.duplicate,
                                     artifact_paths={str(original):str(held)})
        self.assertEqual([r[0] for r in self.c.execute('SELECT id FROM artifacts ORDER BY id')],ids)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM artifacts WHERE paper_id=?',(self.keep,)).fetchone()[0],2)
        self.assertEqual(len(result['artifact_path_changes']),1)
        self.assertEqual(original.read_bytes(),held.read_bytes())

    def test_conflicting_unique_history_rolls_back_without_deleting_rows(self):
        run=self.c.execute('SELECT id FROM curator_runs ORDER BY id LIMIT 1').fetchone()[0]
        self.c.execute("INSERT INTO curator_evaluations(curator_run_id,paper_id,score,rationale) VALUES(?,?,90,'collision')",(run,self.duplicate));self.c.commit()
        with self.assertRaises(sqlite3.IntegrityError):repair.merge_identity(self.c,self.payload,self.keep,self.duplicate)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM papers').fetchone()[0],2)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM curator_evaluations').fetchone()[0],3)
        self.assertEqual(self.c.execute('SELECT paper_id FROM paper_sources').fetchone()[0],self.duplicate)

    def test_distinct_title_or_identifier_is_not_merged(self):
        for field,value in (('title','Different paper'),('doi','10.9999/different'),('arxiv_id','1234.5678')):
            with self.subTest(field=field):
                self.c.execute('UPDATE papers SET '+field+'=? WHERE id=?',(value,self.keep));self.c.commit()
                with self.assertRaises(ValueError):repair.merge_identity(self.c,self.payload,self.keep,self.duplicate)
                self.c.execute('UPDATE papers SET title=?,doi=NULL,arxiv_id=NULL WHERE id=?',('Same Paper',self.keep));self.c.commit()
                self.assertEqual(self.c.execute('SELECT COUNT(*) FROM papers').fetchone()[0],2)

    def test_existing_routine_merge_succeeds_and_retains_local_history(self):
        repair.merge_identity(self.c,self.payload,self.keep,self.duplicate)
        snapshot=build_routine_snapshot(self.db,self.root)
        inode=self.db.stat().st_ino
        apply_routine_snapshot(self.db,snapshot,self.root)
        self.assertEqual(self.db.stat().st_ino,inode)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM recommendations').fetchone()[0],2)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0],2)

    def test_explicit_repair_dry_run_and_apply_preserve_capsule(self):
        state=self.root/'state';state.mkdir()
        raw=json.dumps(self.payload).encode()
        import hashlib
        digest=hashlib.sha256(raw).hexdigest();capsule=state/'held'/digest;capsule.mkdir(parents=True)
        (capsule/'pipeline-outbox.json').write_bytes(raw)
        manifest={'batch_id':repair.BATCH,'run_sequence':33,'files':{'pipeline-outbox.json':digest}}
        (capsule/'manifest.json').write_text(json.dumps(manifest))
        (state/'identity-restore-required.json').write_text(json.dumps({'capsule':digest}))
        (state/'sequence.json').write_text('{"sequence":32}');(state/'reserved-sequence.json').write_text('{"sequence":33}')
        before=_file_hash(self.db);inode=self.db.stat().st_ino
        with patch.object(repair,'CAPSULE',digest):
            plan=repair.repair(self.db,state,keep_id=self.keep,duplicate_id=self.duplicate)
            self.assertEqual(plan['mode'],'dry-run');self.assertEqual(_file_hash(self.db),before)
            result=repair.repair(self.db,state,apply=True,keep_id=self.keep,duplicate_id=self.duplicate)
        self.assertEqual(self.db.stat().st_ino,inode)
        self.assertEqual((capsule/'pipeline-outbox.json').read_bytes(),raw)
        self.assertTrue((state/result['backup_file']).is_file())
        with closing(sqlite3.connect(state/result['backup_file'])) as old:
            self.assertEqual(old.execute('SELECT COUNT(*) FROM papers').fetchone()[0],2)
        self.assertEqual(json.loads((state/'sequence.json').read_text())['sequence'],32)
        self.assertTrue((state/'identity-restore-required.json').exists())

    def test_recovery_path_has_no_full_backup_or_replacement(self):
        import inspect
        from paper_agents.whole_cycle_exchange import pull_after_identity_hold
        source=inspect.getsource(pull_after_identity_hold)
        self.assertIn('download_routine_snapshot',source);self.assertIn('pull_routine(',source)
        self.assertNotIn('download_backup(',source);self.assertNotIn('/sync/backup',source)

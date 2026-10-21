import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from paper_agents import nonprod_snapshot as snapshot

ROOT = Path(__file__).resolve().parents[2]
FLAGS = {'PAPER_AGENT_STARTUP_MODE': 'serving-snapshot', 'PAPER_AGENT_PACKAGED': '1',
         'PAPER_AGENT_SERVING_ONLY': '1', 'PAPER_NON_PRODUCTION': '1',
         'PAPER_AGENT_SCHEDULER_ENABLED': '0', 'PAPER_AGENT_GEMINI_ENABLED': '0',
         'PAPER_AGENT_TELEMETRY': '0', 'PAPER_AGENT_MINILM_ENABLED': '0',
         'PAPER_AGENT_SYNC_SHARED_SECRET': ''}
IMAGE = 'ghcr.io/devitolo/paper-agent@sha256:' + 'a'*64


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source, self.dest = self.root/'source', self.root/'snapshot'
        (self.source/'data/extractions').mkdir(parents=True)
        (self.source/'config').mkdir()
        db = sqlite3.connect(self.source/'data/paper_agent.db')
        db.executescript((ROOT/'sql/schema.sql').read_text())
        db.executescript("""
        INSERT INTO papers(id,canonical_key,title) VALUES(7,'real:example','Example paper');
        INSERT INTO paper_sources(paper_id,source,source_id) VALUES(7,'arxiv','example');
        INSERT INTO workflow_cycles(id) VALUES(11);
        INSERT INTO curator_runs(id,workflow_cycle_id) VALUES(13,11);
        INSERT INTO curator_evaluations(curator_run_id,paper_id,score,rationale) VALUES(13,7,80,'Relevant');
        INSERT INTO recommendations(id,curator_run_id,paper_id,recommendation_order,rationale) VALUES(17,13,7,1,'Relevant');
        INSERT INTO artifacts(paper_id,artifact_type,path) VALUES(7,'triage_summary','data/extractions/example.json');
        INSERT INTO raw_feedback(paper_id,recommendation_id,content,content_hash) VALUES(7,17,'Keep','unique');
        INSERT INTO feedback(paper_id,status,notes) VALUES(7,'keep','Existing feedback');
        """)
        db.close()
        (self.source/'data/extractions/example.json').write_text('{"summary":"Useful result"}')
        (self.source/'data/profile.json').write_text(json.dumps(dict(interests=['AI'],positive_signals=[],negative_signals=[],feedback_history=[])))
        (self.source/'config/topics.yaml').write_text('topics: []\n')
        (self.source/'config/industry.json').write_text('{"include_signals":[],"exclude_signals":[],"max_items_per_run":1}')
        (self.source/'config/provider.secret').write_text('DO NOT COPY')
        (self.source/'data/old-backup.db').write_text('DO NOT COPY')

    def capture(self):
        # Real operator mounts are read-only; fixtures are deliberately writable.
        with patch.object(snapshot, 'readonly'):
            return snapshot.capture(self.source, self.dest, 'b'*40, IMAGE)

    def test_complete_relations_and_artifacts_without_secrets_or_source_writes(self):
        before = {str(p):p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        result = self.capture()
        self.assertEqual(result['counts']['recommendations'], 1)
        self.assertEqual(result['counts']['raw_feedback'], 1)
        self.assertEqual(snapshot.verify(self.dest), result)
        self.assertFalse((self.dest/'config/provider.secret').exists())
        self.assertFalse((self.dest/'data/old-backup.db').exists())
        self.assertEqual(before, {str(p):p.read_bytes() for p in self.source.rglob('*') if p.is_file()})
        with sqlite3.connect(self.dest/'data/paper_agent.db') as db:
            self.assertEqual(db.execute('SELECT paper_id,recommendation_id FROM raw_feedback').fetchone(), (7,17))

    def test_wal_source_becomes_self_contained_snapshot(self):
        db = sqlite3.connect(self.source/'data/paper_agent.db')
        try:
            self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone(), ('wal',))
            db.execute("UPDATE papers SET title='Committed WAL paper'")
            db.commit()
            self.capture()
            snapshot.verify(self.dest)
            self.assertFalse((self.dest/'data/paper_agent.db-wal').exists())
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone(), ('wal',))
        finally:
            db.close()

    def test_writable_source_and_alias_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'read-only'):
            snapshot.capture(self.source,self.dest,'b'*40,IMAGE)
        with self.assertRaisesRegex(RuntimeError, 'separate'):
            snapshot.capture(self.source,self.source/'copy','b'*40,IMAGE)

    def test_runtime_allows_local_feedback_but_staging_checksum_detects_change(self):
        self.capture()
        with sqlite3.connect(self.dest/'data/paper_agent.db') as db:
            db.execute("UPDATE feedback SET notes='Nonprod edit'")
        with patch.dict(os.environ, FLAGS, clear=True):
            self.assertEqual(snapshot.validate_runtime(self.dest)['papers'], 1)
        with self.assertRaisesRegex(RuntimeError, 'checksum'):
            snapshot.verify(self.dest)

    def test_disabled_external_actions_and_explicit_mode_required(self):
        self.capture()
        for flag in ('PAPER_AGENT_GEMINI_ENABLED','PAPER_AGENT_SCHEDULER_ENABLED','PAPER_AGENT_MINILM_ENABLED'):
            with self.subTest(flag=flag), patch.dict(os.environ, {**FLAGS,flag:'1'},clear=True):
                with self.assertRaises(ValueError):
                    snapshot.validate_runtime(self.dest)
        with patch.dict(os.environ, FLAGS, clear=True):
            (self.dest/'data/MINI_NONPROD_SYNTHETIC').write_text('synthetic-v1')
            with self.assertRaisesRegex(RuntimeError, 'Synthetic marker'):
                snapshot.validate_runtime(self.dest)

    def test_missing_and_symlink_artifacts_refused(self):
        artifact = self.source/'data/extractions/example.json'
        artifact.unlink()
        with self.assertRaises(FileNotFoundError):
            self.capture()
        artifact.symlink_to(self.source/'config/provider.secret')
        with self.assertRaisesRegex(RuntimeError, 'Aliased'):
            snapshot.safe_path(self.source, 'data/extractions/example.json')

    def test_unknown_schema_refused(self):
        with sqlite3.connect(self.source/'data/paper_agent.db') as db:
            db.execute('CREATE TABLE unknown(value TEXT)')
        with self.assertRaisesRegex(RuntimeError, 'schema unsupported'):
            self.capture()

    def test_foreign_key_damage_and_outside_artifact_refused(self):
        with sqlite3.connect(self.source/'data/paper_agent.db') as db:
            db.execute('UPDATE raw_feedback SET recommendation_id=999')
        with self.assertRaisesRegex(RuntimeError, 'foreign keys'):
            self.capture()
        for path in ('../config/provider.json','data/../config/provider.json',
                     '/etc/passwd','data/credentials/key.json','data/unknown/key.json'):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                snapshot.artifact_path(path)

    def test_copy_limit_and_extra_state_refused(self):
        with patch.object(snapshot,'MAX_BYTES',1):
            with self.assertRaisesRegex(RuntimeError,'bound'):
                self.capture()
        self.dest = self.root/'another'
        self.capture()
        (self.dest/'config/unapproved.env').write_text('secret')
        with self.assertRaisesRegex(RuntimeError,'Unmanifested'):
            snapshot.verify(self.dest)

    def test_render_queue_contains_real_card_and_snapshot_provenance(self):
        from paper_agents import web
        self.capture()
        old = Path.cwd()
        try:
            os.chdir(self.dest)
            with patch.dict(os.environ, FLAGS, clear=True):
                html = web.render_review_queue(self.dest/'data/paper_agent.db')
            self.assertIn('Example paper', html)
            self.assertIn('NON-PRODUCTION', html)
            self.assertIn('Mini production snapshot', html)
            self.assertIn('local edits only; no sync', html)
        finally:
            os.chdir(old)

    def test_install_separate_roles_and_refuse_overwrite(self):
        self.capture()
        for role in ('ui','pipeline'):
            target=self.root/role
            (target/'data').mkdir(parents=True)
            (target/'config').mkdir()
            with patch.object(os,'chown') as ownership:
                result=snapshot.install(self.dest,target,role)
                transferred = [call.args[0] for call in ownership.call_args_list]
                for path in transferred:
                    if path.parent in transferred:
                        self.assertLess(transferred.index(path), transferred.index(path.parent))
            self.assertEqual(result['destination_role'],role)
            self.assertEqual(snapshot.verify(target)['counts']['papers'],1)
            with self.assertRaisesRegex(RuntimeError,'empty'):
                snapshot.install(self.dest,target,role)
        with sqlite3.connect(self.root/'ui/data/paper_agent.db') as db:
            db.execute("UPDATE feedback SET notes='UI only'")
        self.assertEqual(snapshot.verify(self.root/'pipeline')['counts']['feedback'],1)

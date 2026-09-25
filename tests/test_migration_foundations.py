import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from paper_agents import db, import_state, package_runtime, prepare_model, runtime_config

ROOT = Path(__file__).resolve().parents[1]
MODEL = 'qwen2.5:1.5b-instruct'
DIGEST = '65ec06548149b04c096a120e4a6da9d4017ea809c91734ea5631e89f96ddc57b'


@contextmanager
def fixture_connection(path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def settings(manifest):
    return {'PAPER_AGENT_PACKAGED':'1','PAPER_AGENT_STARTUP_MODE':'imported',
        'PAPER_AGENT_IMPORT_MANIFEST':str(manifest), 'PAPER_AGENT_DEFAULT_SOURCES':'arxiv,semantic_scholar,openalex',
        'PAPER_AGENT_GEMINI_ENABLED':'0','PAPER_AGENT_TELEMETRY':'0', 'PAPER_MODEL_MODE':'verify',
        'PAPER_AGENT_MODEL':MODEL,'PAPER_AGENT_EXPECTED_MODEL_DIGEST':DIGEST}


def files(root):
    return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}


class ImportTests(unittest.TestCase):
    def test_documented_data_config_snapshot_uses_trusted_candidate_schema(self):
        snapshot = self.root/'documented-snapshot'
        snapshot.mkdir()
        for name in ('data', 'config'):
            shutil.copytree(self.source/name, snapshot/name)
        before = files(snapshot)
        approved = import_state.manifest(snapshot)
        self.assertEqual(approved, self.approved)
        self.assertEqual(files(snapshot), before)
        self.assertFalse((snapshot/'sql').exists())
        path = self.root/'documented-manifest.json'
        path.write_text(json.dumps(approved))
        import_state.initialize(snapshot, path)
        self.assertTrue((snapshot/import_state.RECEIPT).is_file())
        # A copied schema must not be able to redefine the import contract.
        (snapshot/'sql').mkdir()
        (snapshot/'sql/schema.sql').write_text('invalid untrusted snapshot SQL')
        self.assertEqual(import_state.manifest(snapshot), approved)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'source'; self.source.mkdir()
        shutil.copytree(ROOT/'sql',self.source/'sql')
        (self.source/'data/papers').mkdir(parents=True)
        (self.source/'config').mkdir()
        shutil.copy(ROOT/'deploy/templates/profile.json',self.source/'data/profile.json')
        (self.source/'config/topics.yaml').write_text('topics:\n  - id: synthetic\n    label: Synthetic\n    query: synthetic operations\n    sources: [arxiv, openalex, semantic_scholar]\n    enabled: true\n')
        (self.source/'data/papers/test.pdf').write_bytes(b'%PDF synthetic fixture')
        db.init_db(self.source/'data/paper_agent.db',self.source/'sql/schema.sql')
        with fixture_connection(self.source/'data/paper_agent.db') as c:
            c.executescript('''
INSERT INTO papers(id,canonical_key,title) VALUES(7,'synthetic:7','Synthetic paper');
INSERT INTO workflow_cycles(id,state) VALUES(2,'complete');
INSERT INTO scouting_guidance(id,guidance_text,active) VALUES(3,'synthetic old',0),(4,'synthetic active',1);
INSERT INTO scout_runs(id,workflow_cycle_id,source,target_candidates,max_candidates,freshness_months,guidance_id,topics_json) VALUES(5,2,'arxiv',2,20,6,4,'["synthetic"]');
INSERT INTO scout_candidates(id,scout_run_id,paper_id,retrieval_order,excluded,exclusion_reason) VALUES(6,5,7,1,1,'synthetic exclusion');
INSERT INTO profile_versions(id,version,profile_json,active) VALUES(18,18,'{"interests":["old"]}',0),(19,19,'{"interests":["active"]}',1);
INSERT INTO curator_runs(id,workflow_cycle_id,profile_version_id) VALUES(8,2,19);
INSERT INTO curator_evaluations(id,curator_run_id,paper_id,scout_candidate_id,score,rationale) VALUES(9,8,7,6,31.5,'synthetic evaluation');
INSERT INTO recommendations(id,curator_run_id,paper_id,recommendation_order,rationale,status) VALUES(10,8,7,1,'synthetic recommendation','reviewed');
INSERT INTO artifacts(id,paper_id,artifact_type,path) VALUES(11,7,'pdf','data/papers/test.pdf');
INSERT INTO raw_feedback(id,paper_id,recommendation_id,content,content_hash) VALUES(12,7,10,'synthetic applied','applied'),(13,7,10,'synthetic pending','pending');
INSERT INTO feedback_parse_attempts(id,raw_feedback_id,parser_name,parser_version,status) VALUES(14,12,'synthetic','1','succeeded'),(15,13,'synthetic','1','succeeded');
INSERT INTO structured_feedback(id,parse_attempt_id,paper_id,decision,score) VALUES(16,14,7,'keep',4.5),(17,15,7,'keep',3.5);
INSERT INTO feedback_profile_applications(id,structured_feedback_id,profile_version_id) VALUES(20,16,19);
INSERT INTO feedback_profile_apply_attempts(id,provider,dry_run,status,profile_version_id) VALUES(21,'synthetic',0,'succeeded',19),(22,'synthetic',1,'failed',NULL);
''')
        self.approved = import_state.manifest(self.source)
        self.manifest = self.root/'manifest.json';self.manifest.write_text(json.dumps(self.approved))
        self.originals = files(self.source)
        self.copy = self.root/'copy';shutil.copytree(self.source,self.copy)

    def start(self):
        with patch.dict(os.environ,settings(self.manifest),clear=True):
            package_runtime.initialize(self.copy,ROOT/'deploy/templates')

    def test_two_startups_recreation_and_exact_state_preservation(self):
        self.start();self.start()
        self.assertEqual(import_state.inspect(self.copy),{k:self.approved[k] for k in ('files','schema_sha256','tables')})
        recreated=self.root/'recreated';shutil.copytree(self.copy,recreated)
        import_state.initialize(recreated,self.manifest)
        self.assertEqual(import_state.inspect(recreated),import_state.inspect(self.copy))
        self.assertEqual(files(self.source),self.originals)
        newfiles=set(files(self.copy))-set(self.originals)
        self.assertEqual(newfiles,{import_state.RECEIPT,import_state.LOCK})
        # Same native db initialization remains a no-delta operation on this exact schema.
        db.init_db(recreated/'data/paper_agent.db',recreated/'sql/schema.sql')
        self.assertEqual(import_state.inspect(recreated),import_state.inspect(self.copy))

    def test_every_required_missing_file_fails_before_any_mutation(self):
        for name in import_state.REQUIRED:
            with self.subTest(name=name):
                path=self.copy/name;original=path.read_bytes();path.unlink()
                before=files(self.copy)
                with self.assertRaises(RuntimeError):self.start()
                self.assertEqual(files(self.copy),before)
                path.write_bytes(original)

    def test_corrupt_required_files_fail_without_mutation(self):
        for name in import_state.REQUIRED:
            with self.subTest(name=name):
                path=self.copy/name;original=path.read_bytes();path.write_bytes(b'corrupt')
                before=files(self.copy)
                with self.assertRaises((RuntimeError,ValueError,sqlite3.Error)):self.start()
                self.assertEqual(files(self.copy),before)
                path.write_bytes(original)

    def test_changed_state_and_manifest_missing_coverage_rejected(self):
        (self.copy/'data/papers/test.pdf').write_bytes(b'changed')
        before=files(self.copy)
        with self.assertRaisesRegex(RuntimeError,'differs from manifest'):self.start()
        self.assertEqual(files(self.copy),before)
        incomplete=copy.deepcopy(self.approved);incomplete['files'].pop('data/profile.json')
        self.manifest.write_text(json.dumps(incomplete))
        with self.assertRaisesRegex(RuntimeError,'Incomplete import manifest'):self.start()

    def test_schema_change_and_broken_relations_rejected(self):
        path=self.copy/'data/paper_agent.db';original=path.read_bytes()
        with fixture_connection(path) as c:c.execute('ALTER TABLE papers ADD COLUMN unknown TEXT')
        before=files(self.copy)
        with self.assertRaisesRegex(RuntimeError,'schema incompatible'):self.start()
        self.assertEqual(files(self.copy),before)
        path.write_bytes(original)
        with fixture_connection(path) as c:c.execute('UPDATE feedback_profile_applications SET profile_version_id=999')
        before=files(self.copy)
        with self.assertRaisesRegex(RuntimeError,'foreign keys'):self.start()
        self.assertEqual(files(self.copy),before)

    def test_missing_external_symlink_and_sidecar_rejected(self):
        path=self.copy/'data/papers/test.pdf';data=path.read_bytes();path.unlink()
        with self.assertRaisesRegex(RuntimeError,'artifact missing'):self.start()
        path.symlink_to(self.source/'data/papers/test.pdf')
        with self.assertRaisesRegex(RuntimeError,'symlinks'):self.start()
        path.unlink();path.write_bytes(data)
        sidecar=self.copy/'data/paper_agent.db-wal';sidecar.write_bytes(b'')
        with self.assertRaisesRegex(RuntimeError,'quiesced'):self.start()
        sidecar.unlink()
        with fixture_connection(self.copy/'data/paper_agent.db') as c:c.execute("UPDATE artifacts SET path='/native/private/file.pdf'")
        with self.assertRaisesRegex(RuntimeError,'mapping'):self.start()

    def test_initializer_exclusion_and_atomic_receipt_recovery(self):
        with import_state.initialization_lock(self.copy):
            process=subprocess.run([sys.executable,'-c',
                'from paper_agents.import_state import initialize; import sys; initialize(sys.argv[1],sys.argv[2])',str(self.copy),str(self.manifest)],
                cwd=ROOT,capture_output=True,text=True,timeout=10)
            self.assertNotEqual(process.returncode,0)
            self.assertIn('already active',process.stderr)
        self.assertFalse((self.copy/import_state.RECEIPT).exists())
        with patch.object(import_state.os,'link',side_effect=OSError('synthetic publication interruption')):
            with self.assertRaises(OSError):self.start()
        self.assertFalse((self.copy/import_state.RECEIPT).exists())
        self.start()
        self.assertEqual(files(self.source),self.originals)

    def test_legitimate_post_import_writes_survive_restart(self):
        self.start()
        with fixture_connection(self.copy/'data/paper_agent.db') as c:
            c.execute("INSERT INTO raw_feedback(id,paper_id,content,content_hash) VALUES(23,7,'new synthetic feedback','new')")
            c.execute("UPDATE profile_versions SET active=0")
            c.execute("INSERT INTO profile_versions(id,version,profile_json,active) VALUES(24,20,'{}',1)")
            c.execute("INSERT INTO artifacts(id,paper_id,artifact_type,path) VALUES(25,7,'synthetic','data/papers/new.txt')")
        (self.copy/'data/papers/new.txt').write_text('new synthetic artifact')
        before=import_state.inspect(self.copy)
        self.start()
        self.assertEqual(import_state.inspect(self.copy),before)
        self.assertEqual(files(self.source),self.originals)


class ConfigurationAndModelTests(unittest.TestCase):
    def test_fresh_defaults_and_explicit_parity(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(runtime_config.default_sources(),['arxiv','semantic_scholar','openalex'])
            self.assertTrue(runtime_config.gemini_enabled())
        with patch.dict(os.environ,{'PAPER_AGENT_PACKAGED':'1'},clear=True):
            self.assertEqual(runtime_config.default_sources(),['arxiv'])
            self.assertFalse(runtime_config.gemini_enabled())
        with patch.dict(os.environ,settings('/manifest.json'),clear=True):
            self.assertEqual(runtime_config.default_sources(),['arxiv','semantic_scholar','openalex'])
            self.assertFalse(runtime_config.gemini_enabled())
        for key,value in [('PAPER_AGENT_DEFAULT_SOURCES','arxiv,arxiv'),('PAPER_AGENT_DEFAULT_SOURCES','arxiv,unknown'),('PAPER_AGENT_GEMINI_ENABLED',''),('PAPER_AGENT_TELEMETRY','yes'),('PAPER_AGENT_STARTUP_MODE','typo'),('PAPER_MODEL_MODE','prepare'),('PAPER_AGENT_EXPECTED_MODEL_DIGEST',DIGEST[:12])]:
            with self.subTest(key=key,value=value),patch.dict(os.environ,{**settings('/manifest.json'),key:value},clear=True):
                with self.assertRaises(ValueError):runtime_config.migration_settings()

    def test_missing_gemini_does_not_block_imported_browsing(self):
        with patch.dict(os.environ, settings('/manifest.json'), clear=True):
            os.environ['PAPER_AGENT_GEMINI_ENABLED'] = '1'
            with patch('paper_agents.import_state.initialize') as initialize:
                package_runtime.initialize(Path('/unused'))
                initialize.assert_called_once()


    def test_verify_is_one_read_only_request_no_pull_or_inference(self):
        payload={'models':[{'name':MODEL,'model':MODEL,'digest':DIGEST}]}
        with patch.object(prepare_model,'request_json',return_value=payload) as request,patch.object(prepare_model,'pull_model') as pull:
            self.assertEqual(prepare_model.verify_model(MODEL,DIGEST)['inference'],'unchecked')
            self.assertEqual(request.call_count,1);self.assertEqual(request.call_args.args,('/api/tags',));pull.assert_not_called()
        for payload in [{'models':[]},{'models':[{'name':MODEL,'digest':DIGEST[:12]}]}, {'models':[{'name':MODEL,'digest':'a'*64}]}, {'models':[{'name':MODEL,'digest':DIGEST}]*2}, {'models':[{'name':MODEL,'model':'other','digest':DIGEST}]},None]:
            with self.subTest(payload=payload),patch.object(prepare_model,'request_json',return_value=payload),patch.object(prepare_model,'pull_model') as pull:
                with self.assertRaises(prepare_model.ModelPreparationError):prepare_model.verify_model(MODEL,DIGEST)
                pull.assert_not_called()
        with patch.object(prepare_model,'request_json') as request:
            with self.assertRaises(prepare_model.ModelPreparationError):prepare_model.verify_model(MODEL,DIGEST[:12])
            request.assert_not_called()

    def test_verify_cli_does_not_fall_back_to_prepare(self):
        with patch.dict(os.environ,{**settings('/unused'),'PAPER_MODEL_MODE':'verify'},clear=True),patch.object(prepare_model,'verify_model',side_effect=prepare_model.ModelPreparationError('mismatch')),patch.object(prepare_model,'prepare_model') as prepare:
            self.assertEqual(prepare_model.main(),1);prepare.assert_not_called()
        with patch.dict(os.environ,{**settings('/unused'),'PAPER_MODEL_MODE':'prepare'},clear=True),patch.object(prepare_model,'prepare_model') as prepare:
            self.assertEqual(prepare_model.main(),1);prepare.assert_not_called()

    def test_mismatched_model_prevents_readiness_inference(self):
        with patch.dict(os.environ,settings('/unused'),clear=True),patch.object(package_runtime,'request_json',return_value={'models':[{'name':MODEL,'digest':'b'*64}]}) as request:
            with self.assertRaises(RuntimeError):package_runtime.check_model()
            self.assertEqual(request.call_count,1)
            self.assertEqual(request.call_args.args,('/api/tags',))

    def test_missing_telemetry_dependencies_reject_before_import(self):
        from importlib.metadata import PackageNotFoundError
        with patch.dict(os.environ,{**settings('/unused'),'PAPER_AGENT_TELEMETRY':'1'},clear=True),patch('importlib.metadata.version',side_effect=PackageNotFoundError('synthetic missing dependency')),patch.object(import_state,'initialize') as initialize:
            with self.assertRaises(PackageNotFoundError):package_runtime.initialize(Path('/unused'))
            initialize.assert_not_called()

    def test_verify_response_after_deadline_rejected(self):
        with patch.object(prepare_model,'request_json',return_value={'models':[{'name':MODEL,'digest':DIGEST}]}),patch.object(prepare_model.time,'monotonic',side_effect=[0,0,0,31]):
            with self.assertRaisesRegex(prepare_model.ModelPreparationError,'timed out'):
                prepare_model.verify_model(MODEL,DIGEST,budget=30)


class ReceiptRecoveryTests(unittest.TestCase):
    setUp = ImportTests.setUp
    start = ImportTests.start
    def test_partial_write_and_fsync_failures_are_retryable(self):
        for failure in ('write', 'stage_fsync', 'publication_fsync'):
            with self.subTest(failure=failure):
                self.copy=self.root/failure;shutil.copytree(self.source,self.copy)
                original_open=Path.open
                class InterruptedWrite:
                    def __init__(self,stream):self.stream=stream
                    def __enter__(self):return self
                    def __exit__(self,*args):self.stream.close()
                    def write(self,value):
                        self.stream.write(value[:9]);self.stream.flush()
                        raise OSError('synthetic partial write')
                def opening(path,*args,**kwargs):
                    stream=original_open(path,*args,**kwargs)
                    return InterruptedWrite(stream) if args and args[0]=='xb' else stream
                original_sync=os.fsync
                calls=0
                def syncing(fd):
                    nonlocal calls
                    calls+=1
                    if calls == (3 if failure=='stage_fsync' else 4):
                        raise OSError('synthetic fsync failure')
                    return original_sync(fd)
                target=patch.object(Path,'open',opening) if failure=='write' else patch.object(import_state.os,'fsync',syncing)
                with target:
                    with self.assertRaises(OSError):self.start()
                self.assertTrue((self.copy/import_state.staging_name(self.approved)).exists())
                self.start();self.start()
                self.assertFalse((self.copy/import_state.staging_name(self.approved)).exists())
                self.assertEqual(import_state.inspect(self.copy),{k:self.approved[k] for k in ('files','schema_sha256','tables')})
                self.assertEqual(files(self.source),self.originals)

    def test_process_death_before_and_after_link_recovers(self):
        for phase in ('before','after'):
            with self.subTest(phase=phase):
                self.copy=self.root/phase;shutil.copytree(self.source,self.copy)
                code='''
import os,sys
from paper_agents import import_state
original=os.link
def interrupted(*args,**kwargs):
    if sys.argv[3]=='after':original(*args,**kwargs)
    os._exit(9)
import_state.os.link=interrupted
import_state.initialize(sys.argv[1],sys.argv[2])
'''
                child=subprocess.run([sys.executable,'-c',code,str(self.copy),str(self.manifest),phase],cwd=ROOT,timeout=10)
                self.assertEqual(child.returncode,9)
                stage=self.copy/import_state.staging_name(self.approved)
                self.assertTrue(stage.exists())
                self.assertEqual((self.copy/import_state.RECEIPT).exists(),phase=='after')
                self.start();self.start()
                self.assertFalse(stage.exists())
                self.assertEqual(import_state.inspect(self.copy),{k:self.approved[k] for k in ('files','schema_sha256','tables')})
                self.assertEqual(files(self.source),self.originals)

    def test_staging_collisions_are_preserved(self):
        stage=self.copy/import_state.staging_name(self.approved)
        stage.write_bytes(b'user file')
        with self.assertRaisesRegex(RuntimeError,'Unowned'):self.start()
        self.assertEqual(stage.read_bytes(),b'user file')
        (self.copy/import_state.LOCK).write_text('foreign owner')
        with self.assertRaisesRegex(RuntimeError,'Unowned'):self.start()
        self.assertEqual(stage.read_bytes(),b'user file')
        (self.copy/import_state.LOCK).write_text(json.dumps({'format':1,'manifest_sha256':import_state.digest(self.approved)},sort_keys=True))
        with self.assertRaisesRegex(RuntimeError,'staging content'):self.start()
        self.assertEqual(stage.read_bytes(),b'user file')
        stage.unlink();stage.symlink_to(self.source/'data/profile.json')
        with self.assertRaisesRegex(RuntimeError,'staging file'):self.start()
        self.assertTrue(stage.is_symlink())
        self.assertEqual(files(self.source),self.originals)

    def test_foreign_manifest_staging_and_user_tmp_are_not_ignored(self):
        for name in ('data/.import-receipt-'+'0'*64+'.pending','data/tmp-user-file'):
            path=self.copy/name;path.write_bytes(b'user owned')
            with self.assertRaisesRegex(RuntimeError,'differs from manifest'):self.start()
            self.assertEqual(path.read_bytes(),b'user owned')
            path.unlink()
        name=import_state.staging_name(self.approved)
        changed=copy.deepcopy(self.approved);changed['files'][name]='0'*64
        # Exact reserved collision is checked against the manifest-derived name.
        with patch.object(import_state,'staging_name',return_value=name):
            self.manifest.write_text(json.dumps(changed))
            with self.assertRaisesRegex(RuntimeError,'collides with snapshot'):self.start()


class LockPreservationTests(unittest.TestCase):
    setUp = ImportTests.setUp
    start = ImportTests.start

    def test_hardlinked_locks_preserve_artifact_database_and_external_file(self):
        external=self.root/'external-sentinel';external.write_bytes(b'external sentinel')
        for kind in ('artifact','database','external'):
            with self.subTest(kind=kind):
                self.copy=self.root/kind;shutil.copytree(self.source,self.copy)
                target={'artifact':self.copy/'data/papers/test.pdf',
                        'database':self.copy/'data/paper_agent.db','external':external}[kind]
                lock=self.copy/import_state.LOCK;lock.hardlink_to(target)
                before=files(self.copy);sentinel=external.read_bytes()
                inode=(lock.stat().st_dev,lock.stat().st_ino)
                with self.assertRaisesRegex(RuntimeError,'isolated regular file'):self.start()
                self.assertEqual(files(self.copy),before)
                self.assertEqual(external.read_bytes(),sentinel)
                self.assertEqual((lock.stat().st_dev,lock.stat().st_ino),inode)
                self.assertTrue(os.path.samefile(lock,target))
                self.assertFalse((self.copy/import_state.RECEIPT).exists())
                self.assertEqual(files(self.source),self.originals)

    def test_fifo_lock_rejected_without_blocking_or_replacement(self):
        lock=self.copy/import_state.LOCK;os.mkfifo(lock)
        before=lock.lstat()
        code='''
import sys
from paper_agents.import_state import initialize
try:
    initialize(sys.argv[1],sys.argv[2])
except RuntimeError as error:
    assert 'isolated regular file' in str(error), str(error)
    sys.exit(3)
raise AssertionError('FIFO was accepted')
'''
        child=subprocess.run([sys.executable,'-c',code,str(self.copy),str(self.manifest)],cwd=ROOT,capture_output=True,text=True,timeout=5)
        self.assertEqual(child.returncode,3,child.stderr)
        self.assertEqual((lock.lstat().st_mode,lock.lstat().st_ino),(before.st_mode,before.st_ino))
        self.assertFalse((self.copy/import_state.RECEIPT).exists())
        self.assertEqual(files(self.source),self.originals)

    def test_opened_descriptor_must_match_path_before_write(self):
        lock=self.copy/import_state.LOCK;lock.write_text('existing isolated lock')
        target=self.copy/'data/papers/test.pdf';before=target.read_bytes()
        original=os.open
        def substituted(path,flags,*args):
            return original(target if Path(path)==lock else path,flags,*args)
        with patch.object(import_state.os,'open',substituted):
            with self.assertRaisesRegex(RuntimeError,'isolated regular file'):self.start()
        self.assertEqual(lock.read_text(),'existing isolated lock')
        self.assertEqual(target.read_bytes(),before)
        self.assertFalse((self.copy/import_state.RECEIPT).exists())

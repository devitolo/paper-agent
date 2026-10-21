"""AWS-only source/runtime and synthetic acceptance orchestration tests; no Docker."""
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from paper_agents.db import init_db
from paper_agents import package_runtime, runtime_config, web
from paper_agents.aws_serving import validate

ROOT=Path(__file__).resolve().parents[2]
SPEC=importlib.util.spec_from_file_location('aws_image_acceptance',ROOT/'scripts/aws_image_acceptance.py')
acceptance=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(acceptance)
IMAGE='ghcr.io/devitolo/paper-agent@sha256:'+'a'*64
REV='b'*40
BUNDLE='c'*64
FLAGS={'PAPER_AGENT_PACKAGED':'1','PAPER_AGENT_STARTUP_MODE':'serving','PAPER_AGENT_AWS_PRODUCTION':'1',
       'PAPER_AGENT_SERVING_ONLY':'1','PAPER_AGENT_SCHEDULER_ENABLED':'0','PAPER_AGENT_MINILM_ENABLED':'0',
       'PAPER_AGENT_TELEMETRY':'0','PAPER_AGENT_GEMINI_ENABLED':'0','PAPER_AGENT_DEFAULT_SOURCES':'openalex,arxiv',
       'PAPER_NON_PRODUCTION':'0','PAPER_AGENT_RUNTIME_ROLE':'ui'}


class AWSStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        (self.root/'data').mkdir();(self.root/'config').mkdir()
        init_db(self.root/'data/paper_agent.db',ROOT/'sql/schema.sql')
        (self.root/'data/profile.json').write_text(json.dumps({'interests':[],'positive_signals':[],'negative_signals':[],'feedback_history':[]}))
        (self.root/'config/topics.yaml').write_text('topics: []\n');(self.root/'config/industry.json').write_text(json.dumps({'enabled':False,'include_signals':[],'exclude_signals':[]}))
        with closing(sqlite3.connect(self.root/'data/paper_agent.db')) as db:
            db.execute("INSERT INTO papers(canonical_key,title) VALUES('existing','Retain')");db.commit()
        self.env=patch.dict(os.environ,FLAGS,clear=True);self.env.start();self.addCleanup(self.env.stop)

    def files(self):return {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_production_startup_preserves_existing_state_without_marker_or_seed(self):
        before=self.files()
        with patch('paper_agents.mini_nonprod_state.validate',side_effect=AssertionError('nonprod validator')):
            package_runtime.initialize(self.root)
            self.assertEqual(init_db(self.root/'data/paper_agent.db')['tables'],validate(self.root)['tables'])
        self.assertEqual(before,self.files())
        self.assertEqual(runtime_config.default_sources(),['openalex','arxiv'])

    def test_feedback_survives_revalidation_without_models(self):
        with patch.object(web,'apply_feedback_to_profile',side_effect=AssertionError('provider')):
            web.save_feedback(self.root/'data/paper_agent.db',paper_id=1,status='reviewed',notes='Decision: keep\nScore: 5\nReason: synthetic',feedback_content='Decision: keep\nScore: 5\nReason: synthetic')
        package_runtime.initialize(self.root)
        with closing(sqlite3.connect(self.root/'data/paper_agent.db')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM raw_feedback').fetchone()[0],1)

    def test_missing_db_and_unknown_schema_fail_without_changes(self):
        path=self.root/'data/paper_agent.db'
        with closing(sqlite3.connect(path)) as db:db.execute('ALTER TABLE papers ADD COLUMN unexpected TEXT');db.commit()
        before=self.files()
        with self.assertRaisesRegex(RuntimeError,'Unapproved'):package_runtime.initialize(self.root)
        self.assertEqual(before,self.files())
        path.unlink()
        with self.assertRaises(RuntimeError):package_runtime.initialize(self.root)
        self.assertFalse(path.exists())

    def test_acceptance_http_feedback_and_restart_checks_execute_against_serving_handler(self):
        import threading
        from http.server import ThreadingHTTPServer
        old=Path.cwd();os.chdir(self.root)
        server=None
        try:
            package_runtime.initialize(self.root)
            with closing(sqlite3.connect(self.root/'data/paper_agent.db')) as db:
                db.execute("INSERT INTO raw_feedback(paper_id,content,content_hash) VALUES(1,'Synthetic retained feedback','synthetic-retained')");db.commit()
            # Source import is outside the changed cwd; auth is synthetic only.
            with patch.dict(os.environ,{'PAPER_AGENT_SYNC_SHARED_SECRET':'s'*32}):
                server=ThreadingHTTPServer(('127.0.0.1',0),web.make_handler(self.root/'data/paper_agent.db'))
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                origin='http://127.0.0.1:'+str(server.server_port)
                exec(compile(acceptance.PUBLIC_HEALTH.replace('http://127.0.0.1:8000',origin),'health','exec'),{})
                exec(compile(acceptance.WRITE_FEEDBACK.replace('http://127.0.0.1:8000',origin),'feedback','exec'),{})
                package_runtime.initialize(self.root)
                exec(compile(acceptance.VERIFY_STATE.replace('/app',str(self.root)),'state','exec'),{})
        finally:
            if server is not None:server.shutdown();server.server_close();thread.join()
            os.chdir(old)

    def test_guard_rejects_pipeline_role_and_nonproduction_mode(self):
        for key,value in [('PAPER_AGENT_RUNTIME_ROLE','pipeline'),('PAPER_NON_PRODUCTION','1'),('PAPER_AGENT_SCHEDULER_ENABLED','1'),('PAPER_AGENT_DEFAULT_SOURCES','')]:
            with self.subTest(key=key),patch.dict(os.environ,{key:value}):
                with self.assertRaises(ValueError):runtime_config.migration_settings()

    def test_existing_nonproduction_contract_still_requires_its_marker(self):
        with patch.dict(os.environ,{'PAPER_AGENT_AWS_PRODUCTION':'0','PAPER_NON_PRODUCTION':'1'}):
            with self.assertRaisesRegex(RuntimeError,'synthetic'):package_runtime.initialize(self.root)


class BuildContractTests(unittest.TestCase):
    def test_hosted_manual_build_has_attestations_and_no_deploy(self):
        text=(ROOT/'.github/workflows/aws-production-build.yml').read_text()
        for expected in ('workflow_dispatch:',"github.ref == 'refs/heads/aws-production'",'runs-on: ubuntu-24.04','target: cloud-production','platforms: linux/amd64','provenance: mode=max','sbom: true','steps.image.outputs.digest','scripts/aws_image_acceptance.py','git archive --format=tar HEAD'):
            self.assertIn(expected,text)
        for forbidden in ('self-hosted','sudo ','ssh ','systemctl','workflow_run','environment: aws-production','project-paper-aws-deploy submit','mini-production','nonprod'):
            self.assertNotIn(forbidden,text)
        self.assertNotIn('\n  push:',text)
        self.assertNotIn('\n  pull_request:',text)

    def test_cloud_stage_excludes_minilm_and_keeps_default_runtime(self):
        text=(ROOT/'Dockerfile').read_text()
        cloud=text.split('FROM migration-gemini AS cloud-production',1)[1].split('FROM base AS runtime',1)[0]
        self.assertIn('org.projectpaper.runtime="cloud-production"',cloud)
        self.assertIn('PAPER_AGENT_AWS_PRODUCTION=1',cloud)
        self.assertIn('SOURCE_BUNDLE_SHA256',cloud)
        self.assertNotIn('requirements-minilm',cloud)
        self.assertNotIn('pip install',cloud)
        self.assertTrue(text.rsplit('FROM ',1)[1].startswith('base AS runtime'))


class AcceptanceControllerTests(unittest.TestCase):
    def run_case(self,fail=False):
        calls=[]
        def command(args,timeout=120):
            calls.append(args)
            if args[:3]==['docker','image','inspect']:
                return json.dumps([{'Os':'linux','Architecture':'amd64','Config':{'Labels':{'org.projectpaper.runtime':'cloud-production','org.opencontainers.image.revision':REV,'org.projectpaper.source-bundle-sha256':BUNDLE}}}])
            if args[:3]==['docker','ps','-aq']:return 'synthetic-container-id'
            if args[:2]==['docker','inspect']:return json.dumps([{'State':{'OOMKilled':False,'Health':{'Status':'healthy'}},'RestartCount':0}])
            if fail and args[-1]==acceptance.WRITE_FEEDBACK:raise RuntimeError('injected acceptance failure')
            return ''
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'result.json'
            if fail:
                with self.assertRaisesRegex(RuntimeError,'injected'):acceptance.accept(IMAGE,REV,BUNDLE,output,command=command,sleep=lambda _:None)
                self.assertFalse(output.exists())
            else:
                record=acceptance.accept(IMAGE,REV,BUNDLE,output,command=command,sleep=lambda _:None)
                self.assertEqual(record['image'],IMAGE);self.assertFalse(record['production_approved'])
            return calls

    def test_exact_digest_success_and_uuid_cleanup(self):
        calls=self.run_case()
        self.assertIn(['docker','pull',IMAGE],calls)
        removals=[args for args in calls if args[:3]==['docker','volume','rm']]
        self.assertEqual(len(removals),3)
        self.assertTrue(all(args[-1].startswith('paper-aws-accept-') for args in removals))
        self.assertTrue(any(args[:2]==['docker','restart'] for args in calls))
        self.assertFalse(any('prune' in args for args in calls))

    def test_failed_feedback_acceptance_has_no_success_record_and_cleans_up(self):
        calls=self.run_case(True)
        self.assertEqual(len([c for c in calls if c[:3]==['docker','rm','-f']]),3)
        self.assertEqual(len([c for c in calls if c[:3]==['docker','volume','rm']]),3)

    def test_tag_rejected_before_docker(self):
        calls=[]
        with self.assertRaises(ValueError):acceptance.accept('ghcr.io/devitolo/paper-agent:latest',REV,BUNDLE,Path('unused'),command=lambda *a,**kw:calls.append(a))
        self.assertFalse(calls)

if __name__=='__main__':unittest.main()

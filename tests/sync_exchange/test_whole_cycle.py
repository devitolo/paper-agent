"""Real HTTPS, shell/container boundary, complete restore and publish regressions."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
import inspect
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from paper_agents.db import init_db
from paper_agents.sync_exchange import connect_db, encoded, receive_result_batch
from paper_agents.web import make_handler
from paper_agents.whole_cycle_exchange import export_delta, push, recover
import paper_agents.whole_cycle_exchange as whole_cycle_exchange

REPO=Path(__file__).resolve().parents[2]


class WholeCycleTests(unittest.TestCase):
    def test_pull_uses_gzip_backup_endpoint(self):
        source = inspect.getsource(whole_cycle_exchange.main)
        self.assertIn("endpoint='/sync/backup'", source)
        self.assertNotIn("endpoint='/sync/snapshot'", source)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve(); self.aws=self.root/'aws'; self.mini=self.root/'mini'
        for root in (self.aws,self.mini):
            (root/'data').mkdir(parents=True); (root/'config').mkdir()
            (root/'config/topics.yaml').write_text('topics: []\n'); (root/'config/industry.json').write_text('{}')
            (root/'data/profile.json').write_text('{"interests":[],"positive_signals":[],"negative_signals":[],"feedback_history":[]}')
            init_db(root/'data/paper_agent.db')
        self.db=self.aws/'data/paper_agent.db'; self.mdb=self.mini/'data/paper_agent.db'; self.state=self.mini/'data/sync-state'
        with connect_db(self.db) as c:
            self.pid=c.execute("INSERT INTO papers(canonical_key,title) VALUES('updated','Old')").lastrowid
            c.execute('INSERT INTO raw_feedback(paper_id,content,content_hash) VALUES(?,?,?)',(self.pid,'before pull','before'))
        with connect_db(self.mdb) as c:
            c.execute("INSERT INTO papers(canonical_key,title) VALUES('mini-only','Must disappear')")
            c.execute("INSERT INTO openalex_search_state VALUES('cursor','{\"next\":42}')")
        self.env=patch.dict(os.environ,{'PAPER_AGENT_SYNC_SHARED_SECRET':'s'*32,'PAPER_AGENT_SYNC_PRODUCER':'mini-production','PAPER_AGENT_SYNC_ENVIRONMENT':'production'})
        self.env.start(); self.addCleanup(self.env.stop)
        cert=self.root/'cert.pem'; key=self.root/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        handler=make_handler(self.db,sync_root=self.aws)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cert,key)
        self.server.socket=context.wrap_socket(self.server.socket,server_side=True)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start(); self.addCleanup(self.close)
        self.url=f'https://localhost:{self.server.server_port}'
        self.bin=self.root/'bin'; self.bin.mkdir(); (self.root/'lifecycle').mkdir()
        # Substitute only Docker: preserve the complete real shell argv and invoke
        # the actual module using a temporary volume root instead of /app.
        docker=self.bin/'docker'; docker.write_text('#!'+sys.executable+'\n'+'''import os,sys,subprocess,json
args=sys.argv[1:]
with open(os.environ['DOCKER_ARGS'],'a') as f:f.write(json.dumps(args)+'\\n')
if 'paper_agents.migration_job' in args:
 stage=args[-1]
 sys.exit(subprocess.call([sys.executable,os.environ['STAGE_HELPER'],stage]))
start=args.index('env')+1
while '=' in args[start]:
 k,v=args[start].split('=',1);os.environ[k]=v;start+=1
args=args[start:];args[0]=sys.executable
args=[os.environ['MINI_ROOT']+v[4:] if v.startswith('/app') else v for v in args]
sys.exit(subprocess.call(args))
'''); docker.chmod(0o755)
        self.cli_env=dict(os.environ,PATH=str(self.bin)+os.pathsep+os.environ['PATH'],SSL_CERT_FILE=str(cert),PAPER_AGENT_SYNC_ENABLED='1',PAPER_AGENT_SYNC_EXPLICIT_PRODUCTION='1',PAPER_MIGRATION_ENV_FILE=str(self.root/'private.env'),PAPER_AGENT_SYNC_AWS_URL=self.url,PAPER_AGENT_LIFECYCLE_DIR=str(self.root/'lifecycle'),MINI_ROOT=str(self.mini),DOCKER_ARGS=str(self.root/'docker-args.jsonl'),PYTHONDONTWRITEBYTECODE='1')
        self.cert_env=patch.dict(os.environ,{'SSL_CERT_FILE':str(cert)}); self.cert_env.start(); self.addCleanup(self.cert_env.stop)

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def shell(self,command,success=True):
        result=subprocess.run(['bash',str(REPO/'scripts/mini_sync_exchange.sh'),command],cwd=REPO,env=self.cli_env,capture_output=True,text=True)
        if success:self.assertEqual(result.returncode,0,result.stderr)
        else:self.assertNotEqual(result.returncode,0,result.stdout)
        return result

    def stages(self, stages=None):
        # Four source stages plus profile, with two curator attempts per source.
        for stage in stages or ('openalex','arxiv','semantic','core','profile'):
            if stage=='profile':
                (self.mini/'data/profile.json').write_text('{"mini_only_profile":true}')
                continue
            with connect_db(self.mdb) as c:
                cycle=c.execute("INSERT INTO workflow_cycles(state,mode) VALUES('completed',?)",(stage,)).lastrowid
                source='semantic_scholar' if stage=='semantic' else stage
                scout=c.execute('INSERT INTO scout_runs(workflow_cycle_id,source,target_candidates,max_candidates,freshness_months) VALUES(?,?,3,3,6)',(cycle,source)).lastrowid
                for number in range(3):
                    key='updated' if stage=='openalex' and number==0 else f'{stage}-{number}'
                    c.execute('INSERT INTO papers(canonical_key,title,updated,authors_json,categories_json) VALUES(?,?,?,?,?) ON CONFLICT(canonical_key) DO UPDATE SET title=excluded.title,updated=excluded.updated,authors_json=excluded.authors_json,categories_json=excluded.categories_json',(key,'New '+key,'2026-10-21','["Author"]','["cs.AI"]'))
                    pid=c.execute('SELECT id FROM papers WHERE canonical_key=?',(key,)).fetchone()[0]
                    c.execute('INSERT INTO paper_sources(paper_id,source,source_id) VALUES(?,?,?)',(pid,source,key))
                    c.execute('INSERT INTO scout_candidates(scout_run_id,paper_id,retrieval_order) VALUES(?,?,?)',(scout,pid,number+1))
                    if number==2:continue # Discovered, never evaluated.
                    curator=c.execute('INSERT INTO curator_runs(workflow_cycle_id) VALUES(?)',(cycle,)).lastrowid
                    c.execute('INSERT INTO curator_evaluations(curator_run_id,paper_id,score,rationale) VALUES(?,?,?,?)',(curator,pid,90-number,'score reason'))
                    if number==0:
                        c.execute('INSERT INTO recommendations(curator_run_id,paper_id,recommendation_order,rationale) VALUES(?,?,1,?)',(curator,pid,'recommend'))
                    # Same order in a separate curator run must be preserved.
                    if number==1 and stage=='openalex':
                        c.execute('INSERT INTO recommendations(curator_run_id,paper_id,recommendation_order,rationale) VALUES(?,?,1,?)',(curator,pid,'second run'))
                    path=f'data/{key}-summary.json'; (self.mini/path).write_text(json.dumps({'merged':{'research_problem':key}}))
                    c.execute("INSERT INTO artifacts(paper_id,artifact_type,path) VALUES(?,'triage_summary',?)",(pid,path))
                    c.execute("INSERT INTO artifacts(paper_id,artifact_type,path) VALUES(?,'pdf','/mini/private.pdf')",(pid,))

    def test_https_full_cycle_retry_feedback_and_grouping(self):
        self.shell('pull')
        with connect_db(self.mdb) as c:
            self.assertIsNone(c.execute("SELECT 1 FROM papers WHERE canonical_key='mini-only'").fetchone())
            self.assertEqual(c.execute('SELECT content FROM raw_feedback').fetchone()[0],'before pull')
            self.assertEqual(c.execute("SELECT value_json FROM openalex_search_state WHERE key='cursor'").fetchone()[0],'{"next":42}')
        with connect_db(self.db) as c:
            c.execute('INSERT INTO raw_feedback(paper_id,content,content_hash) VALUES(?,?,?)',(self.pid,'after pull','after'))
        self.stages()
        payload=export_delta(self.mdb,self.mini,self.state)
        self.assertEqual(set(payload),{'schema','normalized','results'})
        self.assertEqual(len(payload['normalized']['papers']),12)
        self.assertEqual(len(payload['normalized']['evaluations']),8)
        self.assertTrue(all(r['artifact_type']=='triage_summary' and 'path' not in r for r in payload['normalized']['artifacts']))
        from paper_agents.sync_worker import request
        calls=[]
        def lost(*args):
            calls.append(args[2]); request(*args); raise OSError('response lost after commit')
        with self.assertRaises(OSError):push(self.mdb,self.mini,self.state,self.url,transport=lost)
        pending=(self.state/'pipeline-outbox.json').read_bytes()
        self.shell('pull',success=False)
        self.assertEqual((self.state/'pipeline-outbox.json').read_bytes(),pending)
        self.shell('push') # Real CLI replays same batch through HTTPS.
        self.assertFalse((self.state/'pipeline-outbox.json').exists())
        self.assertNotEqual((self.aws/'data/profile.json').read_text(),(self.mini/'data/profile.json').read_text())
        with connect_db(self.db) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM papers').fetchone()[0],12)
            row=c.execute("SELECT title,updated,authors_json,categories_json FROM papers WHERE canonical_key='updated'").fetchone()
            self.assertEqual(tuple(row),('New updated','2026-10-21','["Author"]','["cs.AI"]'))
            self.assertEqual(c.execute('SELECT COUNT(*) FROM curator_evaluations').fetchone()[0],8)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM curator_runs').fetchone()[0],8)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM workflow_cycles').fetchone()[0],4)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM recommendations').fetchone()[0],5)
            self.assertEqual([r[0] for r in c.execute('SELECT content FROM raw_feedback ORDER BY id')],['before pull','after pull'])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM sync_result_batches').fetchone()[0],1)
            for row in c.execute('SELECT path FROM artifacts'):
                self.assertTrue((self.aws/row[0]).is_file())
            self.assertIsNone(c.execute('PRAGMA foreign_key_check').fetchone())
        argv=json.loads((self.root/'docker-args.jsonl').read_text().splitlines()[0])
        self.assertIn('app',argv); self.assertIn('/app/data/paper_agent.db',argv)
        self.assertIn('paper_agents.whole_cycle_exchange',argv)

    def test_actual_daily_wrapper_uses_container_pull_and_push(self):
        helper=self.root/'stage.py'
        helper.write_text("import os,sys\nfrom pathlib import Path\nfrom tests.sync_exchange.test_whole_cycle import WholeCycleTests\nt=WholeCycleTests(); t.mini=Path(os.environ['MINI_ROOT']); t.mdb=t.mini/'data/paper_agent.db'\nt.stages([sys.argv[1]])\nwith open(os.environ['STAGE_ORDER'],'a') as f:f.write(sys.argv[1]+'\\n')\n")
        # macOS has no flock binary; use the same advisory descriptor lock API.
        flock=self.bin/'flock'
        flock.write_text('#!'+sys.executable+'\nimport fcntl,sys\nfcntl.flock(int(sys.argv[-1]),fcntl.LOCK_EX|fcntl.LOCK_NB)\n')
        flock.chmod(0o755)
        env=dict(self.cli_env,STAGE_HELPER=str(helper),STAGE_ORDER=str(self.root/'order'),PYTHONPATH=str(REPO),PAPER_AGENT_ORCHESTRATOR_LOCK_FILE=str(self.root/'cycle.lock'),PAPER_AGENT_ORCHESTRATOR_STATUS_FILE=str(self.root/'status.json'),PAPER_AGENT_ORCHESTRATOR_LOG_DIR=str(self.root/'logs'))
        result=subprocess.run(['bash',str(REPO/'scripts/mini_sequential_orchestrator.sh')],env=env,cwd=REPO,capture_output=True,text=True)
        if result.returncode:
            logs='\n'.join(p.read_text() for p in (self.root/'logs').glob('*.log'))
            self.fail(result.stderr+'\n'+logs)
        self.assertEqual((self.root/'order').read_text().splitlines(),['openalex','arxiv','semantic','core','profile'])
        self.assertEqual(json.loads((self.root/'status.json').read_text())['overall_result'],'completed')
        self.assertEqual(len(list(self.state.glob('published-*.json'))),1)
        published=json.loads(next(self.state.glob('published-*.json')).read_text())
        with connect_db(self.db) as c:
            self.assertTrue(c.execute('SELECT 1 FROM sync_snapshot_revisions WHERE revision=?',(published['input_revision'],)).fetchone())
        with connect_db(self.db) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM papers').fetchone()[0],12)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM curator_evaluations').fetchone()[0],8)

    def test_sql_conflict_rolls_back_metadata_and_preserves_feedback(self):
        self.shell('pull'); self.stages()
        with connect_db(self.db) as c:
            c.execute("INSERT INTO paper_sources(paper_id,source,source_id) VALUES(?,'openalex','openalex-1')",(self.pid,))
        from paper_agents.sync_worker import request
        with self.assertRaises(Exception):
            push(self.mdb,self.mini,self.state,self.url,transport=request)
        with connect_db(self.db) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM papers').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT title FROM papers').fetchone()[0],'Old')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM raw_feedback').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM sync_result_batches').fetchone()[0],0)
        self.assertTrue((self.state/'pipeline-outbox.json').is_file())

    def test_disabled_shell_and_explicit_cli_guard(self):
        result=subprocess.run(['bash',str(REPO/'scripts/mini_sync_exchange.sh'),'pull'],env=dict(self.cli_env,PAPER_AGENT_SYNC_ENABLED='0'),capture_output=True,text=True)
        self.assertEqual(result.returncode,0)
        self.assertFalse((self.root/'docker-args.jsonl').exists())
        result=subprocess.run([sys.executable,'-m','paper_agents.whole_cycle_exchange','pull','--db-path',str(self.mdb),'--root',str(self.mini),'--state-dir',str(self.state),'--aws-url',self.url],cwd=REPO,env=dict(self.cli_env,PAPER_AGENT_SYNC_EXPLICIT_PRODUCTION='0'),capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(self.state.exists())

    def test_malformed_batch_rejected_without_database_writes(self):
        self.shell('pull'); self.stages()
        from paper_agents.sync_worker import request
        def invalid(*args):
            payload=json.loads(args[2]); payload['normalized']['recommendations'].append(dict(payload['normalized']['recommendations'][0]))
            with self.assertRaises(Exception):request(args[0],args[1],encoded(payload),args[3],args[4],args[5])
            raise OSError('expected invalid batch')
        with self.assertRaises(OSError):push(self.mdb,self.mini,self.state,self.url,transport=invalid)
        with connect_db(self.db) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM papers').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM curator_runs').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM sync_result_batches').fetchone()[0],0)
        self.assertFalse((self.aws/'data/sync-summaries').exists())

    def test_failed_cycle_recovery_preserves_outbox_and_is_idempotent(self):
        self.shell('pull')
        result = recover(self.state, 'synthetic source failure')
        self.assertEqual(result['status'], 'recovered')
        self.assertFalse((self.state/'active-cycle.json').exists())
        self.assertTrue((self.state/'recovery-status.json').is_file())
        self.assertEqual(recover(self.state)['status'], 'no-op')

    def test_pending_outbox_recovery_keeps_retryable_payload(self):
        self.shell('pull'); self.stages(['openalex'])
        from paper_agents.sync_worker import request
        with self.assertRaises(Exception):
            push(self.mdb, self.mini, self.state, self.url, transport=lambda *args: (_ for _ in ()).throw(OSError('offline')))
        pending = (self.state/'pipeline-outbox.json').read_bytes()
        result = recover(self.state, 'push failure')
        self.assertTrue(result['outbox_pending'])
        self.assertEqual((self.state/'pipeline-outbox.json').read_bytes(), pending)
        self.assertFalse((self.state/'active-cycle.json').exists())

if __name__=='__main__':unittest.main()

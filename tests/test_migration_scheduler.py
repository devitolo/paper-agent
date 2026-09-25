from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from paper_agents import scheduler, migration_lifecycle, scheduler_child, manual_scout
from tests import test_migration_foundations as fixtures

ROOT=Path(__file__).resolve().parents[1]


def utc(value):return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


class ScheduleTests(unittest.TestCase):
    def test_fixed_schedule_dst_and_missed_minutes(self):
        self.assertEqual([j.name for j in scheduler.due(utc('2026-09-21T11:00:45'))],['openalex'])
        self.assertEqual([j.name for j in scheduler.due(utc('2026-09-21T12:00:00'))],['arxiv'])
        self.assertEqual([j.name for j in scheduler.due(utc('2026-09-21T13:00:00'))],['semantic'])
        self.assertEqual(scheduler.due(utc('2026-09-21T12:01:00')),[])
        self.assertEqual(scheduler.due(utc('2026-09-22T00:00:00')),[])
        first,second=utc('2026-11-01T08:00:00'),utc('2026-11-01T09:00:00')
        self.assertEqual(scheduler.key(scheduler.due(first)[0],first),scheduler.key(scheduler.due(second)[0],second))
        self.assertEqual(utc('2026-03-08T10:00:00').astimezone(scheduler.ZONE).hour,3)
        self.assertEqual(scheduler.due(utc('2026-03-08T10:00:00')),[])

    def test_environment_and_exact_existing_wrapper_commands(self):
        env=scheduler.child_environment('/app','/runtime-control',{'PAPER_AGENT_TODAY':'bad','SEMANTIC_SCHOLAR_API_KEY':'secret','PAPER_AGENT_SELF_UPDATE':'1'})
        self.assertNotIn('PAPER_AGENT_TODAY',env)
        self.assertEqual(env['PAPER_AGENT_SELF_UPDATE'],'0')
        self.assertEqual(env['PAPER_AGENT_TOPIC_SLOT'],'0')
        self.assertEqual(env['PAPER_AGENT_PROFILE_REBUILD_ANCHOR'],'2026-09-07')
        self.assertEqual(env['TZ'],'America/Los_Angeles')
        self.assertEqual(env['SEMANTIC_SCHOLAR_API_KEY'],'secret')
        for job in scheduler.JOBS:
            expected=['bash','/app/scripts/'+job.script]
            if job.name=='backup':expected+=['/app/data/paper_agent.db','/backups']
            self.assertEqual(scheduler.command(job,'/app','/backups'),expected)

    def test_disabled_and_unqualified_enabled_fail_closed(self):
        with patch.dict(os.environ,{},clear=True),patch.object(scheduler,'serve') as serve:
            self.assertEqual(scheduler.main(),0);serve.assert_not_called()
        with self.assertRaisesRegex(RuntimeError,'Gemini'):scheduler.qualify_integrations()
        with patch.dict(os.environ,{'PAPER_APP_IMAGE':'moving:tag','PAPER_MIGRATION_OLLAMA_IMAGE':'moving:tag'},clear=True):
            with self.assertRaises(ValueError):migration_lifecycle.validate_container_contract()


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        fixtures.ImportTests.setUp(self)
        self.control=self.root/'control';self.control.mkdir()
        self.logs=self.root/'logs';self.logs.mkdir()
        self.backup=self.root/'backups';self.backup.mkdir()
        from paper_agents import import_state
        import_state.initialize(self.copy,self.manifest)
        self.launched=[]
        class Process:
            pid=99999999
            returncode=None
            def poll(self):return self.returncode
            def wait(self,timeout=None):return self.returncode
        self.process=Process()
        def launch(*args,**kwargs):self.launched.append((args,kwargs));return self.process
        self.now=0
        self.engine=scheduler.Scheduler(self.copy,self.control,self.logs,self.backup,self.manifest,
            ready=lambda:True,clock=lambda:self.now,launcher=launch,grace=6,budget=20)
        self.cleanup=patch.object(self.engine,'stop_group',side_effect=lambda p:None);self.cleanup.start()
        self.addCleanup(self.cleanup.stop);self.addCleanup(self.engine.close)

    def rows(self):return self.engine.journal.connection.execute('SELECT job,status,reason FROM jobs ORDER BY key').fetchall()

    def test_claim_once_busy_skip_and_restart_never_replays(self):
        self.engine.tick(utc('2026-09-21T12:00:00'));self.engine.tick(utc('2026-09-21T12:00:50'))
        self.engine.tick(utc('2026-09-21T13:00:00'))
        self.assertEqual(len(self.launched),1)
        self.assertIn(('semantic','skipped','scheduler_busy'),self.rows())
        self.engine.close()
        self.engine.journal=scheduler.Journal(self.logs)
        self.engine.tick(utc('2026-09-21T12:00:55'))
        self.assertEqual(len(self.launched),1)
        self.assertIn(('arxiv','interrupted','scheduler_stopped'),self.rows())

    def test_claim_before_launch_and_crash_claim_not_replayed(self):
        instant=utc('2026-09-21T12:00:00');job=[j for j in scheduler.JOBS if j.name=='arxiv'][0]
        identity=scheduler.key(job,instant)
        self.engine.journal.claim(identity,'arxiv')
        self.engine.journal.close();self.engine.journal=scheduler.Journal(self.logs)
        self.engine.tick(instant)
        self.assertEqual(self.launched,[])
        self.assertIn(('arxiv','interrupted','scheduler_restarted'),self.rows())

    def test_lifecycle_barrier_and_readiness_release(self):
        with migration_lifecycle.lease(self.control,exclusive=True):
            self.engine.tick(utc('2026-09-21T12:00:00'))
        self.assertIn(('arxiv','skipped','initialization_busy'),self.rows())
        self.engine.ready=lambda:False
        self.engine.tick(utc('2026-09-21T13:00:00'))
        self.assertIn(('semantic','skipped','app_not_ready'),self.rows())
        with migration_lifecycle.lease(self.control,exclusive=True):pass
        self.engine.ready=lambda:True
        self.engine.tick(utc('2026-09-22T11:00:00'))
        with self.assertRaisesRegex(RuntimeError,'busy'):
            with migration_lifecycle.lease(self.control,exclusive=True):pass
        self.process.returncode=0;self.engine.poll()
        with migration_lifecycle.lease(self.control,exclusive=True):pass
        self.assertEqual(len(self.launched),1)

    def test_manual_lock_busy_and_raced_exit75_are_skips(self):
        handle=manual_scout.acquire(self.copy/'data/paper_agent.db')
        try:self.engine.tick(utc('2026-09-21T12:00:00'))
        finally:handle.close()
        self.assertIn(('arxiv','skipped','pipeline_busy'),self.rows())
        self.engine.tick(utc('2026-09-21T13:00:00'));self.process.returncode=75;self.engine.poll()
        self.assertIn(('semantic','skipped','pipeline_busy'),self.rows())

    def test_anchor_and_privacy_and_timeout(self):
        self.engine.tick(utc('2026-09-14T09:00:00'))
        self.assertIn(('profile','skipped','outside_biweekly_cadence'),self.rows())
        self.engine.tick(utc('2026-09-21T09:00:00'))
        args,kwargs=self.launched[0]
        self.assertIn('biweekly_profile_rebuild_compare.sh',args[0][-1])
        self.assertEqual(kwargs['stdout'],subprocess.DEVNULL)
        self.assertEqual(kwargs['stderr'],subprocess.DEVNULL)
        self.assertEqual(len(kwargs['pass_fds']),1)
        self.now=21;self.engine.poll()
        self.assertIn(('profile','failed','wall_timeout'),self.rows())

    def test_readiness_delay_cannot_launch_after_due_minute(self):
        def delayed():
            self.now+=2
            return True
        self.engine.ready=delayed
        self.engine.tick(utc('2026-09-21T12:00:59'))
        self.assertEqual(self.launched,[])
        self.assertIn(('arxiv','skipped','minute_expired'),self.rows())
        with migration_lifecycle.lease(self.control,exclusive=True):pass



class ProcessLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_singleton_and_inherited_lease_survive_parent_release(self):
        with migration_lifecycle.lease(self.root,exclusive=True,name='.scheduler.lock'):
            code="from paper_agents.migration_lifecycle import lease; import sys;\nwith lease(sys.argv[1],exclusive=True,name='.scheduler.lock'): pass"
            child=subprocess.run([sys.executable,'-c',code,str(self.root)],cwd=ROOT,capture_output=True,timeout=5)
            self.assertNotEqual(child.returncode,0)
        held=migration_lifecycle.lease(self.root);fd=held.__enter__()
        child=subprocess.Popen([sys.executable,'-c','import sys;sys.stdin.read()'],stdin=subprocess.PIPE,pass_fds=(fd,))
        try:
            held.__exit__(None,None,None)
            with self.assertRaisesRegex(RuntimeError,'busy'):
                with migration_lifecycle.lease(self.root,exclusive=True):pass
        finally:child.communicate(timeout=10)
        with migration_lifecycle.lease(self.root,exclusive=True):pass

    def test_watchdog_bounds_resistant_child_and_releases_lease(self):
        pidfile=self.root/'pid'
        with migration_lifecycle.lease(self.root) as fd:
            command=[sys.executable,'-c',"import os,signal,time,pathlib,sys;pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)",str(pidfile)]
            start=time.monotonic()
            code=scheduler_child.run(command,os.dup(fd),self.root,.3)
            self.assertEqual(code,124);self.assertLess(time.monotonic()-start,8)
            with self.assertRaises(ProcessLookupError):os.kill(int(pidfile.read_text()),0)
        with migration_lifecycle.lease(self.root,exclusive=True):pass

    def test_watchdog_retains_singleton_after_scheduler_death(self):
        runtime=migration_lifecycle.lease(self.root);runtime_fd=runtime.__enter__()
        singleton=migration_lifecycle.lease(self.root,exclusive=True,name='.scheduler.lock');singleton_fd=singleton.__enter__()
        child=subprocess.Popen([sys.executable,'-m','paper_agents.scheduler_child',str(runtime_fd),str(singleton_fd),str(self.root),'.4',sys.executable,'-c','import time;time.sleep(60)'],cwd=ROOT,pass_fds=(runtime_fd,singleton_fd))
        runtime.__exit__(None,None,None);singleton.__exit__(None,None,None)
        try:
            for name in ('.runtime.lock','.scheduler.lock'):
                with self.assertRaisesRegex(RuntimeError,'busy'):
                    with migration_lifecycle.lease(self.root,exclusive=True,name=name):pass
            self.assertEqual(child.wait(timeout=9),124)
        finally:
            if child.poll() is None:child.kill();child.wait()
        for name in ('.runtime.lock','.scheduler.lock'):
            with migration_lifecycle.lease(self.root,exclusive=True,name=name):pass

    def test_runtime_hardlink_alias_rejected_without_modifying_target(self):
        target=self.root/'sentinel';target.write_bytes(b'user bytes')
        (self.root/'.runtime.lock').hardlink_to(target)
        with self.assertRaisesRegex(RuntimeError,'isolated runtime lock'):
            with migration_lifecycle.lease(self.root,exclusive=True):pass
        self.assertEqual(target.read_bytes(),b'user bytes')


class ContractTests(unittest.TestCase):
    def test_compose_rejects_blanks_and_resolves_shared_persistent_contract(self):
        if not shutil.which('docker'):self.skipTest('Compose CLI not installed; no Docker runtime required')
        base=['docker','compose','-f',str(ROOT/'docker-compose.mini-migration.yml'),'--profile','scheduler','--profile','model-verification','config','--format','json']
        env={'PATH':os.environ['PATH'],'HOME':os.environ['HOME']}
        rejected=subprocess.run(base,env=env,capture_output=True,text=True,timeout=10)
        self.assertNotEqual(rejected.returncode,0)
        env.update(PAPER_MIGRATION_PROJECT='paper-synthetic',PAPER_MIGRATION_APP_IMAGE='synthetic/app@sha256:'+'a'*64,
            PAPER_MIGRATION_OLLAMA_IMAGE='synthetic/ollama@sha256:'+'b'*64,PAPER_AGENT_GEMINI_ENABLED='0',
            PAPER_AGENT_TELEMETRY='1',PAPER_PHOENIX_DNS='synthetic-phoenix',PAPER_IMPORT_MANIFEST_FILE='/synthetic/manifest.json',
            PAPER_MIGRATION_PORT='18080',PAPER_GEMINI_KEY_FILE='/synthetic/private-key',SEMANTIC_SCHOLAR_API_KEY='synthetic-not-a-credential')
        for kind in ('DATA','CONFIG','CONTROL','LOG','BACKUP','MODEL'):env['PAPER_MIGRATION_'+kind+'_VOLUME']='synthetic-'+kind.lower()
        result=subprocess.run(base,env=env,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        value=json.loads(result.stdout);app=value['services']['app'];scheduled=value['services']['scheduler']
        # Browsing saved content must not depend directly or transitively on inference.
        pending=list(app.get('depends_on',{})); visited=set()
        while pending:
            name=pending.pop()
            if name in visited:continue
            visited.add(name)
            pending.extend(value['services'][name].get('depends_on',{}))
        self.assertFalse({'ollama','prepare-model'} & visited)
        self.assertEqual(value['services']['prepare-model']['profiles'],['model-verification'])
        self.assertEqual(app['image'],scheduled['image'])
        self.assertEqual(app['volumes'],scheduled['volumes'])
        self.assertEqual(scheduled['environment']['PAPER_AGENT_SCHEDULER_ENABLED'],'0')
        self.assertEqual(app['ports'][0]['host_ip'],'127.0.0.1')
        self.assertNotIn('ports',value['services']['ollama'])
        self.assertEqual(value['networks']['phoenix']['name'],'paper-observability_default')
        self.assertTrue(value['networks']['phoenix']['external'])
        mounts={item['target']:item for item in app['volumes']}
        for target in ('/app/data','/app/config','/app/logs','/backups','/runtime-control'):
            self.assertEqual(mounts[target]['type'],'volume')
        self.assertTrue(mounts['/import/manifest.json']['read_only'])
        self.assertFalse(mounts['/import/manifest.json']['bind']['create_host_path'])
        self.assertNotIn('docker.sock',result.stdout)
        self.assertEqual(value['services']['prepare-model']['environment']['PAPER_MODEL_MODE'],'verify')
        # Current fresh install file remains byte-identical to the authorized stable base.
        baseline=os.environ.get('PAPER_TEST_STABLE_COMPOSE')
        stable=Path(baseline).read_bytes() if baseline else subprocess.check_output(['git','show','b4b3fc6:docker-compose.yml'],cwd=ROOT)
        self.assertEqual((ROOT/'docker-compose.yml').read_bytes(),stable)

    def test_actual_source_wrappers_keep_existing_default_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'bin').mkdir();(root/'locks').mkdir()
            output=root/'args.json'
            python=root/'bin/python3';python.write_text(f'#!{sys.executable}\nimport json,sys,os\nopen(os.environ["CAPTURE"],"w").write(json.dumps(sys.argv[1:]))\n');python.chmod(0o755)
            flock=root/'bin/flock';flock.write_text('#!/bin/sh\nexit 0\n');flock.chmod(0o755)
            env=scheduler.child_environment(root,root/'locks',{'PATH':str(root/'bin')+':/usr/bin:/bin','CAPTURE':str(output),'PAPER_AGENT_OPENALEX_TOPIC':'synthetic fixture'})
            expectations={'arxiv':('20','3','90'),'openalex':('30','2','90'),'semantic':('10','1','120')}
            for job in scheduler.JOBS:
                if job.name not in expectations:continue
                result=subprocess.run(['bash',str(ROOT/'scripts'/job.script)],env=env,capture_output=True,timeout=5)
                self.assertEqual(result.returncode,0,result.stderr)
                args=json.loads(output.read_text())
                self.assertEqual(args[:3],['-m','paper_agents.cli','pipeline-daily'])
                for flag,wanted in zip(('--fetch','--keep','--source-timeout'),expectations[job.name]):
                    self.assertEqual(args[args.index(flag)+1],wanted)
                self.assertEqual(args[args.index('--topic-slot')+1],'0')
                self.assertIn('--quick',args)

    def test_app_initialization_is_exclusive_before_shared_serving(self):
        from paper_agents import package_runtime
        with tempfile.TemporaryDirectory() as directory:
            control=Path(directory)
            env={'PAPER_AGENT_STARTUP_MODE':'imported','PAPER_AGENT_LIFECYCLE_DIR':str(control),
                 'PAPER_APP_IMAGE':'synthetic/app@sha256:'+'a'*64,'PAPER_MIGRATION_OLLAMA_IMAGE':'synthetic/model@sha256:'+'b'*64}
            events=[]
            def initialize():
                with self.assertRaisesRegex(RuntimeError,'busy'):
                    with migration_lifecycle.lease(control):pass
                events.append('initialize')
            def web(**kwargs):
                with migration_lifecycle.lease(control):events.append('serve')
                with self.assertRaisesRegex(RuntimeError,'busy'):
                    with migration_lifecycle.lease(control,exclusive=True):pass
            with patch.dict(os.environ,env,clear=True),patch.object(sys,'argv',['package_runtime','start']),patch.object(package_runtime,'initialize',initialize),patch('paper_agents.web.run_review_ui',web):
                self.assertEqual(package_runtime.main(),0)
            self.assertEqual(events,['initialize','serve'])
            with migration_lifecycle.lease(control,exclusive=True):pass

    def test_manual_worker_inherits_lifetime_before_first_write(self):
        with tempfile.TemporaryDirectory() as directory:
            control=Path(directory)
            held=migration_lifecycle.lease(control);fd=held.__enter__()
            code='''
import os,sys
from pathlib import Path
from paper_agents import manual_scout
os.environ['PAPER_AGENT_STARTUP_MODE']='imported'
os.environ['PAPER_AGENT_LIFECYCLE_DIR']=sys.argv[1]
def synthetic_worker(*args):
    print('started',flush=True)
    sys.stdin.read()
    return 0
manual_scout.worker=synthetic_worker
raise SystemExit(manual_scout.worker_main(Path('unused'),-1,int(sys.argv[2])))
'''
            child=subprocess.Popen([sys.executable,'-c',code,str(control),str(fd)],cwd=ROOT,pass_fds=(fd,),stdin=subprocess.PIPE,stdout=subprocess.PIPE)
            held.__exit__(None,None,None)
            try:
                with self.assertRaisesRegex(RuntimeError,'busy'):
                    with migration_lifecycle.lease(control,exclusive=True):pass
                stdout,_=child.communicate(timeout=10)
                self.assertEqual(child.returncode,0)
                self.assertEqual(stdout.strip(),b'started')
            finally:
                if child.poll() is None:child.kill();child.wait()
            with migration_lifecycle.lease(control,exclusive=True):pass

    def test_readiness_probe_has_total_wall_deadline(self):
        def stalled(*args,**kwargs):time.sleep(30)
        start=time.monotonic()
        with patch.object(scheduler.urllib.request,'urlopen',stalled):
            self.assertFalse(scheduler.app_ready('http://synthetic.invalid/ready'))
        self.assertLess(time.monotonic()-start,8)

    def test_enabled_entrypoint_requires_import_and_ready_app_before_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            env={'PAPER_AGENT_SCHEDULER_ENABLED':'1','PAPER_APP_IMAGE':'synthetic/app@sha256:'+'a'*64,
                 'PAPER_MIGRATION_OLLAMA_IMAGE':'synthetic/model@sha256:'+'b'*64,
                 'PAPER_AGENT_LIFECYCLE_DIR':directory,'PAPER_AGENT_IMPORT_MANIFEST':'/synthetic/manifest'}
            for imported,ready in ((False,True),(True,False)):
                with self.subTest(imported=imported,ready=ready),patch.dict(os.environ,env,clear=True),patch.object(scheduler.runtime_config,'migration_settings',return_value={'PAPER_AGENT_GEMINI_ENABLED':True}),patch.object(scheduler,'qualify_integrations'),patch.object(scheduler,'accepted',return_value=imported),patch.object(scheduler,'app_ready',return_value=ready),patch.object(scheduler,'serve') as serve:
                    with self.assertRaisesRegex(RuntimeError,'initialized state and a ready application'):scheduler.main()
                    serve.assert_not_called()
                with migration_lifecycle.lease(directory,exclusive=True):pass


class JournalSchemaTests(unittest.TestCase):
    def test_unknown_constraints_rejected_before_any_state_change(self):
        cases=[
            scheduler.JOURNAL_SCHEMA.replace('key TEXT PRIMARY KEY','key TEXT'),
            scheduler.JOURNAL_SCHEMA.replace('key TEXT PRIMARY KEY','key TEXT UNIQUE'),
            scheduler.JOURNAL_SCHEMA.replace('status TEXT NOT NULL','status TEXT'),
            scheduler.JOURNAL_SCHEMA.replace('exit_code INTEGER','exit_code TEXT'),
            scheduler.JOURNAL_SCHEMA+'; CREATE INDEX extra ON jobs(job)',
            scheduler.JOURNAL_SCHEMA+"; CREATE TRIGGER rewrite AFTER UPDATE ON jobs BEGIN DELETE FROM jobs; END",
        ]
        for schema in cases:
            with self.subTest(schema=schema),tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'scheduler.sqlite'
                connection=sqlite3.connect(path)
                try:
                    connection.executescript(schema)
                    connection.execute("INSERT INTO jobs VALUES('same-slot','arxiv','running','original',NULL)")
                    connection.commit()
                    rows=connection.execute('SELECT * FROM jobs').fetchall()
                finally:connection.close()
                before=path.read_bytes()
                with self.assertRaisesRegex(RuntimeError,'Unexpected scheduler journal schema'):
                    scheduler.Journal(directory)
                self.assertEqual(path.read_bytes(),before)
                connection=sqlite3.connect(path)
                try:self.assertEqual(connection.execute('SELECT * FROM jobs').fetchall(),rows)
                finally:connection.close()
                self.assertEqual({item.name for item in Path(directory).iterdir()},{'scheduler.sqlite'})

    def test_valid_schema_suppresses_claim_after_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            journal=scheduler.Journal(directory)
            self.assertTrue(journal.claim('same-slot','arxiv'))
            self.assertFalse(journal.claim('same-slot','arxiv'))
            journal.close()
            journal=scheduler.Journal(directory)
            try:
                self.assertFalse(journal.claim('same-slot','arxiv'))
                self.assertEqual(journal.connection.execute('SELECT key,status FROM jobs').fetchall(),[('same-slot','interrupted')])
            finally:journal.close()

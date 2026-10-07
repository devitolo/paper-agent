"""Exercise actual host wrapper argv without Docker or any container calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from paper_agents import job_events, migration_job, migration_lifecycle
import unittest

ROOT = Path(__file__).resolve().parents[1]


class HostCronTests(unittest.TestCase):
    def test_minilm_eval_runner_is_pinned_offline_and_calls_experiment_module(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root/'docker'
            executable.write_text('#!'+sys.executable+'\nimport json,os,sys\n'
                'with open(os.environ["CAPTURE"],"a") as stream: stream.write(json.dumps(sys.argv[1:])+"\\n")\n'
                'if sys.argv[1:3] == ["image", "inspect"]: print("sha256:7d8b960220e3c6f60292e6d40a8f300ff19c5ee05cd97cf5f725a76673e2d5c2")\n')
            executable.chmod(0o700)
            capture = root/'calls'
            env = {**os.environ, 'PATH':str(root)+':'+os.environ['PATH'], 'CAPTURE':str(capture)}
            result = subprocess.run(
                ['bash', str(ROOT/'scripts/minilm_eval_after_pipeline.sh'), 'openalex', 'app-id', str(ROOT)],
                env=env, capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('MiniLM Eval started_at=', result.stdout)
            self.assertRegex(
                result.stdout,
                r'MiniLM Eval completed_at=.* status=success elapsed_seconds=\d+',
            )
            calls = [json.loads(line) for line in capture.read_text().splitlines()]
            copies = [call for call in calls if call and call[0] == 'cp']
            self.assertEqual([call[1] for call in copies], [
                'app-id:/app/paper_agents', 'app-id:/app/sql',
            ])
            run = next(call for call in calls if call and call[0] == 'run')
            self.assertIn('--network=none', run)
            self.assertIn('--read-only', run)
            self.assertIn('paper-agent-minilm-model-cache', ' '.join(run))
            self.assertNotIn(f'src={ROOT},dst=/workspace,readonly', run)
            self.assertEqual(run[-6:], [
                '-m', 'paper_agents.minilm_eval', '--db', '/app/data/paper_agent.db',
                '--source', 'openalex',
            ])

    def test_minilm_eval_runner_preserves_container_failure_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root/'docker'
            executable.write_text('#!'+sys.executable+'\nimport sys\n'
                'if sys.argv[1:3] == ["image", "inspect"]:\n'
                ' print("sha256:7d8b960220e3c6f60292e6d40a8f300ff19c5ee05cd97cf5f725a76673e2d5c2")\n'
                'elif sys.argv[1] == "run": raise SystemExit(23)\n')
            executable.chmod(0o700)
            env = {**os.environ, 'PATH':str(root)+':'+os.environ['PATH']}
            result = subprocess.run(
                ['bash', str(ROOT/'scripts/minilm_eval_after_pipeline.sh'), 'openalex', 'app-id', str(ROOT)],
                env=env, capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 23)
            self.assertRegex(
                result.stdout,
                r'MiniLM Eval completed_at=.* status=failed elapsed_seconds=\d+',
            )

    def test_minilm_shadow_runner_is_bounded_and_calls_shadow_module(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root/'docker'
            executable.write_text('#!'+sys.executable+'\nimport json,os,sys\n'
                'args=sys.argv[1:]\n'
                'with open(os.environ["CAPTURE"],"a") as stream: stream.write(json.dumps(args)+"\\n")\n'
                'if args[:2] == ["image", "inspect"]: print("sha256:7d8b960220e3c6f60292e6d40a8f300ff19c5ee05cd97cf5f725a76673e2d5c2")\n'
                'elif args and args[0] == "inspect" and "NetworkSettings.Networks" in " ".join(args): print("production-network")\n'
                'elif args and args[0] == "inspect" and ".Config.Env" in " ".join(args): print("PAPER_AGENT_OLLAMA_URL=http://ollama:11434/api/generate")\n')
            executable.chmod(0o700)
            capture = root/'calls'
            env = {**os.environ, 'PATH':str(root)+':'+os.environ['PATH'], 'CAPTURE':str(capture)}
            result = subprocess.run(
                ['bash', str(ROOT/'scripts/minilm_shadow_after_pipeline.sh'), 'arxiv', 'app-id', str(ROOT)],
                env=env, capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('MiniLM Shadow started_at=', result.stdout)
            calls = [json.loads(line) for line in capture.read_text().splitlines()]
            run = next(call for call in calls if call and call[0] == 'run')
            self.assertIn('production-network', run)
            self.assertIn('--read-only', run)
            self.assertIn('--cpus=2', run)
            self.assertIn('PAPER_AGENT_OLLAMA_URL=http://ollama:11434/api/generate', run)
            self.assertEqual(run[-10:], [
                '-m', 'paper_agents.minilm_shadow', '--db', '/app/data/paper_agent.db',
                '--source', 'arxiv', '--input-limit', '10', '--output-limit', '3',
            ])

    def test_fixed_commands_enter_owned_job_without_sourcing_env(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root/'docker'
            executable.write_text('#!'+sys.executable+'\nimport json,os,sys\n'
                'with open(os.environ["CAPTURE"],"a") as stream: stream.write(json.dumps(sys.argv[1:])+"\\n")\n'
)
            executable.chmod(0o700)
            capture = root/'calls'
            envfile = root/'private config.env'
            envfile.write_text('DO_NOT_SOURCE=$(touch '+str(root/'BAD')+')\n')
            env = {**os.environ, 'PATH':str(root)+':'+os.environ['PATH'],
                   'PAPER_MIGRATION_ENV_FILE':str(envfile), 'CAPTURE':str(capture)}
            for job, script in [('arxiv','nightly_pipeline.sh'),('openalex','openalex_pipeline.sh'),
                                ('semantic','semantic_scholar_pipeline.sh'),
                                ('core','core_pipeline.sh'),
                                ('profile','biweekly_profile_rebuild_compare.sh'),('backup','backup_db.sh')]:
                capture.unlink(missing_ok=True)
                result = subprocess.run(['bash', str(ROOT/'scripts/mini_container_job.sh'), job], env=env,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = [json.loads(line) for line in capture.read_text().splitlines()]
                self.assertEqual(len(calls), 1)
                args = calls[-1]
                self.assertEqual(args[0:3], ['compose','--env-file',str(envfile)])
                self.assertEqual(args[5:8], ['exec','-T','app'])
                self.assertEqual(args[-4:], ['python','-m','paper_agents.migration_job',job])
                expected = ['bash','/app/scripts/'+script]
                if job=='backup': expected += ['/app/data/paper_agent.db','/backups']
                self.assertEqual(migration_job.command(job,Path('/app')), expected)
                self.assertIn('PAPER_AGENT_SELF_UPDATE=0', args)
                self.assertIn('PAPER_AGENT_TOPIC_SLOT=0', args)
            self.assertFalse((root/'BAD').exists())
            self.assertIn(
                'core) eval_source=core',
                (ROOT/'scripts/mini_container_job.sh').read_text(),
            )
            for arguments in (['unknown'], ['arxiv','extra']):
                result = subprocess.run(['bash',str(ROOT/'scripts/mini_container_job.sh'),*arguments],env=env,capture_output=True,timeout=5)
                self.assertEqual(result.returncode,64)

    def test_extra_compose_files_are_absolute_and_not_sourced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root/'docker'
            executable.write_text('#!'+sys.executable+'\nimport json,os,sys\n'
                'with open(os.environ["CAPTURE"],"a") as stream: stream.write(json.dumps(sys.argv[1:])+"\\n")\n')
            executable.chmod(0o700)
            capture = root/'calls'
            envfile = root/'private.env'; envfile.write_text('x=y\n')
            extra_one = root/'local.yml'; extra_one.write_text('services: {}\n')
            extra_two = root/'prod.yml'; extra_two.write_text('services: {}\n')
            env = {**os.environ, 'PATH':str(root)+':'+os.environ['PATH'],
                   'PAPER_MIGRATION_ENV_FILE':str(envfile),
                   'PAPER_MIGRATION_EXTRA_COMPOSE_FILES':str(extra_one)+':'+str(extra_two),
                   'CAPTURE':str(capture)}
            result = subprocess.run(['bash', str(ROOT/'scripts/mini_container_job.sh'), 'arxiv'],
                                    env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads(capture.read_text().splitlines()[-1])
            self.assertIn(str(extra_one), args)
            self.assertIn(str(extra_two), args)
            self.assertLess(args.index(str(extra_one)), args.index('exec'))
            self.assertLess(args.index(str(extra_two)), args.index('exec'))
            env['PAPER_MIGRATION_EXTRA_COMPOSE_FILES'] = 'relative.yml'
            result = subprocess.run(['bash', str(ROOT/'scripts/mini_container_job.sh'), 'arxiv'],
                                    env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 64)

    def test_deploy_lock_skips_job_before_container_exec(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root/'docker'
            executable.write_text('#!'+sys.executable+'\nraise SystemExit("docker should not run")\n')
            executable.chmod(0o700)
            envfile = root/'private.env'; envfile.write_text('x=y\n')
            lock = root/'deploy.lock'
            held = lock.open('w')
            try:
                import fcntl
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                env = {**os.environ, 'PATH':str(root)+':'+os.environ['PATH'],
                       'PAPER_MIGRATION_ENV_FILE':str(envfile),
                       'PAPER_MIGRATION_DEPLOY_LOCK_FILE':str(lock)}
                result = subprocess.run(['bash', str(ROOT/'scripts/mini_container_job.sh'), 'arxiv'],
                                        env=env, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('deployment is in progress', result.stdout)
            finally:
                held.close()


class JobLeaseTests(unittest.TestCase):
    def test_job_event_default_follows_runtime_repo(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {'PAPER_AGENT_REPO': directory}, clear=False,
        ):
            os.environ.pop('PAPER_AGENT_JOB_EVENT_FILE', None)
            self.assertEqual(
                job_events.event_path(),
                Path(directory) / 'data' / 'job-events.jsonl',
            )

    def test_overlapping_scheduled_job_skips_and_records_health_event(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = root / 'job-events.jsonl'
            env = {
                'PAPER_AGENT_STARTUP_MODE': 'imported',
                'PAPER_AGENT_LIFECYCLE_DIR': directory,
                'PAPER_AGENT_JOB_EVENT_FILE': str(events),
            }
            with patch.dict(os.environ, env), patch.object(subprocess, 'Popen') as launch, \
                    migration_lifecycle.lease(root, exclusive=True, name='.scheduled-job.lock'):
                self.assertEqual(migration_job.run('arxiv'), 0)
            launch.assert_not_called()
            recorded = job_events.recent(events)
            self.assertEqual(recorded[0]['status'], 'skipped_busy')
            self.assertEqual(recorded[0]['job'], 'arxiv')
            self.assertIn('another Project Paper job', recorded[0]['message'])

    def test_every_job_fails_busy_before_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'PAPER_AGENT_STARTUP_MODE':'imported',
                    'PAPER_AGENT_LIFECYCLE_DIR':directory}), patch.object(subprocess,'Popen') as launch:
                with migration_lifecycle.lease(Path(directory),exclusive=True):
                    for job in migration_job.SCRIPTS:
                        with self.subTest(job=job), self.assertRaisesRegex(RuntimeError,'busy'):
                            migration_job.run(job)
                launch.assert_not_called()

    def test_profile_preflight_is_inside_owned_watcher(self):
        with tempfile.TemporaryDirectory() as directory:
            with migration_lifecycle.lease(Path(directory)) as descriptor, \
                    patch.dict(os.environ, {'PAPER_AGENT_LIFECYCLE_DIR':directory}), \
                    patch('paper_agents.gemini_runtime.enabled',return_value=True), \
                    patch('paper_agents.gemini_runtime.check_ready',side_effect=RuntimeError('missing key')), \
                    patch.object(migration_job,'supervise') as supervise:
                with self.assertRaisesRegex(RuntimeError,'missing key'):
                    migration_job.watch('profile',None,descriptor,10)
                supervise.assert_not_called()

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux job watchdog regression; pending Linux rehearsal')
    def test_job_parent_loss_keeps_initialization_excluded_through_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'scripts').mkdir()
            output = root/'pid'
            fake = root/'fake.py'
            fake.write_text("import os,signal,time\nfrom pathlib import Path\n"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                "Path(os.environ['JOB_PID']).write_text(str(os.getpid()))\n"
                "time.sleep(60)\n")
            for name in migration_job.SCRIPTS.values():
                (root/'scripts'/name).write_text('exec '+sys.executable+' '+str(fake)+'\n')
            env = {**os.environ, 'PYTHONPATH':str(ROOT),'PAPER_AGENT_STARTUP_MODE':'imported',
                   'PAPER_AGENT_LIFECYCLE_DIR':str(root),'PAPER_AGENT_REPO':str(root),
                   'PAPER_AGENT_JOB_TIMEOUT':'10','JOB_PID':str(output)}
            process = subprocess.Popen([sys.executable,'-m','paper_agents.migration_job','arxiv'],
                                       env=env,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                deadline=time.monotonic()+5
                while not output.exists() and time.monotonic()<deadline: time.sleep(.01)
                self.assertTrue(output.exists())
                with self.assertRaisesRegex(RuntimeError,'busy'):
                    with migration_lifecycle.lease(root,exclusive=True): pass
                process.kill()
                process.wait(timeout=3)
                # The independent watcher, not the dead cron caller or app, owns this lease.
                with self.assertRaisesRegex(RuntimeError,'busy'):
                    with migration_lifecycle.lease(root,exclusive=True): pass
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    try:
                        with migration_lifecycle.lease(root,exclusive=True): break
                    except RuntimeError: time.sleep(.01)
                else: self.fail('watcher did not finish cleanup')
                self.assertFalse((Path('/proc')/output.read_text()).exists())
                process.communicate(timeout=3)
            finally:
                if process.poll() is None: process.kill(); process.communicate(timeout=5)


if __name__=='__main__': unittest.main()

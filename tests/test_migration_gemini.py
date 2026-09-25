"""Synthetic credentials and Python stand-ins only; no installed Gemini needed."""
from contextlib import contextmanager
import json
import hashlib
import shutil
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

from paper_agents import gemini_runtime as runtime, gemini_guardian, gemini_process
from paper_agents import migration_lifecycle, package_runtime, web
from paper_agents import feedback, db

ROOT = Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def test_private_file_bounds_aliases_and_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'key'
            for value, mode, valid in [(b'synthetic-key\n', 0o400, True),
                    (b'', 0o600, False), (b'synthetic-key', 0o644, False),
                    (b'x'*4097, 0o600, False), (b'line\nbreak', 0o600, False),
                    (b'\xff', 0o600, False)]:
                path.chmod(0o600) if path.exists() else None
                path.write_bytes(value); path.chmod(mode)
                if valid:
                    self.assertEqual(runtime.read_key(path), 'synthetic-key')
                else:
                    with self.assertRaises(RuntimeError) as raised:
                        runtime.read_key(path)
                    self.assertNotIn('synthetic-key', str(raised.exception))
                    self.assertNotIn(str(path), str(raised.exception))
            path.chmod(0o600); path.write_text('synthetic-key')
            link = Path(directory)/'link'; link.symlink_to(path)
            with self.assertRaises(RuntimeError): runtime.read_key(link)
            link.unlink(); link.hardlink_to(path)
            with self.assertRaises(RuntimeError): runtime.read_key(path)
            fifo = Path(directory)/'fifo'; os.mkfifo(fifo, 0o600)
            with self.assertRaises(RuntimeError): runtime.read_key(fifo)

    def test_child_environment_drops_host_configuration_and_auth(self):
        with patch.dict(os.environ, {'GEMINI_API_KEY':'host-key', 'NODE_OPTIONS':'host-hook',
                'GOOGLE_GENAI_USE_VERTEXAI':'1', 'GEMINI_MODEL':'host-model'}):
            result = gemini_guardian.child_environment(Path('/private/home'), 'approved-model', 'synthetic-key')
            self.assertEqual(result['GEMINI_API_KEY'], 'synthetic-key')
            self.assertEqual(os.environ['GEMINI_API_KEY'], 'host-key')
            self.assertEqual(result['GEMINI_MODEL'], 'approved-model')
            self.assertNotIn('NODE_OPTIONS', result)
            self.assertNotIn('GOOGLE_GENAI_USE_VERTEXAI', result)
            self.assertEqual(result['GEMINI_CLI_HOME'], '/private/home')
            self.assertEqual(result['GEMINI_CLI_TRUST_WORKSPACE'], 'true')

    def test_unresolved_policy_and_native_launch(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'policy unresolved'): runtime.model_policy()
            with runtime.launch(['gemini', '-p', 'prompt'], 180) as (command, options, grace):
                self.assertEqual(command, ['gemini', '-p', 'prompt'])
                self.assertEqual(options, {})
                self.assertEqual(grace, 1)

    def test_missing_key_preserves_raw_feedback_without_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT/'sql', root/'sql')
            with patch.dict(os.environ, {}, clear=True): package_runtime.initialize(root, ROOT/'deploy/templates')
            database = root/'data/paper_agent.db'
            with sqlite3.connect(database) as connection:
                connection.execute("INSERT INTO papers(id,canonical_key,title) VALUES(1,'test:1','Paper')")
            text = 'Score: 4.5/5\nUseful operational evidence.'
            with patch.object(web, 'gemini_enabled', return_value=True), \
                    patch.object(runtime, 'enabled', return_value=True), \
                    patch.object(runtime, 'check_ready', side_effect=RuntimeError('Gemini credential unavailable')), \
                    patch.object(web, 'start_profile_apply_worker') as worker:
                result = web.save_feedback(database, paper_id=1, status='reviewed', notes=text,
                                           feedback_content=text, profile_apply_mode='background')
            self.assertTrue(result['feedback_saved'])
            self.assertFalse(result['profile_apply_queued'])
            self.assertIn('credential', result['profile_apply_error'])
            worker.assert_not_called()
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute('SELECT content FROM raw_feedback').fetchone()[0], text)

    def test_provider_failure_preserves_feedback_and_retry_applies_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT/'sql', root/'sql')
            with patch.dict(os.environ, {}, clear=True):
                package_runtime.initialize(root, ROOT/'deploy/templates')
            database = root/'data/paper_agent.db'
            with sqlite3.connect(database) as connection:
                connection.execute("INSERT INTO papers(id,canonical_key,title) VALUES(1,'test:1','Paper')")
            content = 'Score: 5/5\nRelevant production evidence.'
            with patch.object(web, 'gemini_enabled', return_value=True), \
                    patch.object(runtime, 'readiness_error', return_value=None), \
                    patch.object(feedback, 'run_gemini', side_effect=RuntimeError('Gemini provider unavailable; output withheld')):
                result = web.save_feedback(database, paper_id=1, status='reviewed', notes=content,
                                           feedback_content=content)
            self.assertTrue(result['feedback_saved'])
            self.assertIn('unavailable', result['profile_apply_error'])
            calls = []
            def provider(payload, model):
                calls.append(model)
                return {'profile': {'interests':['production'], 'positive_signals':[],
                                    'negative_signals':[], 'notes':'synthetic'},
                        'change_summary':'Synthetic retry'}
            with db.connect_db(database) as connection:
                self.assertEqual(connection.execute('SELECT content FROM raw_feedback').fetchone()[0], content)
                self.assertEqual(connection.execute('SELECT count(*) FROM feedback_profile_applications').fetchone()[0], 0)
                feedback.apply_feedback_to_profile(connection, dry_run=False, provider_fn=provider)
                feedback.apply_feedback_to_profile(connection, dry_run=False, provider_fn=provider)
                self.assertEqual(connection.execute('SELECT count(*) FROM feedback_profile_applications').fetchone()[0], 1)
            self.assertEqual(len(calls), 1)

    def test_application_default_preserves_explicit_model_override(self):
        with patch.object(feedback, 'run_gemini', return_value='{}') as run:
            feedback.call_gemini_json({}, None)
            self.assertNotIn('--model', run.call_args.args[0])
            feedback.call_gemini_json({}, 'synthetic-explicit')
            self.assertEqual(run.call_args.args[0][1:3], ['--model', 'synthetic-explicit'])

    def test_timeout_rejects_unbounded_values(self):
        for timeout in (float('nan'), float('inf'), 0, -1, 7201):
            with self.assertRaises(RuntimeError): runtime.check_timeout(timeout)

    def test_linux_reaping_kills_adopted_children_and_retains_lease_on_delay(self):
        with patch.object(gemini_guardian.sys, 'platform', 'linux'), patch('ctypes.CDLL') as libc:
            libc.return_value.prctl.return_value = 0
            self.assertTrue(gemini_guardian.enable_subreaper())
            libc.return_value.prctl.return_value = -1
            with self.assertRaises(RuntimeError): gemini_guardian.enable_subreaper()
        with patch.object(gemini_guardian.sys, 'platform', 'darwin'):
            with self.assertRaisesRegex(RuntimeError, 'requires Linux'): gemini_guardian.enable_subreaper()
        with patch.object(os, 'waitpid', side_effect=[(0,0),(0,0),(123,0),ChildProcessError()]), \
                patch.object(Path, 'read_text', return_value='123 456'), \
                patch.object(os, 'kill') as kill, patch.object(time, 'sleep'), \
                patch.object(time, 'monotonic', side_effect=[0,2,3]):
            gemini_guardian.reap_descendants()
            self.assertEqual(kill.call_count,4)
            self.assertEqual(kill.call_args.args,(456, signal.SIGKILL))

    def test_cleanup_retains_ownership_when_proc_and_log_reader_are_unavailable(self):
        with patch.object(os, 'waitpid', side_effect=[(0,0),(0,0),ChildProcessError()]) as wait, \
                patch.object(Path, 'read_text', side_effect=PermissionError()), \
                patch('builtins.print', side_effect=BrokenPipeError()), \
                patch.object(time, 'sleep'), \
                patch.object(time, 'monotonic', side_effect=[0,2,3]):
            gemini_guardian.reap_descendants()
            self.assertEqual(wait.call_count,3)

    def test_parent_cleanup_never_kills_guardian_that_still_owns_lease(self):
        process = Mock(paper_cleanup_grace=3, stdout=None, stderr=None)
        process.wait.side_effect = subprocess.TimeoutExpired('synthetic guardian',3)
        with patch.object(gemini_process,'_signal_process') as send:
            with self.assertRaisesRegex(RuntimeError,'lease retained'):
                gemini_process._cleanup(process)
            send.assert_called_once_with(process,False)

    def test_runtime_inventory_checks_actual_bytes_without_running_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            node, cli, inventory, key = [root/name for name in ('node','cli','runtime.json','key')]
            node.write_bytes(b'synthetic-node'); node.chmod(0o700)
            cli.write_bytes(b'synthetic-cli')
            key.write_text('synthetic-key'); key.chmod(0o600)
            inventory.write_text(json.dumps({'node_version':'v22.23.2','cli_version':'0.52.0',
                'node_sha256':hashlib.sha256(node.read_bytes()).hexdigest(),
                'cli_sha256':hashlib.sha256(cli.read_bytes()).hexdigest()}))
            with patch.multiple(runtime, NODE=node, CLI=cli, INVENTORY=inventory, SECRET=key), \
                    patch.dict(os.environ, {'PAPER_AGENT_GEMINI_DEFAULT_MODEL':'synthetic-approved'}), \
                    patch.object(subprocess, 'Popen') as popen:
                runtime.check_ready()
                popen.assert_not_called()
                cli.write_bytes(b'modified-cli')
                with self.assertRaisesRegex(RuntimeError, 'runtime unavailable'): runtime.check_ready()
            with patch.object(os, 'geteuid', return_value=os.geteuid()+1):
                with self.assertRaises(RuntimeError): runtime.read_key(key)


FAKE_CLI = '''import json,os,signal,subprocess,sys,time
from pathlib import Path
output=Path(sys.argv[1]); mode=sys.argv[2]
child=subprocess.Popen([sys.executable,'-c', 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'],start_new_session=mode.startswith('detached'))
signal.signal(signal.SIGTERM,signal.SIG_IGN)
output.write_text(json.dumps({'parent':os.getpid(),'child':child.pid,'home':os.environ['HOME'],
 'auth':os.environ.get('GEMINI_API_KEY'),'host_hook':os.environ.get('NODE_OPTIONS')}))
if mode in ('success','detached-success'):
 for event in [{'type':'init'},{'type':'message','role':'assistant','delta':True,'content':'{"ok":true}'},{'type':'result','status':'success'}]:
  print(json.dumps(event),flush=True)
time.sleep(60)
'''

GUARDIAN = '''import sys
from paper_agents import gemini_runtime as r, gemini_guardian as g
from pathlib import Path
r.NODE=Path(sys.executable);r.CLI=Path(sys.argv[1]);r.check_ready=lambda:None
r.read_key=lambda:'synthetic-key'
raise SystemExit(g.run(int(sys.argv[2]),int(sys.argv[3]),int(sys.argv[4]),float(sys.argv[5]),sys.argv[6:]))
'''

DRIVER = '''import sys,threading,time
from contextlib import contextmanager
from paper_agents import gemini_runtime as r, gemini_process as p
original=r.launch;r.check_ready=lambda:None
@contextmanager
def launch(command,timeout):
 with original(command,timeout) as (args,options,grace):
  yield [sys.executable,sys.argv[1],sys.argv[2],*args[3:]],options,grace
r.launch=launch
def call():
 try: print(p.run_gemini(['gemini',sys.argv[3],sys.argv[4]],float(sys.argv[5]),stream_json=True),flush=True)
 except RuntimeError as error: print(str(error),flush=True)
if sys.argv[6]=='thread':
 threading.Thread(target=call).start()
 while True: time.sleep(.1)
else: call()
'''


@unittest.skipUnless(sys.platform.startswith('linux'), 'Migration guardian requires Linux; run synthetic process regressions during Linux rehearsal')
class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name, code in [('fake.py', FAKE_CLI), ('guardian.py', GUARDIAN), ('driver.py', DRIVER)]:
            (self.root/name).write_text(code)
        self.output = self.root/'pids.json'
        self.env = {**os.environ, 'PYTHONPATH':str(ROOT), 'PAPER_AGENT_STARTUP_MODE':'imported',
                    'PAPER_AGENT_GEMINI_ENABLED':'1', 'PAPER_AGENT_LIFECYCLE_DIR':str(self.root),
                    'PAPER_AGENT_GEMINI_DEFAULT_MODEL':'synthetic-approved', 'NODE_OPTIONS':'host-hook'}

    def start(self, mode='hang', budget='10', thread='main'):
        process = subprocess.Popen([sys.executable, str(self.root/'driver.py'),
            str(self.root/'guardian.py'), str(self.root/'fake.py'), str(self.output), mode, budget, thread],
            cwd=ROOT, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (process.kill(), process.communicate()) if process.poll() is None else None)
        deadline = time.monotonic()+5
        while not self.output.exists() and time.monotonic()<deadline:
            if process.poll() is not None: self.fail(process.communicate())
            time.sleep(.02)
        self.assertTrue(self.output.exists())
        self.pids = json.loads(self.output.read_text())
        return process

    def assert_clean(self):
        deadline = time.monotonic()+5
        while time.monotonic()<deadline:
            try:
                with migration_lifecycle.lease(self.root, exclusive=True): break
            except RuntimeError: time.sleep(.05)
        else: self.fail('Runtime lease not released after cleanup')
        for key in ('parent', 'child'):
            self.assertFalse((Path('/proc')/str(self.pids[key])).exists())
        self.assertFalse(Path(self.pids['home']).exists())

    def test_completion_cleans_term_resistant_descendants(self):
        process = self.start('success')
        stdout, stderr = process.communicate(timeout=8)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stdout.strip(), '{"ok":true}')
        self.assertEqual(self.pids['auth'], 'synthetic-key')
        self.assertIsNone(self.pids['host_hook'])
        self.assert_clean()

    def test_timeout_and_main_thread_interrupt_cleanup(self):
        for interruption in (None, signal.SIGTERM, signal.SIGINT):
            with self.subTest(interruption=interruption):
                self.output.unlink(missing_ok=True)
                process = self.start(budget='0.5' if interruption is None else '10')
                with self.assertRaisesRegex(RuntimeError, 'busy'):
                    with migration_lifecycle.lease(self.root, exclusive=True): pass
                if interruption is not None: process.send_signal(interruption)
                stdout, _ = process.communicate(timeout=8)
                if interruption is None: self.assertTrue('timed out' in stdout or 'exit 124' in stdout)
                self.assert_clean()

    def test_detached_descendant_timeout_completion_and_parent_loss(self):
        for mode, thread, kill_parent in [('detached','main',False),
                ('detached-success','main',False),('detached','thread',True)]:
            with self.subTest(mode=mode, thread=thread):
                self.output.unlink(missing_ok=True)
                process = self.start(mode=mode, budget='0.5' if mode=='detached' else '10', thread=thread)
                with self.assertRaisesRegex(RuntimeError, 'busy'):
                    with migration_lifecycle.lease(self.root, exclusive=True): pass
                if kill_parent: process.kill()
                process.communicate(timeout=8)
                self.assert_clean()

    def test_web_thread_parent_sigkill_does_not_orphan_cli(self):
        process = self.start(thread='thread')
        process.kill(); process.communicate(timeout=5)
        self.assert_clean()


if __name__ == '__main__': unittest.main()

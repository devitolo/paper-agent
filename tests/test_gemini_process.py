import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from paper_agents import db
from paper_agents.feedback import call_gemini_json, apply_feedback_to_profile, ingest_feedback_blob
from paper_agents.gemini_process import run_gemini

FIXTURE = r'''
import json, os, signal, subprocess, sys, time
from pathlib import Path
mode, pidfile = sys.argv[1:]
framed = mode.startswith('stream_')
if framed:
    mode = mode[len('stream_'):]
    print(json.dumps({'type':'init','session_id':'fixture','model':'test'}), flush=True)
def answer(value, complete):
    if framed:
        print(json.dumps({'type':'message','role':'assistant','delta':True,'content':value}), flush=True)
        if complete:
            print(json.dumps({'type':'result','status':'success'}), flush=True)
    else:
        print(value, flush=True)
if mode in ('child_pipes', 'resistant', 'closed_pipes'):
    child = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    kwargs = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL} if mode == 'closed_pipes' else {}
    p = subprocess.Popen([sys.executable, '-c', child], **kwargs)
    Path(pidfile).write_text(str(p.pid))
    time.sleep(.15)
    if mode in ('child_pipes', 'closed_pipes'):
        answer('{"ok": true}', True)
        sys.exit(0)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if mode == 'success':
    assert sys.stdin.read() == ''
    answer('{"ok": true}', True)
elif mode == 'invalid':
    print('not json PRIVATE_PROFILE')
elif mode == 'nonzero':
    print('PRIVATE_PROFILE secret-token', file=sys.stderr)
    sys.exit(2)
elif mode == 'quota':
    print('429 quota PRIVATE_PROFILE', file=sys.stderr)
    sys.exit(3)
else:
    answer('{"profile":{"interests":["PRIVATE_PROFILE"]},"change_summary":"ok"}', False)
    print('PRIVATE_REVIEW secret-token', file=sys.stderr, flush=True)
    time.sleep(60)
'''


@unittest.skipUnless(os.name == 'posix', 'POSIX process-group lifecycle fixtures')
class GeminiProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / 'fixture.py'
        self.script.write_text(FIXTURE)
        self.pidfile = self.root / 'child.pid'
        self.addCleanup(self.kill_fixture_child)

    def kill_fixture_child(self):
        if self.pidfile.exists():
            try:
                os.kill(int(self.pidfile.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass

    def command(self, mode):
        return [sys.executable, str(self.script), mode, str(self.pidfile)]

    def assert_child_stopped(self):
        pid = int(self.pidfile.read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            inspection = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True)
            self.assertIn(inspection.returncode, (0, 1), 'ps inspection failed')
            self.assertFalse(inspection.stderr.strip(), 'ps inspection denied or failed')
            status = inspection.stdout.strip()
            if not status or status.startswith('Z'):
                return
            time.sleep(.02)
        self.fail(f'Fixture child {pid} still running: {status}')

    def call_fixture(self, mode):
        with patch('paper_agents.feedback.run_gemini', side_effect=lambda command, timeout, **kwargs: run_gemini(self.command(mode), 1, stream_json=mode.startswith('stream_'))):
            return call_gemini_json({'current_profile': {'notes': 'PRIVATE_PROFILE'}})

    def test_cleanup_error_preserves_timeout_and_attempts_kill_after_term_error(self):
        from paper_agents.gemini_process import _cleanup
        process = Mock()
        with patch('paper_agents.gemini_process._signal_process', side_effect=[PermissionError(), None]) as signals, patch('paper_agents.gemini_process.time.sleep'):
            _cleanup(process)
        self.assertEqual([call.args[1] for call in signals.call_args_list], [False, True])
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()
        process = Mock()
        process.communicate.side_effect = subprocess.TimeoutExpired(['private-prompt'], 1, output=b'PRIVATE_PROFILE')
        with patch('paper_agents.gemini_process.subprocess.Popen', return_value=process), patch('paper_agents.gemini_process._cleanup', side_effect=PermissionError()):
            with self.assertRaisesRegex(RuntimeError, 'timed out.*cleanup incomplete') as caught:
                run_gemini(['private-prompt'], 1)
        self.assertNotIn('PRIVATE_PROFILE', str(caught.exception))
        self.assertNotIn('private-prompt', str(caught.exception))

    def test_success_closes_stdin_and_parses_json(self):
        self.assertEqual(self.call_fixture('success'), {'ok': True})

    def test_nonzero_and_invalid_json_do_not_leak_output(self):
        for mode in ('nonzero', 'invalid', 'quota'):
            with self.subTest(mode=mode), self.assertRaises(RuntimeError) as caught:
                self.call_fixture(mode)
            message = str(caught.exception)
            self.assertNotIn('PRIVATE_PROFILE', message)
            self.assertNotIn('secret-token', message)
            self.assertLess(len(message), 350)
            if mode == 'quota':
                self.assertIn('provider_category=quota', message)

    def test_answer_then_hang_is_failure_with_sanitized_partial_counts(self):
        start = time.monotonic()
        with self.assertRaises(RuntimeError) as caught:
            self.call_fixture('hang')
        self.assertLess(time.monotonic() - start, 5)
        message = str(caught.exception)
        self.assertIn('timed out', message)
        self.assertRegex(message, r'stdout_bytes=[1-9]')
        self.assertRegex(message, r'stderr_bytes=[1-9]')
        self.assertNotIn('PRIVATE', message)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_descendants_cleaned_when_parent_exits_or_ignores_term(self):
        for mode in ('child_pipes', 'resistant', 'closed_pipes', 'stream_child_pipes', 'stream_resistant', 'stream_closed_pipes'):
            with self.subTest(mode=mode):
                start = time.monotonic()
                if mode in ('closed_pipes', 'stream_child_pipes', 'stream_closed_pipes'):
                    self.assertEqual(self.call_fixture(mode), {'ok': True})
                else:
                    with self.assertRaisesRegex(RuntimeError, 'timed out'):
                        self.call_fixture(mode)
                self.assertLess(time.monotonic() - start, 5)
                self.assert_child_stopped()

    def test_term_and_keyboard_interrupt_cleanup(self):
        for sig, framed in ((signal.SIGTERM, False), (signal.SIGINT, False), (signal.SIGTERM, True), (signal.SIGINT, True)):
            with self.subTest(signal=sig, framed=framed):
                self.pidfile.unlink(missing_ok=True)
                driver = subprocess.Popen([sys.executable, '-c',
                    'import sys,signal; signal.signal(signal.SIGINT, signal.default_int_handler); from paper_agents.gemini_process import run_gemini; run_gemini(sys.argv[1:], 60, stream_json=' + repr(framed) + ')',
                    *self.command('stream_resistant' if framed else 'resistant')], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 5
                    while not self.pidfile.exists() and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertTrue(self.pidfile.exists())
                    time.sleep(.2)
                    driver.send_signal(sig)
                    time.sleep(.2)
                    driver.send_signal(sig)
                    driver.send_signal(signal.SIGINT if sig == signal.SIGTERM else signal.SIGTERM)
                    driver.wait(timeout=5)
                    self.assertNotEqual(driver.returncode, 0)
                    stderr = driver.stderr.read().decode()
                    try:
                        self.assert_child_stopped()
                    except AssertionError as error:
                        raise AssertionError(str(error) + " driver stderr: " + stderr) from None
                finally:
                    if driver.poll() is None:
                        driver.kill()
                    driver.wait()
                    driver.stderr.close()

    def test_timeout_preserves_active_profile_and_pending_feedback(self):
        path = self.root / 'state.db'
        db.init_db(path)
        con = db.connect_db(path)
        self.addCleanup(con.close)
        original = db.create_profile_version(con, {'interests': ['original']})
        paper_id, _ = db.upsert_paper(con, {'source': 'test', 'source_id': 'fixture', 'title': 'Fixture'})
        ingest_feedback_blob(con, paper_id=paper_id, recommendation_id=None, content='Decision: keep\nScore: 5\nUseful implementation.', source='test')
        before = db.unapplied_structured_feedback(con)
        with patch('paper_agents.feedback.run_gemini', side_effect=lambda command, timeout, **kwargs: run_gemini(self.command('hang'), 1)):
            result = apply_feedback_to_profile(con, model='explicit-test', dry_run=False)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(db.current_profile_version(con)['id'], original)
        self.assertEqual(db.unapplied_structured_feedback(con), before)
        self.assertEqual(con.execute('SELECT COUNT(*) FROM profile_versions').fetchone()[0], 1)
        error = con.execute('SELECT error FROM feedback_profile_apply_attempts').fetchone()[0]
        self.assertIn('timed out', error)
        self.assertNotIn('PRIVATE', error)


if __name__ == '__main__':
    unittest.main()

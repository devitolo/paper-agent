import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_agents.feedback import parse_json_object
from paper_agents.gemini_diagnostic import diagnose
from paper_agents.gemini_process import run_gemini


FIXTURE = r'''
import json, os, sys, time
mode = sys.argv[1]
def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)
if mode == 'startup_quota':
    emit({'type':'result', 'status':'error', 'error':{'message':'429 PRIVATE'}})
    sys.exit(1)
emit({'type':'init', 'session_id':'fixture', 'model':'test'})
emit({'type':'message', 'role':'user', 'content':'PRIVATE prompt 429'})
if mode == 'partial':
    emit({'type':'message', 'role':'assistant', 'delta':True, 'content':'{"ok":'})
    sys.exit(0)
if mode == 'oversized':
    os.write(1, b'x' * 100000)
    time.sleep(60)
elif mode == 'split':
    data = (json.dumps({'type':'message', 'role':'assistant', 'delta':True,
                       'content':'{"ok":"café"}'}, ensure_ascii=False)+'\n').encode()
    for byte in data:
        os.write(1, bytes([byte]))
        time.sleep(.001)
else:
    emit({'type':'message', 'role':'assistant', 'delta':True, 'content':'{"ok":'})
    emit({'type':'message', 'role':'assistant', 'delta':True, 'content':'true}'})
if mode == 'answer_hang':
    time.sleep(60)
if mode == 'truncated':
    print('{"type":"result","status":"success"}', end='', flush=True)
    sys.exit(0)
if mode == 'tool':
    emit({'type':'tool_use', 'tool_name':'test'})
emit({'type':'result', 'status':'success', 'stats':{'total_tokens':4}})
if mode == 'terminal_hang':
    time.sleep(60)
elif mode == 'error_after':
    time.sleep(.05)
    emit({'type':'error', 'severity':'error', 'message':'PRIVATE'})
elif mode == 'nonzero_after':
    time.sleep(.05)
    sys.exit(2)
elif mode == 'duplicate':
    emit({'type':'result', 'status':'success'})
'''


@unittest.skipUnless(os.name == 'posix', 'POSIX stream fixtures')
class GeminiStreamTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.script = Path(temp.name) / 'stream.py'
        self.script.write_text(FIXTURE)

    def run_fixture(self, mode, metadata=None):
        return run_gemini([sys.executable, str(self.script), mode], 2,
                          stream_json=True, metadata=metadata)

    def test_success_requires_framing_and_assembles_split_utf8(self):
        self.assertEqual(parse_json_object(self.run_fixture('normal')), {'ok': True})
        self.assertEqual(parse_json_object(self.run_fixture('split')), {'ok': 'café'})

    def test_explicit_terminal_result_can_finish_before_process_exit(self):
        metadata = {}
        start = time.monotonic()
        self.assertEqual(parse_json_object(self.run_fixture('terminal_hang', metadata)), {'ok': True})
        self.assertLess(time.monotonic() - start, 4)
        self.assertTrue(metadata['completion_seen'])
        self.assertIsNone(metadata['exit_code_before_cleanup'])

    def test_incomplete_conflicting_and_tool_streams_fail(self):
        for mode in ('partial', 'truncated', 'answer_hang', 'error_after', 'nonzero_after', 'duplicate', 'tool'):
            with self.subTest(mode=mode), self.assertRaises(RuntimeError) as caught:
                self.run_fixture(mode)
            self.assertNotIn('PRIVATE', str(caught.exception))
            self.assertNotIn('provider_category=quota', str(caught.exception))

    def test_startup_quota_keeps_existing_fallback_signal(self):
        with self.assertRaisesRegex(RuntimeError, 'provider_category=quota'):
            self.run_fixture('startup_quota')

    def test_output_without_newlines_is_bounded(self):
        with patch('paper_agents.gemini_process.MAX_STREAM_BYTES', 4096):
            with self.assertRaisesRegex(RuntimeError, 'output limit'):
                self.run_fixture('oversized')

    def test_answer_parser_rejects_partial_or_surrounding_prose(self):
        for value in ('{"ok":true} trailing PRIVATE', 'PRIVATE {"ok":true}', '```json\n{"ok":true}', '{"ok":'):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                parse_json_object(value)
        self.assertEqual(parse_json_object('```json\n{"ok":true}\n```'), {'ok': True})

    def test_diagnostic_uses_fixed_prompt_and_hides_unexpected_output(self):
        with patch('paper_agents.gemini_diagnostic.run_gemini', side_effect=['1.2.3', '{"PRIVATE":true}']) as run:
            report = diagnose('test-model')
        self.assertEqual(report['status'], 'unexpected_answer')
        self.assertNotIn('PRIVATE', json.dumps(report))
        self.assertEqual(run.call_args.kwargs['stream_json'], True)
        self.assertIn('Return exactly {"ok":true}', run.call_args.args[0][-1])


if __name__ == '__main__':
    unittest.main()

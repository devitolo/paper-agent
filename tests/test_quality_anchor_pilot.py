import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from paper_agents.gemini_process import _provider_failure, run_gemini
from paper_agents.quality_anchor_pilot import execute, validate, DIMENSIONS


class QualityAnchorTests(unittest.TestCase):
    def test_503_is_classified_without_leaking_stderr(self):
        hint = _provider_failure(b'PRIVATE_TOKEN Attempt 1 failed with status 503. Retrying with backoff...')
        self.assertEqual(hint['http_status'], 503)
        self.assertEqual(hint['provider_category'], 'unavailable')
        self.assertTrue(hint['internal_retry_observed'])
        self.assertNotIn('PRIVATE', json.dumps(hint))

    def test_batch_stops_and_records_failure(self):
        calls = []
        def provider(*args, **kwargs):
            calls.append(args)
            kwargs['metadata'].update(provider_category='unavailable', http_status=503)
            raise RuntimeError('Gemini provider failure: unavailable; output withheld.')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            self.assertFalse(execute([('first', None, 'x'), ('second', None, 'y')], 'pinned', path, 30, provider))
            self.assertEqual(len(calls), 1)
            result = json.loads((path/'first-result.json').read_text())
            self.assertEqual(result['transport']['http_status'], 503)
            self.assertFalse((path/'second-result.json').exists())

    def test_fabricated_quote_and_bool_score_rejected(self):
        packet = {'passages': [{'id': 'p1', 'text': 'actual source'}]}
        value = {'dimensions': {k: {'status':'complete', 'score':2, 'rationale':'reason',
                 'citations':[{'passage_id':'p1','quote':'actual source'}]} for k in DIMENSIONS}}
        validate(value, packet)
        value['dimensions']['evidence']['score'] = True
        with self.assertRaises(ValueError): validate(value, packet)
        value['dimensions']['evidence']['score'] = 2
        value['dimensions']['evidence']['citations'][0]['quote'] = 'fabricated'
        with self.assertRaises(ValueError): validate(value, packet)

    def test_fail_fast_kills_term_resistant_process(self):
        code = "import signal,time,sys; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('status 503 Retrying PRIVATE',file=sys.stderr,flush=True); time.sleep(60)"
        metadata = {}; start = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, 'provider failure') as caught:
            run_gemini([sys.executable,'-c',code], 20, stream_json=True, metadata=metadata, fail_fast_provider_errors=True)
        self.assertLess(time.monotonic()-start, 6)
        self.assertNotIn('PRIVATE', str(caught.exception))
        self.assertEqual(metadata['http_status'], 503)

    def test_deadline_kills_term_resistant_process(self):
        code = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"
        start = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, 'timed out'):
            run_gemini([sys.executable,'-c',code], 1, stream_json=True, fail_fast_provider_errors=True)
        self.assertLess(time.monotonic()-start, 6)

    def test_existing_transport_default_does_not_fail_fast(self):
        code = "import json,sys; print('status 503 Retrying',file=sys.stderr,flush=True); print(json.dumps({'type':'init'})); print(json.dumps({'type':'message','role':'assistant','delta':True,'content':'OK'})); print(json.dumps({'type':'result','status':'success'}))"
        self.assertEqual(run_gemini([sys.executable,'-c',code], 3, stream_json=True), 'OK')

    def test_check_success_is_saved(self):
        def provider(*args, **kwargs):return 'OK'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            self.assertTrue(execute([('connectivity',None,'Reply OK')], 'pinned', path, 30, provider))
            result = json.loads((path/'connectivity-result.json').read_text())
            self.assertEqual(result['status'], 'connectivity_ok')

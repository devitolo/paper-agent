"""Provider diagnostics and deadline coverage retained from the retired pilot."""
import json
import sys
import time
import unittest

from paper_agents.gemini_process import _provider_failure, run_gemini


class GeminiProviderErrorTests(unittest.TestCase):
    def test_503_is_classified_without_leaking_stderr(self):
        hint = _provider_failure(b'PRIVATE_TOKEN Attempt 1 failed with status 503. Retrying with backoff...')
        self.assertEqual(hint['http_status'], 503)
        self.assertEqual(hint['provider_category'], 'unavailable')
        self.assertTrue(hint['internal_retry_observed'])
        self.assertNotIn('PRIVATE', json.dumps(hint))


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

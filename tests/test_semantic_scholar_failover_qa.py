"""Independent two-key contracts; synthetic keys, disposable DBs, fixture HTTP only."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from paper_agents import db, web
from paper_agents.scout import SemanticScholarSource
from paper_agents.scout_agent import ScoutAgent, ScoutConfig
from paper_agents.semantic_scholar_progress import SemanticScholarProgress

KEY1 = 'QA-first-key-not-a-real-credential'
KEY2 = 'QA-second-key-not-a-real-credential'


class Response(io.BytesIO):
    def __init__(self, value):
        super().__init__(json.dumps(value).encode())


def page(offset=20, rows=None, next_offset=None):
    return {'offset': offset, 'next': next_offset, 'data': rows or []}


def limited(after='7', reason='rate limited', body=None, extra_headers=None):
    headers = Message()
    headers['Retry-After'] = after
    for key, value in (extra_headers or {}).items():
        headers[key] = value
    return HTTPError('https://fixture.invalid/search', 429, reason, headers,
                     io.BytesIO(json.dumps(body or {'message': 'limited'}).encode()))


class FailoverFixture(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            'PAPER_AGENT_PACKAGED': '0', 'PAPER_AGENT_TELEMETRY': '0',
            'SEMANTIC_SCHOLAR_API_KEY1': '', 'SEMANTIC_SCHOLAR_API_KEY2': '',
            'SEMANTIC_SCHOLAR_API_KEY': '',
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def source(self, **kwargs):
        return SemanticScholarSource(api_keys=(KEY1, KEY2), request_delay=2,
                                     retries=kwargs.pop('retries', 4), verbose=False, **kwargs)

    def run_script(self, source, outcomes, call=None):
        requests = []
        iterator = iter(outcomes)

        def transport(request, timeout):
            requests.append({'key': request.get_header('X-api-key'), 'url': request.full_url})
            outcome = next(iterator)
            if isinstance(outcome, BaseException):
                raise outcome
            return Response(outcome)

        with patch('paper_agents.scout.urllib.request.urlopen', side_effect=transport), \
                patch('paper_agents.scout.time.sleep') as sleep:
            result = (call or (lambda: self.fetch(source)) )()
        return result, requests, [c.args[0] for c in sleep.call_args_list]

    def fetch(self, source, mode='page', offset=20):
        if mode == 'topic':
            return source.fetch(['incident'], max_results=2, freshness_months=24)
        return source.fetch_page('incident', offset=offset, cutoff='2025-01-01', page_size=10)


class FailoverQA(FailoverFixture):
    def test_both_paths_send_key2_after_exactly_two_key1_429s(self):
        for mode in ('page', 'topic'):
            with self.subTest(mode=mode):
                source = self.source()
                _, requests, _ = self.run_script(source, [limited(), limited(), page()],
                                                lambda: self.fetch(source, mode))
                self.assertEqual([r['key'] for r in requests], [KEY1, KEY1, KEY2])
                self.assertEqual(source.diagnostics()['failover_count'], 1)

    def test_one_429_then_success_stays_on_key1_and_resets_streak(self):
        source = self.source()
        _, requests, _ = self.run_script(source, [limited(), page()])
        self.assertEqual([r['key'] for r in requests], [KEY1, KEY1])
        self.assertEqual(source.diagnostics()['key1_429_count'], 0)
        self.assertEqual(source.diagnostics()['failover_count'], 0)

    def test_http_503_breaks_consecutive_429_streak(self):
        source = self.source()
        unavailable = HTTPError('fixture', 503, 'unavailable', {}, io.BytesIO())

        def calls():
            with self.assertRaises(HTTPError):
                self.fetch(source)
            return self.fetch(source)

        _, requests, _ = self.run_script(source, [limited(), unavailable, limited(), page()], calls)
        self.assertEqual([r['key'] for r in requests], [KEY1] * 4)
        self.assertEqual(source.diagnostics()['failover_count'], 0)

    def test_ten_minute_window_boundary(self):
        for second, expected in ((599.999, KEY2), (600, KEY2), (600.001, KEY1)):
            with self.subTest(second=second):
                source = self.source()
                with patch('paper_agents.scout.time.monotonic', side_effect=[0, second]):
                    _, requests, _ = self.run_script(source, [limited(), limited(), page()])
                self.assertEqual(requests[-1]['key'], expected)

    def test_retry_after_numeric_is_honored_on_failover_in_both_paths(self):
        for mode in ('page', 'topic'):
            with self.subTest(mode=mode):
                source = self.source()
                _, _, delays = self.run_script(source, [limited('7'), limited('11'), page()],
                                              lambda: self.fetch(source, mode))
                self.assertEqual(delays, [7, 11])

    def test_retry_after_http_date_is_honored_across_key_switch(self):
        source = self.source()
        with patch('paper_agents.scout.time.time', return_value=0):
            _, _, delays = self.run_script(source, [limited('Thu, 01 Jan 1970 00:00:09 GMT'),
                                                   limited('Thu, 01 Jan 1970 00:00:13 GMT'), page()])
        self.assertEqual(delays, [9, 13])

    def test_zero_retry_after_does_not_bypass_configured_request_spacing(self):
        _, _, delays = self.run_script(self.source(), [limited('0'), limited('0'), page()])
        self.assertTrue(delays)
        self.assertTrue(all(delay >= 2 for delay in delays), delays)

    def test_invalid_retry_after_falls_back_to_bounded_configured_delays(self):
        for value in ('bad', '-1', 'NaN', 'inf', ''):
            with self.subTest(value=value):
                _, _, delays = self.run_script(self.source(), [limited(value), limited(value), page()])
                self.assertEqual(delays, [2, 4])

    def test_key2_remains_active_on_later_pages_without_changing_offsets(self):
        source = self.source()
        def calls():
            self.fetch(source, offset=20)
            return self.fetch(source, offset=30)
        _, requests, _ = self.run_script(source, [limited(), limited(), page(20), page(30)], calls)
        self.assertEqual([r['key'] for r in requests], [KEY1, KEY1, KEY2, KEY2])
        self.assertEqual([parse_qs(urlparse(r['url']).query)['offset'][0] for r in requests],
                         ['20', '20', '20', '30'])

    def test_key2_429_stops_immediately_without_sleeping_or_switching_back(self):
        source = self.source()
        def call():
            with self.assertRaises(HTTPError):
                self.fetch(source)
        _, requests, delays = self.run_script(source, [limited(), limited(), limited()], call)
        self.assertEqual([r['key'] for r in requests], [KEY1, KEY1, KEY2])
        self.assertEqual(len(delays), 2)
        self.assertTrue(source.cooldown_active)
        self.assertTrue(source.diagnostics()['key2_429_stop'])

    def test_page_adapter_cannot_request_again_after_key2_terminal_429(self):
        source = self.source()
        def calls():
            with self.assertRaises(HTTPError):
                self.fetch(source)
            with self.assertRaises((RuntimeError, HTTPError)):
                self.fetch(source)
        _, requests, _ = self.run_script(source, [limited(), limited(), limited(), page()], calls)
        self.assertEqual(len(requests), 3)

    def test_non_429_errors_never_activate_key2(self):
        for code in (400, 401, 403, 404, 500, 503):
            with self.subTest(code=code):
                source = self.source()
                error = HTTPError('fixture', code, 'error', {}, io.BytesIO())
                with patch('paper_agents.scout.urllib.request.urlopen', side_effect=error):
                    with self.assertRaises(HTTPError):
                        self.fetch(source)
                error.close()
                self.assertEqual(source.diagnostics()['failover_count'], 0)

    def test_packaged_environment_requires_both_distinct_private_keys(self):
        for first, second, legacy in (('', '', KEY1), (KEY1, '', ''), ('', KEY2, ''),
                                      (KEY1, KEY1, ''), (' ' + KEY1, KEY1 + ' ', '')):
            with self.subTest(first_present=bool(first), second_present=bool(second)):
                with patch.dict(os.environ, {'PAPER_AGENT_PACKAGED': '1',
                        'SEMANTIC_SCHOLAR_API_KEY1': first, 'SEMANTIC_SCHOLAR_API_KEY2': second,
                        'SEMANTIC_SCHOLAR_API_KEY': legacy}):
                    with self.assertRaises(ValueError) as failure:
                        SemanticScholarSource(verbose=False)
                    self.assertNotIn(KEY1, str(failure.exception))
                    self.assertNotIn(KEY2, str(failure.exception))

    def test_explicit_keys_reject_whitespace_duplicates_and_invalid_types(self):
        for pair in ((KEY1, ' ' + KEY1 + ' '), (' ', KEY2), (1, 2), 'ab'):
            with self.subTest(pair_type=type(pair).__name__):
                with self.assertRaises(ValueError):
                    SemanticScholarSource(api_keys=pair, verbose=False)

    def test_valid_environment_strips_whitespace_and_prefers_pair_over_legacy(self):
        with patch.dict(os.environ, {'PAPER_AGENT_PACKAGED': '1',
                'SEMANTIC_SCHOLAR_API_KEY1': ' ' + KEY1 + ' ',
                'SEMANTIC_SCHOLAR_API_KEY2': ' ' + KEY2 + ' ',
                'SEMANTIC_SCHOLAR_API_KEY': 'ignored-legacy'}):
            source = SemanticScholarSource(verbose=False)
        self.assertEqual(source.api_keys, (KEY1, KEY2))
        self.assertEqual(source._headers()['x-api-key'], KEY1)

    def test_local_legacy_and_anonymous_modes_are_preserved(self):
        for key in ('', KEY1):
            with self.subTest(authenticated=bool(key)):
                with patch.dict(os.environ, {'SEMANTIC_SCHOLAR_API_KEY': key}):
                    source = SemanticScholarSource(verbose=False)
                self.assertEqual(source._headers().get('x-api-key'), key or None)

    def test_exception_and_verbose_log_never_echo_either_key(self):
        source = self.source()
        source.verbose = True
        output = io.StringIO()
        reason = 'provider echoed ' + KEY1 + ' and ' + KEY2
        def calls():
            with self.assertRaises(RuntimeError) as failure:
                source.fetch(['incident'], max_results=2, freshness_months=24)
            return str(failure.exception)
        with redirect_stdout(output):
            message, _, _ = self.run_script(source, [limited(reason=reason) for _ in range(3)], calls)
        for secret in (KEY1, KEY2):
            self.assertNotIn(secret, output.getvalue() + message + json.dumps(source.last_diagnostics))

    def test_real_http_sends_the_actual_active_key_header(self):
        requests = []
        class Provider(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.headers.get('X-Api-Key'), self.path))
                code = 429 if len(requests) <= 2 else 200
                body = json.dumps({'message': 'limited'} if code == 429 else page()).encode()
                self.send_response(code)
                self.send_header('Retry-After', '1')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            source = self.source()
            source.api_url = f'http://127.0.0.1:{server.server_port}/search'
            with patch('paper_agents.scout.time.sleep'):
                self.fetch(source)
            self.assertEqual([key for key, _ in requests], [KEY1, KEY1, KEY2])
            self.assertEqual(len({path for _, path in requests}), 1)
        finally:
            server.shutdown(); server.server_close(); thread.join(3)


class FailoverPersistenceQA(FailoverFixture):
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'qa.sqlite'
        db.init_db(self.path)
        self.connection = db.connect_db(self.path)
        self.addCleanup(self.connection.close)

    def test_progress_budget_counts_physical_http_attempts_including_failover(self):
        source = self.source()
        progress = SemanticScholarProgress(self.connection, source)
        _, requests, _ = self.run_script(source,
            [limited(), limited(), page(0), page(0), page(0)],
            lambda: progress.fetch(['incident', 'cloud', 'recovery'],
                freshness_months=24, max_candidates=30, errors=[]))
        self.assertLessEqual(len(requests), progress.diagnostics['attempt_budget'])
        self.assertEqual(progress.diagnostics['attempts'], len(requests))

    def test_provider_cooldown_is_persisted_and_next_run_makes_no_http_requests(self):
        source = self.source()
        progress = SemanticScholarProgress(self.connection, source)
        self.run_script(source, [limited(), limited(), limited()],
            lambda: progress.fetch(['incident'], freshness_months=24,
                                   max_candidates=10, errors=[]))
        control = self.connection.execute(
            "SELECT value_json FROM semantic_scholar_search_state WHERE key='@source'"
        ).fetchone()
        self.assertIsNotNone(control)
        self.assertGreater(json.loads(control[0])['not_before'], progress.now.timestamp())
        rerun = SemanticScholarProgress(self.connection, source, now=progress.now)
        _, requests, _ = self.run_script(source, [],
            lambda: rerun.fetch(['incident'], freshness_months=24,
                                max_candidates=10, errors=[]))
        self.assertEqual(requests, [])
        self.assertEqual(rerun.diagnostics['stop_reason'], 'source_cooldown')

    def test_persisted_diagnostics_redact_both_keys_from_reason_body_and_headers(self):
        source = self.source()
        progress = SemanticScholarProgress(self.connection, source)
        echo = KEY1 + ' ' + KEY2
        self.run_script(source,
            [limited(reason=echo, body={'message': echo},
                     extra_headers={'X-Request-ID': echo}) for _ in range(3)],
            lambda: progress.fetch(['incident'], freshness_months=24,
                                   max_candidates=10, errors=[]))
        persisted = json.dumps(progress.diagnostics) + json.dumps([
            tuple(row) for row in self.connection.execute(
                'SELECT key, value_json FROM semantic_scholar_search_state')])
        for secret in (KEY1, KEY2):
            self.assertNotIn(secret, persisted)


    def seed_scout(self, diagnostics):
        cycle = db.create_workflow_cycle(self.connection, mode='test', max_scout_attempts=1)
        run = db.insert_scout_run(self.connection, workflow_cycle_id=cycle,
            attempt_number=1, source='semantic_scholar', target_candidates=1,
            max_candidates=1, freshness_months=24, topics=['incident'], guidance_id=None,
            diagnostics={})
        self.connection.execute('UPDATE scout_runs SET diagnostics_json=? WHERE id=?',
                                (json.dumps(diagnostics), run))
        self.connection.commit()
        return run

    def test_health_renders_despite_unexpected_diagnostics_json_shapes(self):
        for malformed in ([], None, {'source_diagnostics': []}, {'source_diagnostics': None}):
            with self.subTest(shape=malformed):
                self.seed_scout(malformed)
                summary = db.health_summary(self.path)
                self.assertIsInstance(summary['warnings'], list)
                self.assertIn('Health', web.render_health_page(self.path))

    def test_recent_key2_warning_clears_after_successful_latest_scout(self):
        self.seed_scout({'source_diagnostics': {'stop_reason': 'provider_rate_limited_key2'}})
        self.assertTrue(any('key2 received HTTP 429' in warning['message']
                            for warning in db.health_summary(self.path)['warnings']))
        self.seed_scout({'source_diagnostics': {'stop_reason': 'candidate_budget_reached'}})
        self.assertFalse(any('key2 received HTTP 429' in warning['message']
                             for warning in db.health_summary(self.path)['warnings']))

    def test_stale_key2_warning_is_not_reported_as_current(self):
        run = self.seed_scout({'source_diagnostics': {'stop_reason': 'provider_rate_limited_key2'}})
        self.connection.execute("UPDATE scout_runs SET started_at=datetime('now','-8 days') WHERE id=?", (run,))
        self.connection.commit()
        self.assertFalse(any('key2 received HTTP 429' in warning['message']
                             for warning in db.health_summary(self.path)['warnings']))

    def test_real_scout_preserves_offset_and_rows_across_cooldown_and_recovery(self):
        source = self.source()
        def scout():
            cycle = db.create_workflow_cycle(self.connection, mode='test', max_scout_attempts=1)
            result = ScoutAgent(source).run(self.connection, workflow_cycle_id=cycle,
                attempt_number=1, config=ScoutConfig(topics=['incident'], target_candidates=1,
                    max_candidates=1, freshness_months=24, semantic_scholar_progress_enabled=True))
            self.connection.commit()
            return result
        def paper(number):
            return {'paperId': f'QA-{number}', 'title': f'Cloud incident diagnosis {number}',
                    'abstract': 'Incident diagnosis method.', 'authors': [],
                    'publicationDate': '2026-09-01', 'externalIds': {}}
        self.run_script(source, [page(0, [paper(1)], 20)], scout)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM papers').fetchone()[0], 1)
        self.run_script(source, [limited(), limited(), limited()], scout)
        _, requests, _ = self.run_script(source, [], scout)
        self.assertEqual(requests, [])
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM papers').fetchone()[0], 1)
        state = json.loads(self.connection.execute(
            "SELECT value_json FROM semantic_scholar_search_state WHERE key='@source'").fetchone()[0])
        state['not_before'] = 0
        self.connection.execute("UPDATE semantic_scholar_search_state SET value_json=? WHERE key='@source'",
                                (json.dumps(state),))
        self.connection.commit()
        _, requests, _ = self.run_script(source, [page(20, [paper(2)], 30)], scout)
        self.assertEqual(requests[0]['key'], KEY1)
        self.assertEqual(parse_qs(urlparse(requests[0]['url']).query)['offset'], ['20'])
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM papers').fetchone()[0], 2)
        self.assertEqual(self.connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(self.connection.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_concurrent_cooldown_writer_is_preserved_and_stale_writer_fails(self):
        source = self.source()
        progress = SemanticScholarProgress(self.connection, source)
        concurrent = db.connect_db(self.path)
        try:
            with concurrent:
                concurrent.execute('INSERT INTO semantic_scholar_search_state VALUES (?,?)',
                    ('@source', json.dumps({'not_before': 9999999999, 'sequence': 42})))
        finally:
            concurrent.close()
        with self.assertRaisesRegex(RuntimeError, 'Concurrent Semantic Scholar'):
            self.run_script(source, [limited(), limited(), limited()],
                lambda: progress.fetch(['incident'], freshness_months=24,
                                       max_candidates=10, errors=[]))
        state = json.loads(self.connection.execute(
            "SELECT value_json FROM semantic_scholar_search_state WHERE key='@source'").fetchone()[0])
        self.assertEqual(state, {'not_before': 9999999999, 'sequence': 42})
        self.assertFalse(self.connection.in_transaction)

    def test_failover_cannot_exceed_remaining_budget_after_an_earlier_page(self):
        source = self.source()
        progress = SemanticScholarProgress(self.connection, source)
        _, requests, _ = self.run_script(source,
            [page(0), limited(), limited(), page(0)],
            lambda: progress.fetch(['incident', 'cloud'], freshness_months=24,
                                   max_candidates=30, errors=[]))
        self.assertLessEqual(len(requests), progress.diagnostics['attempt_budget'])
        self.assertEqual(progress.diagnostics['attempts'], len(requests))
        control = self.connection.execute(
            "SELECT value_json FROM semantic_scholar_search_state WHERE key='@source'"
        ).fetchone()
        self.assertIsNotNone(control, 'Budget exhaustion after HTTP 429 must persist provider cooldown')
        self.assertGreater(json.loads(control[0])['not_before'], progress.now.timestamp())

    def test_remaining_budget_boundaries_preserve_cooldown_and_block_immediate_rerun(self):
        for consumed in (0, 1, 2):
            for terminal_429 in (False, True):
                with self.subTest(consumed=consumed, terminal_429=terminal_429):
                    self.connection.execute('DELETE FROM semantic_scholar_search_state')
                    self.connection.commit()
                    source = self.source()
                    progress = SemanticScholarProgress(self.connection, source)
                    tail = limited() if terminal_429 else page(0)
                    outcomes = [page(0)] * consumed + [limited(), limited(), tail]
                    _, requests, _ = self.run_script(source, outcomes,
                        lambda: progress.fetch(['first', 'second', 'third'],
                            freshness_months=24, max_candidates=30, errors=[]))
                    self.assertEqual(len(requests), 3)
                    self.assertEqual(progress.diagnostics['attempts'], 3)
                    should_cool_down = consumed > 0 or terminal_429
                    control = self.connection.execute(
                        "SELECT value_json FROM semantic_scholar_search_state WHERE key='@source'"
                    ).fetchone()
                    if should_cool_down:
                        self.assertIsNotNone(control)
                        self.assertGreater(json.loads(control[0])['not_before'], progress.now.timestamp())
                        rerun = SemanticScholarProgress(self.connection, source, now=progress.now)
                        _, repeated, _ = self.run_script(source, [],
                            lambda: rerun.fetch(['first'], freshness_months=24,
                                                max_candidates=30, errors=[]))
                        self.assertEqual(repeated, [])
                        self.assertEqual(rerun.diagnostics['stop_reason'], 'source_cooldown')
                    else:
                        self.assertIsNone(control)

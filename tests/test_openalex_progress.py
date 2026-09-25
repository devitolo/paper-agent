from datetime import datetime, timedelta, timezone
from email.message import Message
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

from paper_agents import db
from paper_agents.openalex_progress import OpenAlexProgress, cooldown_seconds
from paper_agents.scout import OpenAlexSource
from paper_agents.scout_agent import ScoutAgent, ScoutConfig


def work(number, **changes):
    return {'id': f'https://openalex.org/W{number}', 'title': f'Cloud incident diagnosis {number}',
            'type': 'article', 'publication_date': '2026-09-01', **changes}


def page(items, cursor='next'):
    return {'results': items, 'meta': {'next_cursor': cursor}}


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'db.sqlite'
        db.init_db(self.path)
        self.c = db.connect_db(self.path)
        self.source = OpenAlexSource(request_delay=0, verbose=False)
        self.now = datetime(2026, 9, 24, tzinfo=timezone.utc)

    def tearDown(self):
        self.c.close()
        self.temp.cleanup()

    def run_scout(self, response, **kwargs):
        cycle = db.create_workflow_cycle(self.c, mode='test', max_scout_attempts=1)
        with patch.object(self.source, 'fetch_page', side_effect=response) as fetch, patch(
                'paper_agents.scout_agent.topics_with_guidance', side_effect=lambda topics, _: topics):
            result = ScoutAgent(self.source).run(self.c, workflow_cycle_id=cycle, attempt_number=1,
                config=ScoutConfig(topics=kwargs.pop('topics', ['incident']),
                                   openalex_cursor_enabled=True, **kwargs))
        return result, fetch

    def progress(self, now=None):
        self.c.commit()
        return OpenAlexProgress(self.c, self.source, now=now or self.now)

    def fetch(self, p, responses, topics=None, maximum=30):
        with patch.object(self.source, 'fetch_page', side_effect=responses) as mock:
            result = p.fetch(topics or ['incident'], freshness_months=24,
                             max_candidates=maximum, errors=[])
        return result, mock

    def save(self, p):
        cycle = db.create_workflow_cycle(self.c, mode='test', max_scout_attempts=1)
        run = db.insert_scout_run(self.c, workflow_cycle_id=cycle, attempt_number=1,
            source='openalex', target_candidates=20, max_candidates=30,
            freshness_months=24, topics=['incident'], guidance_id=None, diagnostics={})
        with self.c:
            p.checkpoint(run)

    def test_next_run_uses_next_page_and_finds_new_candidate(self):
        first, _ = self.run_scout([page([work(1)], 'second')])
        second, fetch = self.run_scout([page([work(1), work(2)], 'third')])
        self.assertEqual(fetch.call_args.kwargs['cursor'], 'second')
        self.assertEqual(second['eligible_count'], 1)
        self.assertEqual(second['stored_count'], 2)
        self.assertEqual(first['eligible_count'], 1)

    def test_filtered_page_advances_and_records_all_membership(self):
        result, _ = self.run_scout([page([work(1, type='book')], 'second')])
        self.assertEqual(result['stored_count'], 0)
        ledger = json.loads(self.c.execute('SELECT page_json FROM openalex_page_dispositions').fetchone()[0])
        self.assertEqual(ledger['dispositions'][0]['reason'], 'excluded_type:book')
        _, fetch = self.run_scout([page([], None)])
        self.assertEqual(fetch.call_args.kwargs['cursor'], 'second')

    def test_candidate_write_failure_rolls_back_checkpoint_and_page(self):
        with patch('paper_agents.db.insert_scout_candidate', side_effect=RuntimeError('write failed')):
            with self.assertRaisesRegex(RuntimeError, 'write failed'):
                self.run_scout([page([work(1)], 'second')])
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM openalex_search_state').fetchone()[0], 0)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM papers').fetchone()[0], 0)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM openalex_page_dispositions').fetchone()[0], 0)
        _, fetch = self.run_scout([page([work(1)])])
        self.assertEqual(fetch.call_args.kwargs['cursor'], '*')

    def test_request_budget_and_fair_rotation(self):
        p = self.progress()
        _, fetch = self.fetch(p, [page([])] * 3, topics=list('abcdef'))
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(p.diagnostics['deferred_queries'], list('def'))
        self.save(p)
        p = self.progress()
        _, fetch = self.fetch(p, [page([])] * 3, topics=list('abcdef'))
        self.assertEqual([x.args[0] for x in fetch.call_args_list], list('def'))

    def test_cooldown_persists_even_without_candidate_checkpoint(self):
        headers = Message(); headers['Retry-After'] = '120'
        error = urllib.error.HTTPError('url', 429, 'rate', headers, io.BytesIO())
        p = self.progress()
        _, fetch = self.fetch(p, [error], topics=list('abc'))
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(p.diagnostics['stop_reason'], 'source_cooldown')
        p = self.progress(self.now + timedelta(seconds=60))
        _, fetch = self.fetch(p, [], topics=list('abc'))
        self.assertEqual(fetch.call_count, 0)
        self.assertEqual(p.diagnostics['stop_reason'], 'source_cooldown')

    def test_cooldown_commits_with_preceding_successful_page(self):
        error = urllib.error.HTTPError('url', 429, 'rate', Message(), io.BytesIO())
        result, fetch = self.run_scout([page([work(1)]), error], topics=['a', 'b', 'c'])
        self.assertEqual(result['stored_count'], 1)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM openalex_page_dispositions').fetchone()[0], 1)

    def test_frozen_window_and_refresh_preserve_deep_cursor(self):
        p = self.progress(); self.fetch(p, [page([work(1)], 'deep')]); self.save(p)
        p = self.progress(self.now + timedelta(days=1))
        _, fetch = self.fetch(p, [page([], 'deeper')]); self.save(p)
        self.assertEqual(fetch.call_args.kwargs['through'], '2026-09-24')
        p = self.progress(self.now + timedelta(days=8))
        _, fetch = self.fetch(p, [page([work(2)], 'fresh-next')]); self.save(p)
        self.assertEqual(fetch.call_args.kwargs['cursor'], '*')
        self.assertEqual(fetch.call_args.kwargs['through'], '2026-10-02')
        p = self.progress(self.now + timedelta(days=9))
        _, fetch = self.fetch(p, [page([], None)])
        self.assertEqual(fetch.call_args.kwargs['cursor'], 'deeper')

    def test_changed_query_gets_new_progress(self):
        p = self.progress(); self.fetch(p, [page([], 'deep')]); self.save(p)
        p = self.progress(); _, fetch = self.fetch(p, [page([])], topics=['different'])
        self.assertEqual(fetch.call_args.kwargs['cursor'], '*')

    def test_expired_traversal_resets_window(self):
        p = self.progress(); self.fetch(p, [page([], 'deep')]); self.save(p)
        p = self.progress(self.now + timedelta(days=31))
        _, fetch = self.fetch(p, [page([])])
        self.assertEqual(fetch.call_args.kwargs['cursor'], '*')
        self.assertEqual(fetch.call_args.kwargs['through'], '2026-10-25')

    def test_400_continuation_reset_is_deferred(self):
        p = self.progress(); self.fetch(p, [page([], 'bad')]); self.save(p)
        p = self.progress()
        _, fetch = self.fetch(p, [urllib.error.HTTPError('url',400,'bad',Message(),io.BytesIO())])
        self.assertEqual(fetch.call_count, 1); self.save(p)
        p = self.progress(); _, fetch = self.fetch(p, [page([])])
        self.assertEqual(fetch.call_args.kwargs['cursor'], '*')

    def test_concurrent_checkpoint_rejected(self):
        p1 = self.progress(); p2 = self.progress()
        self.fetch(p1, [page([], 'one')]); self.fetch(p2, [page([], 'two')]); self.save(p1)
        with self.assertRaisesRegex(RuntimeError, 'Concurrent'):
            self.save(p2)

    def test_page_size_matches_remaining_capacity_no_truncation(self):
        result, fetch = self.run_scout([page([work(1), work(2)])], max_candidates=2)
        self.assertEqual(fetch.call_args.kwargs['page_size'], 2)
        self.assertEqual(result['stored_count'], 2)

    def test_no_write_transaction_during_http(self):
        def response(*args, **kwargs):
            self.assertFalse(self.c.in_transaction)
            return page([work(1)])
        self.run_scout(response)

    def test_end_of_traversal_waits_until_refresh(self):
        p = self.progress(); self.fetch(p, [page([], None)]); self.save(p)
        p = self.progress(self.now + timedelta(days=1))
        _, fetch = self.fetch(p, [])
        self.assertEqual(fetch.call_count, 0)
        self.assertEqual(p.diagnostics['stop_reason'], 'traversals_exhausted_until_refresh')

    def test_repeated_cursor_does_not_advance(self):
        p = self.progress(); self.fetch(p, [page([], 'deep')]); self.save(p)
        p = self.progress(); self.fetch(p, [page([], 'deep')]); self.save(p)
        self.assertEqual(p.diagnostics['stop_reason'], 'source_error')
        p = self.progress(); _, fetch = self.fetch(p, [page([], 'next')])
        self.assertEqual(fetch.call_args.kwargs['cursor'], 'deep')

    def test_duplicate_membership_saved_for_both_queries(self):
        result, _ = self.run_scout([page([work(1)]), page([work(1)])], topics=['a', 'b'])
        self.assertEqual(result['stored_count'], 1)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM openalex_page_dispositions').fetchone()[0], 2)

    def test_all_candidate_rows_rollback_after_partial_save(self):
        original = db.insert_scout_candidate
        calls = []
        def fail_second(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('second write failed')
            return original(*args, **kwargs)
        with patch('paper_agents.db.insert_scout_candidate', side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, 'second write failed'):
                self.run_scout([page([work(1), work(2)])])
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM papers').fetchone()[0], 0)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM scout_candidates').fetchone()[0], 0)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM openalex_search_state').fetchone()[0], 0)

    def test_changed_freshness_policy_gets_new_traversal(self):
        p = self.progress(); self.fetch(p, [page([], 'deep')]); self.save(p)
        p = self.progress()
        with patch.object(self.source, 'fetch_page', return_value=page([])) as fetch:
            p.fetch(['incident'], freshness_months=12, max_candidates=30, errors=[])
        self.assertEqual(fetch.call_args.kwargs['cursor'], '*')

    def test_retry_after_date_and_invalid_fallback(self):
        self.assertEqual(cooldown_seconds('Thu, 24 Sep 2026 00:02:00 GMT', self.now),120)
        for value in [None,'nonsense','NaN','-1']:
            self.assertTrue(60 <= cooldown_seconds(value,self.now) <= 90)

    def test_transport_sends_cursor_and_single_attempt(self):
        response = io.BytesIO(json.dumps(page([work(1)], 'next')).encode())
        with patch('urllib.request.urlopen', return_value=response) as request:
            self.source.fetch_page('llm', cursor='opaque+token', cutoff='2024-09-01',
                                   through='2026-09-24', page_size=10)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(request.call_args.args[0].full_url).query)
        self.assertEqual(query['cursor'], ['opaque+token'])
        self.assertEqual(query['search'], ['llm software cloud operations observability'])
        error = urllib.error.HTTPError('url',429,'rate',Message(),io.BytesIO())
        with patch('urllib.request.urlopen', side_effect=error) as request:
            with self.assertRaises(urllib.error.HTTPError):
                self.source.fetch_page('llm', cursor='*', cutoff='2024-09-01', through='2026-09-24',page_size=10)
        self.assertEqual(request.call_count, 1)

    def test_oversized_or_malformed_page_rejected(self):
        for payload in [page([work(1),work(2)]), {'results': [], 'meta': {}}, page(['bad'])]:
            with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(payload).encode())):
                with self.assertRaises(ValueError):
                    self.source.fetch_page('llm',cursor='*',cutoff='2024-01-01',through='2026-09-24',page_size=1)

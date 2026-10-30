from datetime import timedelta
import io
import json
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

from paper_agents import db
from paper_agents.scout import core_work_to_candidate
from tests import test_core_progress as fixtures


class CoreRefillTests(unittest.TestCase):
    setUp = fixtures.CoreProgressTests.setUp
    tearDown = fixtures.CoreProgressTests.tearDown
    progress = fixtures.CoreProgressTests.progress
    fetch = fixtures.CoreProgressTests.fetch
    save = fixtures.CoreProgressTests.save
    run_scout = fixtures.CoreProgressTests.run_scout

    def known(self):
        db.upsert_paper(self.connection, core_work_to_candidate(fixtures.work(1)).as_dict())
        self.connection.commit()

    def test_duplicate_then_old_then_valid_refills_single_query_with_three_attempts(self):
        self.known(); progress = self.progress()
        result, fetch = self.fetch(progress, [fixtures.page([fixtures.work(1)], next_offset=1),
            fixtures.page([fixtures.work(2, publishedDate='2000-01-01')], offset=1, next_offset=2),
            fixtures.page([fixtures.work(3)], offset=2, next_offset=3)], maximum=1)
        self.assertEqual([call.kwargs['offset'] for call in fetch.call_args_list], [0,1,2])
        self.assertEqual(len(result), 1)
        self.assertEqual(progress.diagnostics['attempts'], 3)
        self.assertEqual(progress.diagnostics['stop_reason'], 'candidate_budget_reached')
        self.assertFalse(self.connection.in_transaction)

    def test_actual_scout_and_transport_advance_checkpoint_across_runs(self):
        self.known(); offsets = []
        def response(request, timeout):
            offset = int(urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)['offset'][0])
            offsets.append(offset)
            return io.BytesIO(json.dumps({'offset':offset, 'limit':10, 'totalHits':100,
                'results':[fixtures.work(1 if offset == 0 else offset+1)]}).encode())
        with patch('paper_agents.scout.urllib.request.urlopen', side_effect=response):
            # Use the real CoreSource adapter, not a prebuilt candidate pool.
            from paper_agents.scout_agent import ScoutAgent, ScoutConfig
            for _ in range(2):
                cycle = db.create_workflow_cycle(self.connection, mode='test', max_scout_attempts=1)
                result = ScoutAgent(self.source).run(self.connection, workflow_cycle_id=cycle, attempt_number=1,
                    config=ScoutConfig(topics=['incident'], core_progress_enabled=True, max_candidates=1))
                self.assertEqual(result['eligible_count'], 1)
        self.assertEqual(offsets, [0,1,2])
        state = json.loads(self.connection.execute("SELECT value_json FROM core_search_state WHERE key!='@source'").fetchone()[0])
        self.assertEqual(state['deep']['offset'], 3)

    def test_three_queries_share_budget_and_deferred_query_rotates_first(self):
        self.known(); progress = self.progress()
        result, fetch = self.fetch(progress, [fixtures.page([fixtures.work(1)], next_offset=1),
            fixtures.page([fixtures.work(2)], offset=1, next_offset=2),
            fixtures.page([fixtures.work(1)], next_offset=1)], topics=['a','b','c'])
        self.assertEqual([call.args[0] for call in fetch.call_args_list], ['a','a','b'])
        self.assertEqual(progress.diagnostics['attempts'], 3)
        self.assertEqual(progress.diagnostics['deferred_queries'], ['c'])
        self.save(progress)
        later = self.progress()
        _, fetch = self.fetch(later, [fixtures.page([fixtures.work(i)]) for i in (3,4,5)], topics=['a','b','c'])
        self.assertEqual([call.args[0] for call in fetch.call_args_list], ['c','a','b'])

    def test_freshness_continuation_does_not_reset_or_trap_deep_offset(self):
        self.known(); progress = self.progress()
        self.fetch(progress, [fixtures.page([fixtures.work(2)], next_offset=40)])
        self.save(progress)
        refreshed = self.progress(self.now + timedelta(days=8))
        _, fetch = self.fetch(refreshed, [fixtures.page([fixtures.work(1)], next_offset=1),
            fixtures.page([fixtures.work(3)], offset=1, next_offset=2)])
        self.assertEqual([call.kwargs['offset'] for call in fetch.call_args_list], [0,1])
        state = next(value for key,value in refreshed.states.items() if key != '@source')
        self.assertEqual(state['deep']['offset'], 40)
        self.assertEqual([row['kind'] for row in refreshed.diagnostics['pages']], ['freshness','freshness'])
        self.save(refreshed)
        _, fetch = self.fetch(self.progress(self.now + timedelta(days=9)), [fixtures.page([fixtures.work(4)], offset=40, next_offset=41)])
        self.assertEqual(fetch.call_args.kwargs['offset'], 40)

    def test_exhaustion_and_nonadvancing_pages_do_not_spin(self):
        self.known()
        for payload, expected in [(fixtures.page([], next_offset=None), None),
                                  (fixtures.page([fixtures.work(1)], next_offset=0), 'pagination_not_advancing')]:
            with self.subTest(expected=expected):
                progress = self.progress()
                _, fetch = self.fetch(progress, [payload])
                self.assertEqual(fetch.call_count, 1)
                if expected: self.assertEqual(progress.diagnostics['stop_reason'], expected)

    def test_freshness_refill_can_continue_when_deep_traversal_is_exhausted(self):
        self.known(); progress = self.progress()
        self.fetch(progress, [fixtures.page([fixtures.work(2)], next_offset=None)])
        self.save(progress)
        refreshed = self.progress(self.now + timedelta(days=8))
        result, fetch = self.fetch(refreshed, [fixtures.page([fixtures.work(1)], next_offset=1),
            fixtures.page([fixtures.work(3)], offset=1, next_offset=2)])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(len(result), 1)
        state = next(value for key,value in refreshed.states.items() if key != '@source')
        self.assertTrue(state['deep']['exhausted'])

    def test_reordered_duplicate_page_is_still_detected_as_repeated(self):
        self.known()
        db.upsert_paper(self.connection, core_work_to_candidate(fixtures.work(2)).as_dict())
        self.connection.commit(); progress = self.progress()
        _, fetch = self.fetch(progress, [fixtures.page([fixtures.work(1),fixtures.work(2)], next_offset=2),
            fixtures.page([fixtures.work(2),fixtures.work(1)], offset=2, next_offset=4)])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(progress.diagnostics['stop_reason'], 'repeated_page')

    def test_repeated_provider_page_stops_without_skipping_checkpoint(self):
        self.known(); progress = self.progress()
        _, fetch = self.fetch(progress, [fixtures.page([fixtures.work(1)], next_offset=1),
            fixtures.page([fixtures.work(1)], offset=1, next_offset=2)])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(progress.diagnostics['stop_reason'], 'repeated_page')
        self.save(progress)
        _, fetch = self.fetch(self.progress(), [fixtures.page([fixtures.work(2)], offset=1, next_offset=2)])
        self.assertEqual(fetch.call_args.kwargs['offset'], 1)

    def test_429_during_continuation_preserves_progress_and_shared_cooldown(self):
        self.known(); progress = self.progress()
        error = urllib.error.HTTPError('url',429,'rate',{'Retry-After':'120'},io.BytesIO())
        _, fetch = self.fetch(progress, [fixtures.page([fixtures.work(1)], next_offset=1), error], topics=['a','b','c'])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(progress.diagnostics['stop_reason'], 'source_cooldown')
        self.save(progress)
        later = self.progress(self.now + timedelta(seconds=60))
        _, fetch = self.fetch(later, [], topics=['a','b','c'])
        fetch.assert_not_called()

    def test_long_retry_after_stops_before_sleeping_past_time_budget(self):
        progress = self.progress()
        error = urllib.error.HTTPError('url',503,'unavailable',{'Retry-After':'10000'},io.BytesIO())
        with patch('paper_agents.core_progress.time.sleep') as sleep:
            _, fetch = self.fetch(progress, [error])
        self.assertEqual(fetch.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(progress.diagnostics['stop_reason'], 'time_budget_reached')

"""Offline acceptance tests through the actual Semantic Scholar HTTP adapter."""
from datetime import datetime, timedelta, timezone
from email.message import Message
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from paper_agents import db
from paper_agents.scout import SemanticScholarSource, semantic_scholar_paper_to_candidate
from paper_agents.scout_agent import ScoutAgent, ScoutConfig
from paper_agents.semantic_scholar_progress import SemanticScholarProgress
from tests.test_semantic_scholar_progress import page, paper


class SemanticFourPageExperimentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / 'test.db'
        db.init_db(path)
        self.connection = db.connect_db(path)
        self.addCleanup(self.connection.close)
        self.source = SemanticScholarSource(api_keys=('fixture-one', 'fixture-two'), request_delay=0,
                                            timeout=60, verbose=False)
        self.now = datetime(2026, 10, 28, tzinfo=timezone.utc)
        self.calls = []

    def known(self, *ids):
        for number in ids:
            db.upsert_paper(self.connection, semantic_scholar_paper_to_candidate(paper(number)).as_dict())
        self.connection.commit()

    def transport(self, responses):
        responses = iter(responses)
        def urlopen(request, **kwargs):
            query = parse_qs(urlsplit(request.full_url).query)
            self.assertFalse(self.connection.in_transaction)
            self.calls.append((query['query'][0], int(query['offset'][0]), request.get_header('X-api-key')))
            self.assertGreater(kwargs['timeout'], 0)
            self.assertLessEqual(kwargs['timeout'], 60)
            item = next(responses)
            if isinstance(item, Exception):
                raise item
            return io.BytesIO(json.dumps(item).encode())
        return patch('urllib.request.urlopen', side_effect=urlopen)

    def progress(self, **kwargs):
        self.connection.commit()
        return SemanticScholarProgress(self.connection, self.source, now=kwargs.pop('now', self.now),
                                       experiment=True, **kwargs)

    def fetch(self, progress, responses, topics=None):
        with self.transport(responses), patch('time.sleep'):
            result = progress.fetch(topics or ['incident'], freshness_months=24, max_candidates=30, errors=[])
        return result

    def run_scout(self, responses, *, experiment=True):
        cycle = db.create_workflow_cycle(self.connection, mode='test', max_scout_attempts=1)
        with self.transport(responses), patch('time.sleep'):
            result = ScoutAgent(self.source).run(self.connection, workflow_cycle_id=cycle, attempt_number=1,
                config=ScoutConfig(topics=['incident'], semantic_scholar_progress_enabled=True,
                                   semantic_scholar_four_page_experiment=experiment))
        return result

    @staticmethod
    def error(status, retry_after='0'):
        headers = Message()
        headers['Retry-After'] = retry_after
        return HTTPError('https://api.semanticscholar.org/graph/v1/paper/search', status,
                         'fixture provider error', headers, io.BytesIO(b'{"message":"fixture"}'))

    def test_duplicate_then_new_is_one_scout_run_and_commits_both_pages(self):
        self.known(1)
        result = self.run_scout([page([paper(1)], next_offset=10), page([paper(2)], offset=10, next_offset=20)])
        self.assertEqual(result['stored_count'], 1)
        self.assertEqual([offset for _,offset,_ in self.calls], [0,10])
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM semantic_scholar_page_dispositions').fetchone()[0], 2)
        next_run = self.progress()
        self.fetch(next_run, [page([paper(3)], offset=20, next_offset=30)])
        self.assertEqual(self.calls[-1][1], 20)

    def test_four_duplicate_pages_use_two_keys_and_stop_at_four(self):
        self.known(1,2,3,4)
        progress = self.progress()
        result = self.fetch(progress, [page([paper(n+1)], offset=n*10, next_offset=(n+1)*10) for n in range(4)])
        self.assertEqual(result, [])
        self.assertEqual([key for _,_,key in self.calls], ['fixture-one']*2 + ['fixture-two']*2)
        self.assertEqual(progress.diagnostics['attempts'], 4)
        self.assertEqual(progress.diagnostics['stop_reason'], 'page_budget_reached')
        self.assertEqual(progress.diagnostics['pagination_key_switches'], 1)
        self.assertEqual(progress.diagnostics['failover_count'], 0)
        self.assertNotIn('fixture-one',json.dumps(progress.diagnostics))
        self.assertNotIn('fixture-two',json.dumps(progress.diagnostics))

    def test_transient_retry_consumes_the_same_four_attempts(self):
        self.known(1,2,3)
        progress = self.progress()
        result = self.fetch(progress, [self.error(503), page([paper(1)], next_offset=10),
            page([paper(2)], offset=10, next_offset=20), page([paper(3)], offset=20, next_offset=30)])
        self.assertEqual(result, [])
        self.assertEqual(progress.diagnostics['attempts'], 4)
        self.assertEqual(len(progress.pages), 3)
        self.assertEqual(progress.diagnostics['stop_reason'], 'request_budget_reached')
        self.assertEqual([offset for _,offset,_ in self.calls], [0,0,10,20])

    def test_key1_429_failover_uses_budget_and_never_switches_back(self):
        self.known(1)
        progress = self.progress()
        result = self.fetch(progress, [self.error(429), self.error(429), page([paper(1)], next_offset=10),
                                      page([paper(2)], offset=10, next_offset=20)])
        self.assertEqual(len(result), 1)
        self.assertEqual([key for _,_,key in self.calls], ['fixture-one']*2 + ['fixture-two']*2)
        self.assertEqual(progress.diagnostics['attempts'], 4)
        self.assertEqual(progress.diagnostics['failover_count'], 1)
        self.assertEqual(progress.diagnostics['stop_reason'], 'new_paper_found')

    def test_both_keys_429_stop_and_persist_cooldown_without_more_requests(self):
        progress = self.progress()
        self.fetch(progress, [self.error(429,'120') for _ in range(3)])
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(progress.diagnostics['key2_429_stop'])
        self.assertEqual(progress.diagnostics['stop_reason'], 'provider_rate_limited_key2')
        self.assertIn('@source', dict(self.connection.execute('SELECT key,value_json FROM semantic_scholar_search_state')))
        next_run = self.progress(now=self.now + timedelta(seconds=1))
        self.fetch(next_run, [])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(next_run.diagnostics['stop_reason'], 'source_cooldown')

    def test_repeated_provider_page_does_not_advance_its_checkpoint(self):
        self.known(1)
        progress = self.progress()
        self.fetch(progress, [page([paper(1)], next_offset=10), page([paper(1)], offset=10, next_offset=20)])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(progress.diagnostics['stop_reason'], 'repeated_provider_page')
        state = next(v for k,v in progress.states.items() if k != '@source')
        self.assertEqual(state['deep']['offset'], 10)

    def test_nonadvancing_cursor_is_rejected_without_advancing(self):
        progress = self.progress()
        self.fetch(progress, [page([], next_offset=0)])
        self.assertEqual(progress.diagnostics['stop_reason'], 'non_advancing_cursor')
        self.assertEqual(progress.diagnostics['attempts'], 1)
        self.assertEqual(progress.pages, [])

    def test_provider_exhaustion_stops_without_extra_request(self):
        progress = self.progress()
        self.fetch(progress, [page([], next_offset=None)])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(progress.diagnostics['stop_reason'], 'provider_exhausted')

    def test_query_rotation_is_fair_before_revisiting_a_query(self):
        self.known(1,2,3,4)
        progress = self.progress()
        self.fetch(progress, [page([paper(1)]), page([paper(2)]),
                             page([paper(3)], offset=10,next_offset=20),
                             page([paper(4)], offset=10,next_offset=20)], topics=['a','b'])
        self.assertEqual([topic for topic,_,_ in self.calls], ['a','b','a','b'])

    def test_long_retry_after_is_bounded_without_sleeping_or_another_attempt(self):
        progress = self.progress()
        with self.transport([self.error(429, '10000')]), patch('time.sleep') as sleep:
            progress.fetch(['incident'], freshness_months=24, max_candidates=30, errors=[])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(progress.diagnostics['attempts'], 1)
        self.assertEqual(progress.diagnostics['stop_reason'], 'time_budget_reached')
        sleep.assert_not_called()

    def test_atomic_candidate_write_failure_rolls_back_pages_and_cursors(self):
        self.known(1)
        with patch('paper_agents.db.insert_scout_candidate', side_effect=RuntimeError('fixture failure')):
            with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
                self.run_scout([page([paper(1)]),page([paper(2)],offset=10,next_offset=20)])
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM semantic_scholar_search_state').fetchone()[0],0)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM semantic_scholar_page_dispositions').fetchone()[0],0)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM papers').fetchone()[0],1)

    def test_default_path_remains_three_attempts_and_one_page_per_query(self):
        self.known(1)
        progress = SemanticScholarProgress(self.connection,self.source,now=self.now)
        self.fetch(progress,[page([paper(1)])])
        self.assertEqual(len(self.calls),1)
        self.assertEqual(progress.diagnostics['attempt_budget'],3)
        self.assertNotIn('experiment',progress.diagnostics)

    def test_first_new_page_on_key2_stops_before_page_four(self):
        self.known(1,2)
        progress = self.progress()
        result = self.fetch(progress,[page([paper(1)]),page([paper(2)],offset=10,next_offset=20),
                                     page([paper(3)],offset=20,next_offset=30)])
        self.assertEqual(len(result),1)
        self.assertEqual([key for _,_,key in self.calls],['fixture-one','fixture-one','fixture-two'])
        self.assertEqual(progress.diagnostics['stop_reason'],'new_paper_found')

    def test_freshness_sample_keeps_deep_checkpoint_then_resumes_it_same_run(self):
        self.known(1)
        first = self.progress()
        self.fetch(first,[page([paper(2)],next_offset=30)])
        with self.connection:
            self.connection.executemany('INSERT INTO semantic_scholar_search_state VALUES (?,?)',
                [(key,json.dumps(value)) for key,value in first.states.items()])
        progress = self.progress(now=self.now + timedelta(days=8))
        result = self.fetch(progress,[page([paper(1)],next_offset=10),
            page([paper(3)],offset=30,next_offset=40)])
        self.assertEqual(len(result),1)
        self.assertEqual([page['kind'] for _,_,page in progress.pages],['freshness','continuation'])
        self.assertEqual([page['offset'] for _,_,page in progress.pages],[0,30])
        state = next(v for k,v in progress.states.items() if k!='@source')
        self.assertEqual(state['deep']['offset'],40)

    def test_elapsed_time_budget_does_not_reset_for_next_page(self):
        self.known(1)
        clock = [0.0]
        progress = self.progress()
        def response(request, **kwargs):
            clock[0]=241.0
            return io.BytesIO(json.dumps(page([paper(1)])).encode())
        with patch('time.monotonic',side_effect=lambda:clock[0]), \
             patch('urllib.request.urlopen',side_effect=response) as fetch:
            progress.fetch(['incident'],freshness_months=24,max_candidates=30,errors=[])
        self.assertEqual(fetch.call_count,1)
        self.assertEqual(progress.diagnostics['attempts'],1)
        self.assertEqual(progress.diagnostics['stop_reason'],'time_budget_reached')

    def test_malformed_page_counts_actual_attempt_without_checkpointing(self):
        progress = self.progress()
        self.fetch(progress,[{'offset':0,'data':None}])
        self.assertEqual(progress.diagnostics['attempts'],1)
        self.assertEqual(progress.diagnostics['stop_reason'],'source_error')
        self.assertEqual(progress.pages,[])

    def test_experiment_requires_two_keys_before_any_request(self):
        source = SemanticScholarSource(api_key='single-fixture',verbose=False)
        with patch('urllib.request.urlopen') as fetch, self.assertRaisesRegex(ValueError,'two distinct keys'):
            SemanticScholarProgress(self.connection,source,experiment=True)
        fetch.assert_not_called()

    def test_production_environment_flag_enables_semantic_progress_only(self):
        import os
        self.known(1)
        with patch.dict(os.environ, {'PAPER_SEMANTIC_SCHOLAR_FOUR_PAGE_EXPERIMENT':'1'}):
            result=self.run_scout([page([paper(1)]),page([paper(2)],offset=10,next_offset=20)],experiment=False)
        self.assertEqual(result['stored_count'],1)
        self.assertEqual([offset for _,offset,_ in self.calls],[0,10])

    def test_updater_persists_explicit_activation_and_supports_disable(self):
        import subprocess
        import sys
        root=Path(__file__).resolve().parents[1]
        script=(root/'scripts/mini_production_update.sh').read_text()
        code=script.split('python3 - "$ENV_FILE" "$tmp_env"',1)[1].split("<<'PY'\n",1)[1].split('\nPY',1)[0]
        source=Path(self.tmp.name)/'env'
        target=Path(self.tmp.name)/'next'
        source.write_text('PRIVATE_FIXTURE=preserved\n')
        args=[sys.executable,'-c',code,str(source),str(target),'fixture-image','fixture-model','','','','']
        subprocess.run(args+['1'],check=True,capture_output=True)
        self.assertIn('PAPER_SEMANTIC_SCHOLAR_FOUR_PAGE_EXPERIMENT=1',target.read_text())
        self.assertIn('PRIVATE_FIXTURE=preserved',target.read_text())
        source.write_bytes(target.read_bytes())
        subprocess.run(args+['0'],check=True,capture_output=True)
        self.assertIn('PAPER_SEMANTIC_SCHOLAR_FOUR_PAGE_EXPERIMENT=0',target.read_text())
        self.assertNotIn('PAPER_SEMANTIC_SCHOLAR_FOUR_PAGE_EXPERIMENT=1',target.read_text())

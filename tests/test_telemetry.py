"""Hermetic trace contracts. Optional SDK tests run in the telemetry venv."""
import io
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error

from paper_agents import telemetry as t
from paper_agents.local_extract import call_ollama, extract_chunks
from paper_agents.pipeline import run_daily_pipeline
from paper_agents.scout import ArxivSource

try:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
    HAVE_SDK = True
except ImportError:
    HAVE_SDK = False


class NoDependencyTests(unittest.TestCase):
    def test_disabled_does_not_initialize_exporter(self):
        with patch.dict(os.environ, {"PAPER_AGENT_TELEMETRY": "0"}), patch.object(t, 'Backend', side_effect=AssertionError('must not initialize')):
            self.assertIsNone(t._get_backend())
            @t.pipeline_trace
            def function():
                return 42
            self.assertEqual(function(), 42)

    def test_missing_dependency_and_bad_endpoint_fail_open(self):
        with patch.dict(os.environ, {"PAPER_AGENT_TELEMETRY": "1"}), patch.object(t, '_backend', None), patch.object(t, 'Backend', side_effect=ImportError('PRIVATE')):
            self.assertIsNone(t._get_backend())
        for url in ('bad', 'http://user:PRIVATE@localhost/v1/traces', 'http://localhost/v1/traces?key=PRIVATE', 'http://localhost/nope'):
            with self.subTest(url=url), patch.dict(os.environ, {"PAPER_AGENT_TELEMETRY": "1", "PAPER_AGENT_OTLP_ENDPOINT": url}), patch.object(t, '_backend', None):
                self.assertIsNone(t._get_backend())

    def test_initialization_failure_does_not_change_original_exception(self):
        original = ValueError('PRIVATE')
        @t.pipeline_trace
        def function():
            raise original
        with patch.object(t, '_get_backend', side_effect=RuntimeError('PRIVATE')):
            with self.assertRaises(ValueError) as caught:
                function()
        self.assertIs(caught.exception, original)


@unittest.skipUnless(HAVE_SDK, 'install requirements-telemetry.txt for SDK tests')
class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.exporter = InMemorySpanExporter()
        self.backend = t.Backend(self.exporter)
        self.addCleanup(self.backend.processor.shutdown)
        self.override = patch.object(t, '_get_backend', return_value=self.backend)
        self.override.start()
        self.addCleanup(self.override.stop)

    def spans(self):
        self.backend.processor.force_flush()
        return self.exporter.get_finished_spans()

    def test_thread_context_actual_llm_usage_and_privacy(self):
        payload = {'response': json.dumps({'research_problem':'PRIVATE_RESPONSE'}),
                   'prompt_eval_count':12, 'eval_count':0}
        @t.pipeline_trace
        def run():
            with t.span('reviewer', paper_id=42):
                return extract_chunks('http://local/api/generate?secret=PRIVATE_URL', 'fixture-model',
                                      ['PRIVATE_PROMPT', 'PRIVATE_FEEDBACK'], 5, workers=2)
        with patch('urllib.request.urlopen', side_effect=lambda *a, **kw: io.BytesIO(json.dumps(payload).encode())):
            result = run()
        self.assertEqual(len(result), 2)
        spans = self.spans()
        roots = [s for s in spans if s.parent is None]
        self.assertEqual(len(roots), 1)
        self.assertEqual(len({s.context.trace_id for s in spans}), 1)
        reviewer = next(s for s in spans if s.name == 'reviewer')
        chunks = [s for s in spans if s.name == 'reviewer.chunk']
        self.assertTrue(all(s.parent.span_id == reviewer.context.span_id for s in chunks))
        calls = [s for s in spans if s.name == 'ollama.generate']
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(s.parent.span_id in {c.context.span_id for c in chunks} for s in calls))
        for call in calls:
            self.assertEqual(call.attributes['llm.token_count.prompt'], 12)
            self.assertEqual(call.attributes['llm.token_count.completion'], 0)
        encoded = encode_spans(spans).SerializeToString()
        self.assertNotIn(b'PRIVATE', encoded)
        self.assertNotIn(b'http://', encoded)
        self.assertEqual(dict(roots[0].resource.attributes), {'service.name':'project-paper', 'openinference.project.name':'project-paper'})

    def test_unknown_usage_and_original_error_remain_unknown_and_private(self):
        @t.pipeline_trace
        def run():
            return call_ollama('http://unused', 'fixture', 'PRIVATE', 1)
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'{"response":"PRIVATE"}')):
            run()
        original = urllib.error.HTTPError('http://PRIVATE', 503, 'PRIVATE', {}, None)
        with patch('urllib.request.urlopen', side_effect=original):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                run()
        self.assertIs(caught.exception, original)
        for item in self.spans():
            self.assertFalse(any('token_count' in key for key in item.attributes))
            self.assertFalse(item.events)
        self.assertNotIn(b'PRIVATE', encode_spans(self.spans()).SerializeToString())

    def test_real_source_http_attempts_retry_without_llm(self):
        @t.pipeline_trace
        def run():
            with t.span('scout'):
                return ArxivSource(retries=1, request_delay=0, verbose=False)._fetch_topic('PRIVATE', 1)
        error = urllib.error.HTTPError('http://PRIVATE',429,'PRIVATE',{},None)
        with patch('urllib.request.urlopen', side_effect=[error, io.BytesIO(b'<feed xmlns="http://www.w3.org/2005/Atom"/>')]), patch('paper_agents.scout.time.sleep'):
            self.assertEqual(run(), [])
        spans = self.spans()
        requests = [s for s in spans if s.name == 'source.http']
        self.assertEqual([s.attributes['paper.attempt'] for s in requests], [1,2])
        self.assertEqual(requests[0].attributes['paper.http_status'],429)
        self.assertFalse(any(s.attributes.get('openinference.span.kind')=='LLM' for s in spans))
        self.assertTrue(any(e.name=='retry' for s in spans for e in s.events))

    def test_blocked_exporter_queue_worker_and_caller_wait_are_bounded(self):
        entered, release = threading.Event(), threading.Event()
        class Stalled:
            def export(self, spans):
                entered.set()
                release.wait(5)
        backend = t.Backend(Stalled())
        self.addCleanup(backend.processor.shutdown)
        self.addCleanup(release.set)
        @t.pipeline_trace
        def run():
            for _ in range(t.QUEUE_SIZE + 50):
                with t.span('scout'): pass
            return 'unchanged'
        worker = backend.processor.worker
        with patch.object(t, '_get_backend', return_value=backend):
            for _ in range(3):
                started = time.monotonic()
                self.assertEqual(run(), 'unchanged')
                self.assertLess(time.monotonic()-started, .7)
                self.assertLessEqual(backend.processor.queue.qsize(), t.QUEUE_SIZE)
                self.assertIs(backend.processor.worker, worker)
        self.assertTrue(entered.is_set())
        self.assertGreater(backend.processor.dropped, 0)
        started = time.monotonic()
        backend.processor.shutdown()
        self.assertLess(time.monotonic()-started, .5)

    def test_pipeline_rescout_outputs_and_database_equal_when_traced(self):
        from paper_agents.scout import ScoutCandidate
        class Source:
            name = 'arxiv'
            def fetch(self, *args, **kwargs):
                return [ScoutCandidate(source='arxiv', source_id='fixture', title='PRIVATE_TITLE',
                    abstract='PRIVATE_ABSTRACT', authors=[], published='2026-09-01', updated=None,
                    url='http://PRIVATE', pdf_url=None, categories=[], metadata={})]
        outputs, snapshots = [], []
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'state.db'
            for backend in (None, self.backend):
                if path.exists(): path.unlink()
                with patch.object(t, '_get_backend', return_value=backend), patch('paper_agents.pipeline.create_scout_source', return_value=Source()), patch('paper_agents.pipeline.load_profile', return_value={'interests':[]}), patch('urllib.request.urlopen', return_value=io.BytesIO(b'{"response":"{}"}')):
                    output = run_daily_pipeline(topics=['PRIVATE'], db_path=path, fetch_limit=1,
                                                max_scout_attempts=2, min_quality_score=100, request_delay=0)
                # Times are inherently nondeterministic; compare all other outcome fields.
                def stable(value):
                    if isinstance(value, dict):
                        return {k:stable(v) for k,v in value.items() if k not in {'created_at','updated_at','started_at','completed_at','captured_at','wall_clock_sec','first_discovered_at','last_discovered_at'}}
                    if isinstance(value,list): return [stable(v) for v in value]
                    return value
                outputs.append(stable(output))
                with sqlite3.connect(path) as connection:
                    snapshot = {}
                    for table in ('papers','scout_runs','scout_candidates','curator_runs','curator_evaluations','recommendations','profile_versions'):
                        cursor = connection.execute(f'SELECT * FROM {table} ORDER BY id')
                        columns = [item[0] for item in cursor.description]
                        rows = []
                        for row in cursor.fetchall():
                            values = dict(zip(columns, row))
                            for key, value in values.items():
                                if key.endswith('_json') and value:
                                    values[key] = json.loads(value)
                            rows.append(stable(values))
                        snapshot[table] = rows
                    snapshots.append(snapshot)
                connection.close()
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(snapshots[0], snapshots[1])
        spans = self.spans()
        self.assertEqual(len([s for s in spans if s.parent is None]), 1)
        self.assertEqual(len([s for s in spans if s.name=='scout']), 2)
        self.assertEqual(len([s for s in spans if s.name=='curator']), 2)
        self.assertEqual(len([s for s in spans if s.name=='reviewer']), 1)
        self.assertNotIn(b'PRIVATE', encode_spans(spans).SerializeToString())

    def test_otlp_http_wire_encoding_and_content_type(self):
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                received.append((self.path, self.headers['Content-Type'], body))
                self.send_response(200)
                self.end_headers()
            def log_message(self, *args): pass
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            @t.pipeline_trace
            def run():
                with t.span('scout', fetched_count=1): pass
            run()
            exporter = t.HTTPExporter(f'http://127.0.0.1:{server.server_port}/v1/traces')
            self.assertTrue(exporter.export(self.spans()))
            path, content_type, body = received[0]
            self.assertEqual(path, '/v1/traces')
            self.assertEqual(content_type, 'application/x-protobuf')
            decoded = ExportTraceServiceRequest.FromString(body)
            names = [span.name for resource in decoded.resource_spans for scope in resource.scope_spans for span in scope.spans]
            self.assertEqual(set(names), {'pipeline.daily', 'scout'})
        finally:
            server.shutdown()
            thread.join(timeout=1)
            server.server_close()


if __name__ == '__main__':
    unittest.main()

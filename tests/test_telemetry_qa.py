"""Independent telemetry acceptance; all model/export inputs are synthetic."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from paper_agents import telemetry as t
from paper_agents.local_extract import extract_chunks

try:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
    HAVE_SDK = True
except ImportError:
    HAVE_SDK = False


@unittest.skipUnless(HAVE_SDK, "optional telemetry SDK required")
class IndependentTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.exporter = InMemorySpanExporter()
        self.backend = t.Backend(self.exporter)
        self.addCleanup(self.backend.processor.shutdown)
        override = patch.object(t, "_get_backend", return_value=self.backend)
        override.start()
        self.addCleanup(override.stop)

    def test_concurrent_workflows_and_chunk_threads_never_share_parent_context(self):
        barrier = threading.Barrier(2)

        @t.pipeline_trace
        def run(cycle):
            t.attributes(workflow_cycle_id=cycle)
            barrier.wait(timeout=2)
            with t.span("reviewer", workflow_cycle_id=cycle):
                return extract_chunks("http://unused/PRIVATE", "fixture", ["PRIVATE_A", "PRIVATE_B"], 1, workers=2)

        payload = json.dumps({"response": "{}", "eval_count": 0}).encode()
        with patch("urllib.request.urlopen", side_effect=lambda *a, **k: io.BytesIO(payload)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(run, (101, 202)))
        self.assertEqual([len(result) for result in results], [2, 2])
        self.assertTrue(self.backend.processor.force_flush())
        spans = self.exporter.get_finished_spans()
        roots = [span for span in spans if span.parent is None]
        self.assertEqual(len(roots), 2)
        self.assertEqual(len({span.context.trace_id for span in roots}), 2)
        by_id = {span.context.span_id: span for span in spans}
        for span in spans:
            if span.parent:
                self.assertEqual(by_id[span.parent.span_id].context.trace_id, span.context.trace_id)
        for root in roots:
            descendants = [span for span in spans if span.context.trace_id == root.context.trace_id]
            self.assertEqual(len([span for span in descendants if span.name == "ollama.generate"]), 2)
            reviewer = next(span for span in descendants if span.name == "reviewer")
            self.assertEqual(reviewer.attributes["paper.workflow_cycle_id"], root.attributes["paper.workflow_cycle_id"])
        self.assertNotIn(b"PRIVATE", encode_spans(spans).SerializeToString())

    def test_span_start_failure_does_not_mask_application_result_or_exception(self):
        original = ValueError("PRIVATE_APPLICATION")

        @t.pipeline_trace
        def run(fail):
            with t.span("scout"):
                if fail:
                    raise original
                return {"business": "unchanged"}

        with patch.object(self.backend.tracer, "start_as_current_span", side_effect=RuntimeError("PRIVATE_EXPORT")):
            self.assertEqual(run(False), {"business": "unchanged"})
            with self.assertRaises(ValueError) as caught:
                run(True)
            self.assertIs(caught.exception, original)

    def test_export_exception_drops_batch_and_next_workflow_recovers(self):
        exporter = self.exporter
        entered = threading.Event()

        class FailsOnce:
            calls = 0

            def export(self, spans):
                self.calls += 1
                if self.calls == 1:
                    entered.set()
                    raise RuntimeError("PRIVATE_EXPORT_URL")
                return exporter.export(spans)

        backend = t.Backend(FailsOnce())
        self.addCleanup(backend.processor.shutdown)

        @t.pipeline_trace
        def run():
            return "unchanged"

        with patch.object(t, "_get_backend", return_value=backend):
            self.assertEqual(run(), "unchanged")
            self.assertTrue(entered.wait(1))
            self.assertEqual(run(), "unchanged")
        self.assertEqual(backend.processor.export_failures, 1)
        self.assertEqual(len(exporter.get_finished_spans()), 1)

    def test_many_workflows_behind_stuck_exporter_keep_one_worker_and_bounded_queue(self):
        release, entered = threading.Event(), threading.Event()

        class Blocked:
            def export(self, spans):
                entered.set()
                release.wait(10)

        backend = t.Backend(Blocked())
        self.addCleanup(backend.processor.shutdown)
        self.addCleanup(release.set)
        worker = backend.processor.worker

        @t.pipeline_trace
        def run():
            for _ in range(100):
                with t.span("scout"):
                    pass
            return 17

        with patch.object(t, "_get_backend", return_value=backend):
            for _ in range(6):
                started = time.monotonic()
                self.assertEqual(run(), 17)
                self.assertLess(time.monotonic() - started, .65)
                self.assertLessEqual(backend.processor.queue.qsize(), t.QUEUE_SIZE)
                self.assertIs(backend.processor.worker, worker)
        self.assertTrue(entered.is_set())
        self.assertGreater(backend.processor.dropped, 0)
        started = time.monotonic()
        backend.processor.shutdown()
        self.assertLess(time.monotonic() - started, .5)

    def test_recommended_paper_and_review_rows_match_with_tracing_off_on_and_export_failure(self):
        from paper_agents.pipeline import run_daily_pipeline
        from paper_agents.scout import ScoutCandidate

        class Source:
            name = "arxiv"

            def fetch(self, *args, **kwargs):
                return [ScoutCandidate(source="arxiv", source_id="qa-paper", title="PRIVATE_TITLE",
                    abstract="PRIVATE_ABSTRACT applied incident response evaluation", authors=[],
                    published="2026-09-01", updated=None, url="http://unused/PRIVATE_URL",
                    pdf_url=None, categories=[], metadata={})]

        class FailedExport:
            def export(self, spans):
                raise RuntimeError("PRIVATE_EXPORT")

        failed_backend = t.Backend(FailedExport())
        self.addCleanup(failed_backend.processor.shutdown)
        clocks = {"created_at", "updated_at", "started_at", "completed_at", "captured_at",
                  "wall_clock_sec", "first_discovered_at", "last_discovered_at"}

        def stable(value):
            if isinstance(value, dict):
                return {key: stable(item) for key, item in value.items() if key not in clocks}
            if isinstance(value, list):
                return [stable(item) for item in value]
            return value

        outputs, snapshots = [], []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "state.db"
            for backend in (None, self.backend, failed_backend):
                path.unlink(missing_ok=True)
                with patch.object(t, "_get_backend", return_value=backend), \
                     patch("paper_agents.pipeline.create_scout_source", return_value=Source()), \
                     patch("paper_agents.pipeline.load_profile", return_value={"interests": []}), \
                     patch("paper_agents.reviewer_agent.DEFAULT_EXTRACTION_DIR", root / "extractions"), \
                     patch("paper_agents.reviewer_agent.download_pdf_for_recommendation", return_value=None), \
                     patch("urllib.request.urlopen", side_effect=lambda *a, **k: io.BytesIO(b'{"response":"{}"}')):
                    result = run_daily_pipeline(topics=["PRIVATE_TOPIC"], db_path=path,
                        min_quality_score=0, max_scout_attempts=1, keep_limit=1,
                        pdf_dir=root / "pdf", scout_dir=root / "scout", request_delay=0)
                self.assertEqual(len(result["curator"]["recommendations"]), 1)
                self.assertEqual(len(result["cards"]), 1)
                outputs.append(stable(result))
                con = sqlite3.connect(path)
                try:
                    snapshot = {}
                    for table in ("workflow_cycles", "papers", "scout_runs", "scout_candidates",
                                  "curator_runs", "curator_evaluations", "recommendations", "profile_versions", "artifacts"):
                        cursor = con.execute(f"SELECT * FROM {table} ORDER BY id")
                        columns = [item[0] for item in cursor.description]
                        rows = []
                        for row in cursor.fetchall():
                            values = dict(zip(columns, row))
                            for key, value in values.items():
                                if key.endswith("_json") and value:
                                    values[key] = json.loads(value)
                            rows.append(stable(values))
                        snapshot[table] = rows
                    snapshots.append(snapshot)
                finally:
                    con.close()
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0], outputs[2])
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(snapshots[0], snapshots[2])
        spans = self.exporter.get_finished_spans()
        self.assertTrue(any(span.name == "reviewer.extract" for span in spans))
        self.assertNotIn(b"PRIVATE", encode_spans(spans).SerializeToString())

    def test_http_receiver_that_never_sends_headers_does_not_hold_pipeline(self):
        entered, release = threading.Event(), threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                entered.set()
                release.wait(3)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        backend = t.Backend(t.HTTPExporter(f"http://127.0.0.1:{server.server_port}/v1/traces"))
        try:
            @t.pipeline_trace
            def run():
                return {"business": "unchanged"}

            started = time.monotonic()
            with patch.object(t, "_get_backend", return_value=backend):
                self.assertEqual(run(), {"business": "unchanged"})
            self.assertLess(time.monotonic() - started, .65)
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            backend.processor.shutdown()
            self.assertLess(time.monotonic() - started, .5)
        finally:
            release.set()
            server.shutdown()
            worker.join(timeout=1)
            server.server_close()
            backend.processor.shutdown()

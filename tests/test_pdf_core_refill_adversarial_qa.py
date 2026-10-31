"""Offline acceptance boundaries for the combined PDF/CORE change."""
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import sqlite3
import threading
import unittest
from unittest.mock import patch
import urllib.error

from paper_agents import db
from paper_agents.core_progress import CoreProgress
from paper_agents.reviewer_agent import fetch_pdf_bytes, normalize_pdf_url
from tests import test_core_progress as fixture


class PDFBoundaryQA(unittest.TestCase):
    def test_existing_percent_escapes_reserved_query_order_and_normalization_are_idempotent(self):
        for suffix in ('?a=1&a=2&empty=&bare', '?sig=%2f%2F%25%3D+&x=a/b?c=d', '?', '?#fragment'):
            raw = 'https://example.org/a%2Fb%20c.pdf'+suffix
            with self.subTest(url=raw):
                self.assertEqual(normalize_pdf_url(raw), raw)
                self.assertEqual(normalize_pdf_url(normalize_pdf_url(raw)), raw)

    def test_protocol_failure_during_body_read_returns_to_fallback(self):
        class Broken:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): raise IncompleteRead(b'partial')
        with patch('paper_agents.reviewer_agent.urllib.request.urlopen', return_value=Broken()):
            self.assertIsNone(fetch_pdf_bytes('https://example.org/a.pdf'))

    def test_html_cycle_does_not_prevent_later_valid_link(self):
        calls = []
        class Response:
            def __init__(self, url):
                self.url = url
                self.headers = {'content-type':'application/pdf' if url.endswith('valid.pdf') else 'text/html'}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def geturl(self): return self.url
            def read(self):
                if self.url.endswith('valid.pdf'): return b'%PDF-1.7 fixture'
                if self.url.endswith('root.pdf'): return b'<a href="child.pdf">child</a><a href="valid.pdf">valid</a>'
                return b'<a href="root.pdf">cycle</a>'
        def opened(request, **kwargs):
            calls.append(request.full_url)
            return Response(request.full_url)
        with patch('paper_agents.reviewer_agent.urllib.request.urlopen', side_effect=opened):
            self.assertEqual(fetch_pdf_bytes('https://example.org/root.pdf'), b'%PDF-1.7 fixture')
        self.assertEqual(calls, ['https://example.org/root.pdf','https://example.org/child.pdf','https://example.org/valid.pdf'])

    def test_html_chain_stops_after_four_distinct_network_requests(self):
        calls = []
        class Response:
            headers = {'content-type':'text/html'}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def geturl(self): return calls[-1]
            def read(self): return ('<a href="'+str(len(calls))+'.pdf">next</a>').encode()
        def opened(request, **kwargs):
            calls.append(request.full_url)
            return Response()
        with patch('paper_agents.reviewer_agent.urllib.request.urlopen', side_effect=opened):
            self.assertIsNone(fetch_pdf_bytes('https://example.org/start.pdf'))
        self.assertEqual(len(calls), 4)


class PDFLocalHTTPQA(unittest.TestCase):
    def setUp(self):
        self.paths=[]
        paths=self.paths
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                paths.append(self.path)
                self.send_response(200)
                self.send_header('Content-Type','application/pdf')
                self.send_header('Content-Length','100' if self.path=='/truncated.pdf' else '16')
                self.end_headers()
                self.wfile.write(b'%PDF-1.7 fixture')
                self.close_connection=True
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.url='http://127.0.0.1:'+str(self.server.server_port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_actual_http_selector_encodes_raw_text_and_preserves_signed_query_bytes(self):
        self.assertEqual(fetch_pdf_bytes(self.url+'/étude cloud%20one.pdf?sig=a%2fb%3D+&part=1&part=2'),
                         b'%PDF-1.7 fixture')
        self.assertEqual(self.paths,['/%C3%A9tude%20cloud%20one.pdf?sig=a%2fb%3D+&part=1&part=2'])

    def test_actual_truncated_pdf_body_is_an_isolated_failure(self):
        self.assertIsNone(fetch_pdf_bytes(self.url+'/truncated.pdf'))
        self.assertEqual(fetch_pdf_bytes(self.url+'/good.pdf'),b'%PDF-1.7 fixture')


class CoreBudgetAtomicityQA(unittest.TestCase):
    setUp = fixture.CoreProgressTests.setUp
    tearDown = fixture.CoreProgressTests.tearDown
    progress = fixture.CoreProgressTests.progress
    fetch = fixture.CoreProgressTests.fetch
    save = fixture.CoreProgressTests.save
    run_scout = fixture.CoreProgressTests.run_scout

    def test_refill_retry_uses_shared_three_attempts_and_deferred_query_rotates(self):
        from paper_agents.scout import core_work_to_candidate
        db.upsert_paper(self.connection, core_work_to_candidate(fixture.work(1)).as_dict())
        self.connection.commit()
        progress = self.progress()
        transient = urllib.error.HTTPError('url',503,'temporary',{},io.BytesIO())
        candidates, fetched = self.fetch(progress, [fixture.page([fixture.work(1)],next_offset=1),
            transient, fixture.page([fixture.work(2)],offset=1,next_offset=2)], topics=['a','b','c'])
        self.assertEqual([(c.args[0],c.kwargs['offset']) for c in fetched.call_args_list], [('a',0),('a',1),('a',1)])
        self.assertEqual(progress.diagnostics['attempts'],3)
        self.assertEqual(len(candidates),1)
        self.assertEqual(progress.diagnostics['deferred_queries'],['b','c'])
        self.save(progress)
        later=self.progress()
        _, fetched=self.fetch(later,[fixture.page([fixture.work(3)],next_offset=None)],topics=['a','b','c'],maximum=1)
        self.assertEqual(fetched.call_args.args[0],'b')

    def test_separate_sqlite_writer_can_acquire_lock_during_each_refill_request(self):
        from paper_agents.scout import core_work_to_candidate
        db.upsert_paper(self.connection,core_work_to_candidate(fixture.work(1)).as_dict())
        self.connection.commit()
        progress=self.progress()
        def response(topic, **kwargs):
            other=sqlite3.connect(self.path,timeout=0)
            try:
                other.execute('BEGIN IMMEDIATE')
                other.rollback()
            finally: other.close()
            return fixture.page([fixture.work(1 if kwargs['offset']==0 else 2)],
                                offset=kwargs['offset'],next_offset=kwargs['offset']+1)
        with patch.object(self.source,'fetch_page',side_effect=response) as opened:
            candidates=progress.fetch(['a'],freshness_months=24,max_candidates=1,errors=[])
        self.assertEqual(opened.call_count,2)
        self.assertEqual(len(candidates),1)

    def test_failure_after_checkpoint_sql_rolls_back_papers_and_checkpoint_together(self):
        original=CoreProgress.checkpoint
        def failed(progress, scout_run_id):
            original(progress,scout_run_id)
            raise RuntimeError('QA after checkpoint writes')
        with patch.object(CoreProgress,'checkpoint',failed):
            with self.assertRaisesRegex(RuntimeError,'after checkpoint'):
                self.run_scout([fixture.page([fixture.work(2)],next_offset=1)])
        for table in ('papers','scout_candidates','core_search_state','core_page_dispositions'):
            with self.subTest(table=table):
                self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)

    def test_insufficient_remaining_deadline_stops_before_a_second_request(self):
        progress=self.progress()
        self.source.timeout=10
        # Start has 30s; first request admitted at t=0; second cannot fit at t=21.
        with patch('paper_agents.core_progress.time.monotonic',side_effect=[0,0,21]):
            _, opened=self.fetch(progress,[fixture.page([fixture.work(2)],next_offset=1)],topics=['a','b'])
        self.assertEqual(opened.call_count,1)
        self.assertEqual(progress.diagnostics['stop_reason'],'time_budget_reached')


if __name__=='__main__':
    unittest.main()

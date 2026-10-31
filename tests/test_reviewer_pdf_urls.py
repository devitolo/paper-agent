from http.client import IncompleteRead, InvalidURL
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_agents import db
from paper_agents.reviewer_agent import ReviewerAgent, ReviewerConfig, fetch_pdf_bytes, normalize_pdf_url


class Response:
    headers = {'content-type': 'application/pdf'}
    def __init__(self, url): self.url = url
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return b'%PDF-1.7 test'
    def geturl(self): return self.url


class ReviewerPDFURLTests(unittest.TestCase):
    def test_publisher_spaces_unicode_and_existing_escapes(self):
        raw = 'https://publisher.example/get/IJEMT/VOL. 11 NO. 1 2025/Standardization in Cloud 139-159.pdf'
        with patch('paper_agents.reviewer_agent.urllib.request.urlopen', side_effect=lambda request, **kw: Response(request.full_url)) as opened:
            self.assertTrue(fetch_pdf_bytes(raw).startswith(b'%PDF'))
        self.assertEqual(opened.call_args.args[0].full_url, raw.replace(' ', '%20'))
        self.assertEqual(normalize_pdf_url('https://münich.example/étude%20one.pdf?title=été+cloud&sig=a%2Fb%3D'),
                         'https://xn--mnich-kva.example/%C3%A9tude%20one.pdf?title=%C3%A9t%C3%A9+cloud&sig=a%2Fb%3D')

    def test_signed_query_is_not_parsed_reordered_or_double_encoded(self):
        url = 'https://example.org/a%2fb.pdf?X-Signature=a%2fb%3D&part=1&part=2&name=a+b&empty=&bare'
        self.assertEqual(normalize_pdf_url(url), url)
        self.assertEqual(normalize_pdf_url('https://example.org/a.pdf?#section'), 'https://example.org/a.pdf?#section')

    def test_malformed_html_link_and_self_link_do_not_block_valid_fallback(self):
        class HTML(Response):
            headers = {'content-type':'text/html'}
            def read(self):
                return b'<a href="https://[bad/a.pdf">bad</a><a href="/page.pdf">loop</a><a href="/valid paper.pdf">good</a>'
        def response(request, **kwargs):
            return HTML(request.full_url) if request.full_url.endswith('/page.pdf') else Response(request.full_url)
        with patch('paper_agents.reviewer_agent.urllib.request.urlopen', side_effect=response) as opened:
            self.assertTrue(fetch_pdf_bytes('https://example.org/page.pdf').startswith(b'%PDF'))
        self.assertEqual(opened.call_count, 2)

    def test_malformed_and_unsafe_urls_do_not_reach_transport(self):
        for url in ('https://[bad/a.pdf', 'https://example.org:bad/a.pdf', 'https://example.org/a\nb.pdf',
                    'https://example.org/a\tb.pdf', 'file:///etc/private.pdf', 'ftp://example.org/a.pdf',
                    'https://user:password@example.org/a.pdf', 'https:///a.pdf'):
            with self.subTest(url=url), patch('paper_agents.reviewer_agent.urllib.request.urlopen') as opened:
                self.assertIsNone(fetch_pdf_bytes(url))
                opened.assert_not_called()

    def test_expected_http_failures_are_isolated_but_programming_errors_propagate(self):
        for error in (InvalidURL('bad publisher URL'), IncompleteRead(b'partial'), OSError('unavailable')):
            with self.subTest(error=type(error).__name__), patch('paper_agents.reviewer_agent.urllib.request.urlopen', side_effect=error):
                self.assertIsNone(fetch_pdf_bytes('https://example.org/paper.pdf'))
        with patch('paper_agents.reviewer_agent.urllib.request.urlopen', side_effect=RuntimeError('programming bug')):
            with self.assertRaisesRegex(RuntimeError, 'programming bug'):
                fetch_pdf_bytes('https://example.org/paper.pdf')

    def test_bad_paper_uses_abstract_or_explicit_skip_and_next_paper_is_reviewed(self):
        for abstract in ('Source evidence for abstract-only triage.', ''):
            with self.subTest(abstract=bool(abstract)), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); path = root/'data.db'; db.init_db(path)
                connection = db.connect_db(path)
                self.addCleanup(connection.close)
                recommendations = []
                for index, url in enumerate(('https://[bad/a.pdf', 'https://example.org/good paper.pdf')):
                    paper_id, _ = db.upsert_paper(connection, {'source':'openalex', 'source_id':str(index), 'title':f'Paper {index}'})
                    recommendations.append({'paper_id':paper_id,'source':'openalex','source_id':str(index),
                        'title':f'Paper {index}','pdf_url':url,'abstract':abstract if index == 0 else ''})
                connection.commit()
                with patch('paper_agents.reviewer_agent.DEFAULT_EXTRACTION_DIR', root/'extract'), \
                     patch('paper_agents.reviewer_agent.output_path_for', side_effect=lambda pdf, model: root/'extract'/(pdf.stem+'.json')), \
                     patch('paper_agents.reviewer_agent.extract_paper', return_value={'merged':{'research_problem':'Evidence'}}), \
                     patch('paper_agents.reviewer_agent.urllib.request.urlopen', side_effect=lambda request, **kw: Response(request.full_url)):
                    result = ReviewerAgent().run(connection, recommendations=recommendations, config=ReviewerConfig(pdf_dir=root/'pdf'))
                self.assertEqual(result['reviewed_count'], 2)
                self.assertEqual([card['paper_id'] for card in result['cards']], [row['paper_id'] for row in recommendations])
                self.assertTrue((root/'pdf/openalex/1.pdf').is_file())
                if abstract:
                    row = connection.execute("SELECT metadata_json FROM artifacts WHERE paper_id=? AND artifact_type='triage_summary'", (recommendations[0]['paper_id'],)).fetchone()
                    self.assertIn('"abstract_only": true', row[0])
                else:
                    self.assertIn('no source abstract', result['cards'][0]['error'])

"""Independent Health UI contracts using disposable SQLite, logs and HTTP only."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from paper_agents import db, job_events, web


class HealthDOM(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.headings = []; self.links = []; self.inputs = []; self.tables = []
        self.tags = []; self.definitions = []
        self.heading = None; self.heading_text = []; self.latest_heading = ''
        self.table = None; self.row = None; self.cell = None
        self.definition = None; self.term = ''
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs); self.tags.append(tag)
        if tag in ('h1', 'h2', 'h3'):
            self.heading = tag; self.heading_text = []
        if tag == 'a': self.links.append(attrs)
        if tag == 'input': self.inputs.append(attrs)
        if tag == 'table': self.table = {'heading': self.latest_heading, 'rows': []}
        if tag == 'tr': self.row = []
        if tag in ('th', 'td'): self.cell = []
        if tag in ('dt', 'dd'): self.definition = (tag, [])

    def handle_data(self, data):
        if self.heading: self.heading_text.append(data)
        if self.cell is not None: self.cell.append(data)
        if self.definition is not None: self.definition[1].append(data)

    def handle_endtag(self, tag):
        if tag == self.heading:
            text = ' '.join(''.join(self.heading_text).split())
            self.headings.append((tag, text)); self.latest_heading = text; self.heading = None
        if tag in ('th', 'td') and self.cell is not None:
            self.row.append(''.join(self.cell)); self.cell = None
        if tag == 'tr' and self.row is not None:
            self.table['rows'].append(self.row); self.row = None
        if tag == 'table' and self.table is not None:
            self.tables.append(self.table); self.table = None
        if self.definition is not None and tag == self.definition[0]:
            text = ''.join(self.definition[1])
            if tag == 'dt': self.term = text
            else: self.definitions.append((self.term, text))
            self.definition = None

    def rows(self, title='Operational job history'):
        table = next(t for t in self.tables if t['heading'] == title)
        return [dict(zip(table['rows'][0], row)) for row in table['rows'][1:]]

    def h2(self):
        return [text for tag, text in self.headings if tag == 'h2']


class HealthRedesignQA(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.path = self.root / 'paper_agent.db'
        self.status = self.root / 'orchestrator.json'
        db.init_db(self.path)
        env = patch.dict(os.environ, {'PAPER_AGENT_ORCHESTRATOR_STATUS_FILE': str(self.status),
            'PAPER_AGENT_JOB_EVENT_FILE': str(self.root / 'job-events.jsonl')})
        env.start(); self.addCleanup(env.stop)
        self.sync_status()

    def sync_status(self, *, age_hours=0, result='completed', **extra):
        self.status.write_text(json.dumps({'overall_result': result,
            'updated_at': (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat(),
            'input_revision': 'a' * 64, **extra}))

    def seed(self, source='openalex', *, candidates=False):
        with closing(db.connect_db(self.path)) as conn, conn:
            cycle = conn.execute("INSERT INTO workflow_cycles(state,mode) VALUES('complete','qa')").lastrowid
            run = conn.execute('INSERT INTO scout_runs(workflow_cycle_id,source,target_candidates,max_candidates,freshness_months) VALUES(?,?,20,30,6)', (cycle, source)).lastrowid
            if candidates:
                paper = conn.execute("INSERT INTO papers(canonical_key,title) VALUES('qa-paper','QA Health paper')").lastrowid
                conn.execute('INSERT INTO paper_sources(paper_id,source,source_id) VALUES(?,?,?)', (paper, source, 'qa-source'))
                conn.execute('INSERT INTO scout_candidates(scout_run_id,paper_id,retrieval_order) VALUES(?,?,1)', (run, paper))
            return run

    def profile_backlog(self):
        with closing(db.connect_db(self.path)) as conn, conn:
            raw = conn.execute("INSERT INTO raw_feedback(content,content_hash) VALUES('QA feedback','qa-feedback')").lastrowid
            parse = conn.execute("INSERT INTO feedback_parse_attempts(raw_feedback_id,parser_name,parser_version,status) VALUES(?,'qa','1','succeeded')", (raw,)).lastrowid
            conn.execute('INSERT INTO structured_feedback(parse_attempt_id,score) VALUES(?,5)', (parse,))

    def page(self, **kwargs):
        return web.render_health_page(self.path, **kwargs)

    def test_jobs_is_default_and_invalid_tab_falls_back_without_analytics(self):
        for tab in (None, 'unknown', 'attention', '<script>bad</script>'):
            with self.subTest(tab=tab):
                dom = HealthDOM(self.page(**({'tab': tab} if tab is not None else {})))
                self.assertIn('Jobs', dom.h2())
                self.assertEqual(dom.h2().count('Needs Attention'), 1)
                self.assertNotIn('Pipeline analytics', dom.h2())
                self.assertNotIn('Infrastructure', dom.h2())
                active = [a for a in dom.links if 'health-tab' in a.get('class', '').split() and 'active' in a.get('class', '').split()]
                self.assertEqual(len(active), 1)
                self.assertEqual(parse_qs(urlparse(active[0]['href']).query)['tab'], ['jobs'])

    def test_real_warning_profile_backlog_and_sync_issue_share_one_attention_section(self):
        self.seed(); self.profile_backlog(); self.sync_status(result='failed', reason='QA delivery failed')
        dom = HealthDOM(self.page())
        self.assertEqual(dom.h2().count('Needs Attention'), 1)
        self.assertNotIn('Warnings', dom.h2())
        self.assertNotIn('Profile maintenance / Needs attention', dom.h2())
        items = web.render_needs_attention_items(db.health_summary(self.path))
        self.assertTrue({'processing', 'profile', 'delivery'} <= {item['domain'] for item in items})

    def test_infrastructure_omits_job_history_and_labels_every_synthetic_resource(self):
        job_events.record('semantic', 'completed', 'QA job message should stay in Jobs')
        html = self.page(tab='infrastructure'); dom = HealthDOM(html)
        self.assertIn('Infrastructure', dom.h2())
        self.assertNotIn('QA job message should stay in Jobs', html)
        self.assertNotIn('Operational job history', [t['heading'] for t in dom.tables])
        self.assertIn('PLACEHOLDER TELEMETRY', html)
        for name, value in dom.definitions:
            if name.startswith(('Mini Cpu', 'Mini Memory', 'Mini Disk', 'Aws Cpu', 'Aws Disk', 'Telemetry Freshness')):
                self.assertIn('placeholder', value.lower(), name)
        self.assertEqual(dict(dom.definitions)['Database Integrity'], 'ok')

    def test_analytics_contains_real_funnel_graphs_without_operational_job_history(self):
        self.seed(candidates=True)
        dom = HealthDOM(self.page(tab='analytics'))
        self.assertIn('Pipeline analytics', dom.h2())
        self.assertIn('Daily Trend', dom.h2())
        self.assertIn('svg', dom.tags)
        self.assertIn('Workflow cycles', [t['heading'] for t in dom.tables])
        self.assertIn('Scout', [t['heading'] for t in dom.tables])
        self.assertNotIn('Operational job history', [t['heading'] for t in dom.tables])

    def test_tabs_preserve_selected_source_and_range(self):
        self.seed()
        dom = HealthDOM(self.page(tab='infrastructure', source_value='openalex', days=90))
        for link in (a for a in dom.links if 'health-tab' in a.get('class', '').split()):
            with self.subTest(href=link['href']):
                query = parse_qs(urlparse(link['href']).query)
                self.assertEqual(query.get('source'), ['openalex'])
                self.assertEqual(query.get('days'), ['90'])

    def test_range_and_source_filter_submission_keeps_selected_tab(self):
        dom = HealthDOM(self.page(tab='analytics'))
        self.assertTrue(any(i.get('name') == 'tab' and i.get('value') == 'analytics'
                            for i in dom.inputs), 'Filtering must not reset Analytics to Jobs')

    def test_rate_limit_with_key2_is_not_misdiagnosed_as_configuration_failure(self):
        message = 'semantic_scholar stopped after key2 received HTTP 429; provider/IP-wide rate limiting is likely.'
        self.assertEqual(web.classify_attention_message(message)[0], 'rate_limit')

    def test_completed_processing_does_not_turn_push_failure_or_pending_text_into_acceptance(self):
        for message in ('Processing complete; push failed', 'Processing complete; push pending',
                        'Processing complete; push skipped'):
            with self.subTest(message=message):
                job_events.record('semantic', 'completed', message)
                row = HealthDOM(self.page()).rows()[0]
                self.assertEqual(row['Processing'], 'completed')
                self.assertNotEqual(row['Delivery'], 'accepted', 'Delivery acceptance needs receipt evidence')

    def test_failure_and_started_job_states_stay_separate_from_delivery(self):
        for status in ('failed', 'started', 'skipped_busy'):
            with self.subTest(status=status):
                job_events.record('semantic', status, 'QA source job event')
                row = HealthDOM(self.page()).rows()[0]
                self.assertEqual(row['Processing'], status)
                self.assertNotEqual(row['Delivery'], 'accepted')

    def test_attention_keeps_scout_diagnostic_link_for_actual_warning(self):
        run = self.seed(source='semantic_scholar')
        summary = db.health_summary(self.path)
        self.assertTrue(any(w.get('scout_run_id') == run for w in summary['warnings']))
        links = [a.get('href') for a in HealthDOM(self.page()).links]
        self.assertIn(f'/health/scout-diagnostics/{run}', links)

    def test_failed_profile_update_item_retains_failure_severity(self):
        with patch('paper_agents.web.sync_health_snapshot', return_value={'state': 'completed'}):
            items = web.render_needs_attention_items({'warnings': [], 'profile_maintenance': [
                {'level': 'critical', 'message': 'QA profile integrity failure'}]})
        self.assertEqual(items[0]['severity'], 'critical')

    def test_stale_delivery_cannot_be_presented_as_current_completed_sync(self):
        self.sync_status(age_hours=72)
        items = web.render_needs_attention_items(db.health_summary(self.path))
        delivery = next(i for i in items if i['domain'] == 'delivery')
        self.assertEqual(delivery['state'], 'stale')
        self.assertIn('stale', self.page())

    def test_completed_status_without_timestamp_does_not_suppress_delivery_attention(self):
        self.status.write_text(json.dumps({'overall_result': 'completed', 'input_revision': 'a' * 64}))
        items = web.render_needs_attention_items(db.health_summary(self.path))
        self.assertTrue(any(i['domain'] == 'delivery' for i in items),
                        'A status without freshness evidence cannot establish current delivery health')

    def test_unavailable_or_malformed_sync_keeps_health_page_usable(self):
        for value in (None, 'not-json', '[]', '{"updated_at":"bad","overall_result":"completed"}'):
            with self.subTest(value=value):
                if value is None: self.status.unlink(missing_ok=True)
                else: self.status.write_text(value)
                dom = HealthDOM(self.page())
                self.assertIn('Jobs', dom.h2())
                self.assertEqual(dom.h2().count('Needs Attention'), 1)
                items = web.render_needs_attention_items(db.health_summary(self.path))
                self.assertTrue(any(i['domain'] == 'delivery' for i in items))

    def test_job_messages_are_escaped_and_malformed_event_lines_are_ignored(self):
        message = 'QA <script>window.bad=true</script> & job evidence'
        job_events.record('semantic', 'failed', message)
        with (self.root / 'job-events.jsonl').open('a') as stream:
            stream.write('bad-json\n[]\n')
        html = self.page(); row = HealthDOM(html).rows()[0]
        self.assertEqual(row['Message'], message)
        self.assertNotIn('<script>window.bad=true</script>', html)

    def test_rendering_all_tabs_preserves_database_rows_and_status_files(self):
        self.seed(candidates=True); self.profile_backlog()
        with closing(db.connect_db(self.path)) as conn:
            before = '\n'.join(conn.iterdump())
        status = self.status.read_bytes()
        for tab in ('jobs', 'infrastructure', 'analytics'): self.page(tab=tab)
        with closing(db.connect_db(self.path)) as conn:
            self.assertEqual('\n'.join(conn.iterdump()), before)
        self.assertEqual(self.status.read_bytes(), status)

    def test_http_health_route_honors_tabs_and_attention_api(self):
        self.seed()
        server = ThreadingHTTPServer(('127.0.0.1', 0), web.make_handler(self.path))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            for tab, heading in (('jobs', 'Jobs'), ('infrastructure', 'Infrastructure'), ('analytics', 'Pipeline analytics')):
                with self.subTest(tab=tab):
                    with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/health?tab={tab}&days=90&source=openalex', timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        self.assertIn(heading, HealthDOM(response.read().decode()).h2())
            with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/health/needs-attention', timeout=5) as response:
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertTrue(json.load(response)['items'])
        finally:
            server.shutdown(); server.server_close(); thread.join(3)

"""Independent Mini CLI/export -> exact AWS HTTPS receiver -> Health checks.

QA_HEALTH_AWS_SOURCE must name a separately frozen AWS checkout for mixed-role
acceptance. Providers/Docker are fixture boundaries; auth, export, import,
SQLite, receipt handling and served Health HTML are real. No production access.
"""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import select
import sqlite3
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from paper_agents import operational_evidence, sync_exchange as exchange
from paper_agents.sync_worker import request
from paper_agents.whole_cycle_exchange import export_delta, push
from tests import test_health_redesign_qa as html_fixture
from tests.sync_exchange import test_whole_cycle as fixture


SERVER = r'''
import ssl,sys
from pathlib import Path
from http.server import ThreadingHTTPServer
from paper_agents.web import make_handler
server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(Path(sys.argv[1]),sync_root=Path(sys.argv[2])))
context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(sys.argv[3],sys.argv[4])
server.socket=context.wrap_socket(server.socket,server_side=True)
print(server.server_port,flush=True)
server.serve_forever()
'''


class OperationalHealthE2EQA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = os.environ.get('QA_HEALTH_AWS_SOURCE')
        if not source:
            raise RuntimeError('Set QA_HEALTH_AWS_SOURCE to the exact AWS receiver checkout; '
                               'the Mini checkout is not an AWS receiver substitute')
        cls.aws_source = Path(source).resolve()
        if not (cls.aws_source/'paper_agents/operational_evidence.py').is_file():
            raise RuntimeError('QA_HEALTH_AWS_SOURCE is not a receiver source checkout: '+str(cls.aws_source))

    def setUp(self):
        self.t = fixture.WholeCycleTests()
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        self.t.cli_env['PAPER_AGENT_ROUTINE_SYNC'] = '1'
        aws_source = self.aws_source
        # Match the deployed app root: schema is present beside private data.
        # Imports use the frozen source, while runtime artifacts stay in fixture.
        (self.t.aws/'sql').symlink_to(aws_source/'sql', target_is_directory=True)
        env = dict(os.environ, PYTHONPATH=str(aws_source), PYTHONDONTWRITEBYTECODE='1',
                   PAPER_AGENT_ORCHESTRATOR_STATUS_FILE=str(self.t.root/'missing-orchestrator'),
                   PAPER_AGENT_MINI_RESOURCE_STATUS_FILE=str(self.t.root/'missing-mini'),
                   PAPER_AGENT_AWS_RESOURCE_STATUS_FILE=str(self.t.root/'missing-aws'))
        log = (self.t.root/'aws-server.log').open('w+')
        self.addCleanup(log.close)
        child = subprocess.Popen([sys.executable, '-B', '-u', '-c', SERVER,
            str(self.t.db), str(self.t.aws), str(self.t.root/'cert.pem'), str(self.t.root/'key.pem')],
            cwd=self.t.aws, env=env, stdout=subprocess.PIPE, stderr=log, text=True)
        self.addCleanup(self.stop_server, child)
        ready, _, _ = select.select([child.stdout], [], [], 15)
        self.assertTrue(ready, 'Exact AWS receiver failed to start')
        port = child.stdout.readline().strip()
        if not port.isdigit():
            log.seek(0)
            self.fail('AWS receiver startup failed: '+log.read())
        self.t.url = 'https://localhost:'+port
        self.t.cli_env['PAPER_AGENT_SYNC_AWS_URL'] = self.t.url
        self.context = ssl.create_default_context(cafile=str(self.t.root/'cert.pem'))
        try:
            self.t.shell('pull')
        except AssertionError as error:
            log.flush()
            log.seek(0)
            self.fail(str(error)+'\nSelected receiver source: '+str(aws_source)
                      +'\nAWS fixture stderr:\n'+log.read()[-6000:])
        self.raw_profile = (self.t.aws/'data/profile.json').read_bytes()

    @staticmethod
    def stop_server(child):
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()

    def seed(self, source='arxiv', *, nonzero=False, status='completed', when=None):
        when = when or datetime.now(timezone.utc).isoformat()
        if nonzero:
            self.t.stages((source,))
            with exchange.connect_db(self.t.mdb) as conn:
                run = conn.execute('SELECT MAX(id) FROM scout_runs').fetchone()[0]
                conn.execute('UPDATE scout_runs SET started_at=?,completed_at=? WHERE id=?', (when, when, run))
                conn.execute('UPDATE scout_candidates SET excluded=1 WHERE scout_run_id=? AND retrieval_order=3', (run,))
        else:
            with exchange.connect_db(self.t.mdb) as conn:
                cycle = conn.execute("INSERT INTO workflow_cycles(state,mode) VALUES('completed',?)", (source,)).lastrowid
                conn.execute('''INSERT INTO scout_runs(workflow_cycle_id,source,target_candidates,
                    max_candidates,freshness_months,started_at,completed_at) VALUES(?,?,3,3,6,?,?)''',
                    (cycle, source, when, when))
        job = 'semantic' if source == 'semantic_scholar' else source
        self.write_event(job, status, when)

    def write_event(self, job, status, when=None, message='QA harmless event'):
        path = self.t.mini/'data/job-events.jsonl'
        with path.open('a') as stream:
            stream.write(json.dumps({'recorded_at':when or datetime.now(timezone.utc).isoformat(),
                                    'job':job, 'status':status, 'message':message})+'\n')

    def html(self, tab='jobs', source='all', days=21):
        with urllib.request.urlopen(self.t.url+f'/health?tab={tab}&source={source}&days={days}',
                                    context=self.context, timeout=10) as response:
            self.assertEqual(response.status, 200)
            return response.read().decode()

    def rows(self, source='all', days=21):
        rows = html_fixture.HealthDOM(self.html(source=source, days=days)).rows()
        # Same receipt meaning across the initial and clarified column label.
        return [{**row, **({'Delivery':row['Evidence Delivery']} if 'Evidence Delivery' in row else {})}
                for row in rows]

    def published(self):
        return json.loads(next(self.t.state.glob('published-*.json')).read_text())

    def publish_legacy_export(self):
        payload = export_delta(self.t.mdb, self.t.mini, self.t.state)
        payload.pop('operational')
        cycle = json.loads((self.t.state/'active-cycle.json').read_text())
        payload.update(input_revision=cycle['input_revision'], producer_id='mini-production',
                       environment='production', run_sequence=1, batch_id=cycle['batch_id'])
        sequence = self.t.state/'sequence.json'
        if sequence.exists():
            payload['run_sequence'] = json.loads(sequence.read_text())['sequence']+1
        (self.t.state/'pipeline-outbox.json').write_text(json.dumps(payload))
        return push(self.t.mdb, self.t.mini, self.t.state, self.t.url)

    def database_dump(self):
        with exchange.connect_db(self.t.db) as conn:
            return '\n'.join(conn.iterdump())

    def assert_protected(self):
        self.assertEqual((self.t.aws/'data/profile.json').read_bytes(), self.raw_profile)
        with exchange.connect_db(self.t.db) as conn:
            self.assertEqual(conn.execute('SELECT content FROM raw_feedback').fetchone()[0], 'before pull')
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_real_export_signed_import_and_health_counts_match_mini_rows(self):
        self.seed(nonzero=True)
        self.t.shell('push')
        payload = self.published()
        run = payload['operational']['scout_runs'][0]
        self.assertEqual((run['candidate_count'], run['eligible_count'], run['excluded_count']), (3, 2, 1))
        with exchange.connect_db(self.t.db) as conn:
            body, state = conn.execute('SELECT payload_json,status FROM sync_result_batches').fetchone()
            self.assertEqual(json.loads(body)['operational'], payload['operational'])
            self.assertEqual(state, 'applied')
        row = next(row for row in self.rows('arxiv') if row['Job']=='arxiv')
        self.assertEqual((row['Processing'], row['Delivery']), ('completed', 'accepted'))
        day = datetime.fromisoformat(run['started_at']).astimezone(ZoneInfo('America/Los_Angeles')).date()
        html = self.html('analytics', 'arxiv')
        self.assertIn(f'Candidates {day}: 3', html)
        self.assertIn(f'Eligible {day}: 2', html)
        self.assert_protected()

    def test_zero_result_success_is_exported_imported_and_displayed_as_observed_zero(self):
        self.seed()
        self.t.shell('push')
        payload = self.published()
        self.assertEqual(payload['results'], [])
        self.assertTrue(all(not rows for rows in payload['normalized'].values()))
        self.assertEqual(payload['operational']['scout_runs'][0]['candidate_count'], 0)
        receipt = json.loads(next(self.t.state.glob('receipt-*.json')).read_text())
        self.assertEqual(receipt['operational_version'], 1)
        self.assertFalse((self.t.state/'pipeline-outbox.json').exists())
        row = next(row for row in self.rows() if row['Job']=='arxiv')
        self.assertEqual((row['Processing'], row['Delivery']), ('completed', 'accepted'))
        self.assertRegex(self.html('analytics'), r'Candidates [\d-]+: 0')
        self.assert_protected()

    def test_replay_and_repeated_facts_in_new_batch_do_not_double_count(self):
        self.seed()
        self.t.shell('push')
        payload = self.published()
        before = self.database_dump()
        receipt = request(self.t.url, 'POST', exchange.encoded(payload), 's'*32,
                          '/sync/pipeline-results', payload['batch_id'])
        self.assertTrue(receipt['replayed'])
        self.assertEqual(self.database_dump(), before)
        repeated = dict(payload, run_sequence=payload['run_sequence']+1, batch_id='QA-repeat-facts')
        request(self.t.url, 'POST', exchange.encoded(repeated), 's'*32,
                '/sync/pipeline-results', repeated['batch_id'])
        self.assertEqual(len([row for row in self.rows() if row['Job']=='arxiv']), 1)
        self.assert_protected()

    def test_failed_processing_can_be_delivered_successfully_and_source_filter_is_exact(self):
        self.seed('arxiv', status='failed')
        self.seed('openalex')
        self.t.shell('push')
        arxiv = [row for row in self.rows('arxiv') if row['Job']=='arxiv']
        self.assertEqual(len(arxiv), 1)
        self.assertEqual((arxiv[0]['Processing'], arxiv[0]['Delivery']), ('failed', 'accepted'))
        self.assertFalse(any(row['Job']=='openalex' for row in self.rows('arxiv')))
        self.assertFalse(any(row['Job']=='arxiv' for row in self.rows('openalex')))

    def test_real_export_omits_raw_messages_credentials_and_provider_headers(self):
        self.seed()
        canary = 'QA_PRIVATE_OPERATIONAL_CANARY_42'
        self.write_event('arxiv', 'failed', message='Authorization: Bearer '+canary+' x-api-key='+canary)
        with exchange.connect_db(self.t.mdb) as conn:
            conn.execute('UPDATE scout_runs SET errors_json=?,diagnostics_json=?',
                         (json.dumps([canary]), json.dumps({'private_header':canary})))
        self.t.shell('push')
        self.assertNotIn(canary, json.dumps(self.published()))
        self.assertNotIn(canary, self.database_dump())
        self.assertNotIn(canary, self.html()+self.html('analytics'))

    def test_missing_operational_ack_keeps_exact_outbox_until_authenticated_retry(self):
        self.seed()
        def old_ack(*args):
            body = json.loads(args[2])
            return {'status':'applied', 'batch_key':body['batch_id']}
        with self.assertRaisesRegex(ValueError, 'acknowledge'):
            push(self.t.mdb, self.t.mini, self.t.state, self.t.url, transport=old_ack)
        outbox = self.t.state/'pipeline-outbox.json'
        before = outbox.read_bytes()
        self.assertTrue((self.t.state/'active-cycle.json').exists())
        receipt = push(self.t.mdb, self.t.mini, self.t.state, self.t.url)
        self.assertEqual(receipt['operational_version'], 1)
        self.assertEqual(next(self.t.state.glob('published-*.json')).read_bytes(), before)

    def test_zero_result_run_does_not_leave_latest_run_card_saying_none(self):
        self.seed()
        self.t.shell('push')
        html = self.html()
        card = re.search(r'<h2>Latest Run</h2><strong>(.*?)</strong>', html).group(1)
        self.assertNotEqual(card, 'none', 'Imported completed zero-result run is missing from Latest Run')
        state = re.search(r'<h2>Workflow</h2><strong>(.*?)</strong>', html).group(1)
        self.assertNotEqual(state, 'none')
        self.assertNotIn('No workflow cycle has been recorded.', html)

    def test_future_completed_at_is_rejected_atomically_over_authenticated_http(self):
        self.seed()
        payload = export_delta(self.t.mdb, self.t.mini, self.t.state)
        cycle = json.loads((self.t.state/'active-cycle.json').read_text())
        payload.update(input_revision=cycle['input_revision'], producer_id='mini-production',
                       environment='production', run_sequence=1, batch_id='QA-future-completion')
        payload['operational']['scout_runs'][0]['completed_at'] = (
            datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
        before = self.database_dump()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            request(self.t.url, 'POST', exchange.encoded(payload), 's'*32,
                    '/sync/pipeline-results', payload['batch_id'])
        self.assertEqual(caught.exception.code, 422)
        self.assertEqual(self.database_dump(), before)

    def test_legacy_export_without_facts_keeps_missing_scout_na_not_zero(self):
        self.seed(nonzero=True)
        receipt = self.publish_legacy_export()
        self.assertEqual(receipt['status'], 'applied')
        self.assertNotIn('operational_version', receipt)
        html = self.html('analytics')
        self.assertIn('N/A', html)
        self.assertFalse(re.search(r'Candidates [\d-]+: 0', html),
                         'Missing legacy Scout facts must not become observed zero chart points')
        self.assert_protected()

    def test_ack_lost_after_aws_commit_replays_without_new_rows_or_lost_outbox(self):
        self.seed()
        def commit_without_ack(*args):
            request(*args)
            raise TimeoutError('QA simulated acknowledgement loss')
        with self.assertRaises(TimeoutError):
            push(self.t.mdb, self.t.mini, self.t.state, self.t.url, transport=commit_without_ack)
        body = (self.t.state/'pipeline-outbox.json').read_bytes()
        before = self.database_dump()
        receipt = push(self.t.mdb, self.t.mini, self.t.state, self.t.url)
        self.assertTrue(receipt['replayed'])
        self.assertEqual(self.database_dump(), before)
        self.assertEqual(next(self.t.state.glob('published-*.json')).read_bytes(), body)
        self.assertEqual(len([row for row in self.rows() if row.get('Job')=='arxiv']), 1)

    def test_invalid_fact_shapes_and_bad_auth_do_not_advance_aws_database(self):
        self.seed()
        payload = export_delta(self.t.mdb, self.t.mini, self.t.state)
        cycle = json.loads((self.t.state/'active-cycle.json').read_text())
        payload.update(input_revision=cycle['input_revision'], producer_id='mini-production',
                       environment='production', run_sequence=1, batch_id='QA-bad-fact')
        variants = []
        for value in (-1, True, 1.5, None):
            bad = copy.deepcopy(payload)
            bad['operational']['scout_runs'][0]['candidate_count'] = value
            variants.append(bad)
        bad = copy.deepcopy(payload)
        bad['operational']['events'][0]['message'] = 'QA_PRIVATE_NOT_ALLOWLISTED'
        variants.append(bad)
        bad = copy.deepcopy(payload)
        bad['operational']['events'] *= 201
        variants.append(bad)
        before = self.database_dump()
        for bad in variants:
            with self.subTest(shape=str(bad['operational'])[:80]):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    request(self.t.url, 'POST', exchange.encoded(bad), 's'*32,
                            '/sync/pipeline-results', bad['batch_id'])
                self.assertEqual(caught.exception.code, 422)
                self.assertEqual(self.database_dump(), before)
        unsigned = urllib.request.Request(self.t.url+'/sync/pipeline-results',
            data=exchange.encoded(payload), method='POST', headers={'Content-Type':'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(unsigned, context=self.context, timeout=10)
        self.assertEqual(caught.exception.code, 401)
        self.assertEqual(self.database_dump(), before)

    def test_failed_job_without_scout_remains_selectable_in_source_filter(self):
        self.write_event('core', 'failed')
        self.t.shell('push')
        html = self.html(source='core')
        self.assertTrue(re.search(r'<option value="core"[^>]*selected', html),
                        'Observed job-only source must remain the selected source, not silently become All')
        row = next(row for row in self.rows('core') if row.get('Job')=='core')
        self.assertEqual((row['Processing'], row['Delivery']), ('failed', 'accepted'))

    def test_date_filter_uses_event_time_not_fresh_receipt_time(self):
        instant = datetime.now(timezone.utc)-timedelta(days=10)
        old = instant.isoformat()
        cycle_path = self.t.state/'active-cycle.json'
        cycle = json.loads(cycle_path.read_text())
        cycle['started_at'] = (datetime.now(timezone.utc)-timedelta(days=11)).isoformat()
        cycle_path.write_text(json.dumps(cycle))
        self.seed('arxiv', when=old)
        # Produce the actual outbox under its historical export clock, then
        # deliver it now. Retained-event collection intentionally covers 7 days;
        # a current export must not be expected to invent a 10-day-old event.
        class ExportClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
        with patch.object(operational_evidence, 'datetime', ExportClock):
            payload = export_delta(self.t.mdb, self.t.mini, self.t.state)
        payload.update(input_revision=cycle['input_revision'], producer_id='mini-production',
                       environment='production', run_sequence=1, batch_id=cycle['batch_id'])
        (self.t.state/'pipeline-outbox.json').write_text(json.dumps(payload))
        push(self.t.mdb, self.t.mini, self.t.state, self.t.url)
        self.t.shell('pull')
        self.seed('openalex')
        self.t.shell('push')
        self.assertFalse(any(row.get('Job')=='arxiv' for row in self.rows(days=7)))
        self.assertTrue(any(row.get('Job')=='arxiv' for row in self.rows(days=21)))
        self.assertTrue(any(row.get('Job')=='openalex' for row in self.rows(days=7)))

    def test_daily_total_stays_unknown_when_other_source_has_legacy_missing_scout_counts(self):
        self.seed('arxiv')
        self.t.shell('push')
        self.t.shell('pull')
        self.seed('openalex', nonzero=True)
        self.publish_legacy_export()
        all_sources = self.html('analytics')
        self.assertIn('N/A', all_sources)
        self.assertFalse(re.search(r'Candidates [\d-]+: 0', all_sources),
                         'Known arXiv zero cannot imply an overall zero when OpenAlex counts are missing')
        self.assertTrue(re.search(r'Candidates [\d-]+: 0', self.html('analytics', 'arxiv')),
                        'A source with a measured zero must retain its numeric zero')

    def test_delayed_import_keeps_curator_yield_on_original_mini_run_day(self):
        # UTC midnight boundary: this is the preceding Pacific calendar day.
        old = (datetime.now(timezone.utc)-timedelta(days=2)).replace(
            hour=0, minute=30, second=0, microsecond=0)
        when = old.isoformat()
        cycle_path = self.t.state/'active-cycle.json'
        cycle = json.loads(cycle_path.read_text())
        cycle['started_at'] = (old-timedelta(minutes=1)).isoformat()
        cycle_path.write_text(json.dumps(cycle))
        self.seed('arxiv', nonzero=True, when=when)
        with exchange.connect_db(self.t.mdb) as conn:
            conn.execute('UPDATE workflow_cycles SET created_at=? WHERE id>?',
                         (when, cycle['baseline']['workflow_cycles']))
            conn.execute('UPDATE curator_runs SET created_at=? WHERE workflow_cycle_id>?',
                         (when, cycle['baseline']['workflow_cycles']))
            conn.execute('''UPDATE recommendations SET created_at=? WHERE curator_run_id IN
                (SELECT id FROM curator_runs WHERE workflow_cycle_id>?)''',
                (when, cycle['baseline']['workflow_cycles']))
        self.t.shell('push')
        day = old.astimezone(ZoneInfo('America/Los_Angeles')).date()
        html = self.html('analytics', 'arxiv')
        self.assertTrue(re.search(rf'Recommendations {day}: 1\b', html),
                        'Delayed delivery must not move a past Mini recommendation to the AWS import day')

    def test_publish_health_sends_retained_facts_without_business_or_discovery_changes(self):
        self.seed('arxiv', nonzero=True)
        self.t.shell('push')
        mini_before = self.mini_database_dump()
        business_before = self.business_rows()
        self.t.shell('publish-health')
        payload = json.loads(next(self.t.state.glob('health-published-*.json')).read_text())
        self.assertEqual(payload['results'], [])
        self.assertTrue(all(not rows for rows in payload['normalized'].values()))
        self.assertTrue(payload['operational']['scout_runs'])
        self.assertEqual(self.mini_database_dump(), mini_before)
        self.assertEqual(self.business_rows(), business_before)
        self.assertFalse((self.t.state/'active-cycle.json').exists())
        self.assertFalse((self.t.state/'health-outbox.json').exists())
        calls = [json.loads(line) for line in (self.t.root/'docker-args.jsonl').read_text().splitlines()]
        self.assertFalse(any('paper_agents.migration_job' in call for call in calls),
                         'Health publication must not launch discovery or inference')
        row = next(row for row in self.rows('arxiv') if row.get('Job')=='arxiv')
        self.assertEqual(row['Delivery'], 'accepted')
        self.assert_protected()

    def test_publish_health_uses_registered_routine_revision_when_full_input_exceeds_eight_mib(self):
        self.seed('arxiv', nonzero=True)
        self.t.shell('push')
        # Real production boundary: large paper data exceeds legacy JSON's cap,
        # while the authenticated compressed routine endpoint remains usable.
        with exchange.connect_db(self.t.db) as conn:
            conn.execute('UPDATE papers SET abstract=? WHERE id=?',
                         ('QA large retained abstract ' * 370000, self.t.pid))
        mini_before = self.mini_database_dump()
        business_before = self.business_rows()
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            request(self.t.url, 'GET', b'', os.environ['PAPER_AGENT_SYNC_SHARED_SECRET'], '/sync/input')
        self.assertEqual(rejected.exception.code, 413)
        rejected.exception.close()
        self.t.shell('publish-health')
        payload = json.loads(next(self.t.state.glob('health-published-*.json')).read_text())
        self.assertEqual(payload['results'], [])
        self.assertTrue(all(not rows for rows in payload['normalized'].values()))
        self.assertTrue(payload['operational']['scout_runs'])
        with exchange.connect_db(self.t.db) as conn:
            body, state = conn.execute('SELECT payload_json,status FROM sync_result_batches '
                                       'ORDER BY id DESC LIMIT 1').fetchone()
            self.assertEqual(state, 'applied')
            self.assertEqual(json.loads(body)['input_revision'], payload['input_revision'])
            self.assertEqual(json.loads(body)['operational'], payload['operational'])
            self.assertIsNotNone(conn.execute('SELECT 1 FROM sync_snapshot_revisions WHERE revision=?',
                                             (payload['input_revision'],)).fetchone())
        self.assertEqual(self.mini_database_dump(), mini_before)
        self.assertEqual(self.business_rows(), business_before)
        self.assertFalse((self.t.state/'active-cycle.json').exists())
        self.assertFalse((self.t.state/'health-outbox.json').exists())
        calls = [json.loads(line) for line in (self.t.root/'docker-args.jsonl').read_text().splitlines()]
        self.assertFalse(any('paper_agents.migration_job' in call for call in calls))
        row = next(row for row in self.rows('arxiv') if row.get('Job')=='arxiv')
        self.assertEqual(row['Delivery'], 'accepted')
        self.assert_protected()

    def test_observed_zero_scout_counts_render_as_zero_in_visible_analytics_tables(self):
        # Mix a real legacy delivery (no Scout evidence) on an earlier day with
        # today's measured zero. This also activates the chart fallback tables.
        old = (datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
        self.seed('core', nonzero=True, when=old)
        with exchange.connect_db(self.t.mdb) as conn:
            for table in ('workflow_cycles', 'curator_runs', 'recommendations'):
                conn.execute('UPDATE '+table+' SET created_at=?', (old,))
        self.publish_legacy_export()
        self.t.shell('pull')
        self.seed('core')
        self.t.shell('push')
        dom = html_fixture.HealthDOM(self.html('analytics', 'core'))
        row = next(row for row in dom.rows('Scout') if row.get('Source')=='core')
        for column in ('Candidate Count', 'Eligible Count', 'Excluded Count'):
            with self.subTest(column=column):
                self.assertEqual(row[column], '0',
                                 'A measured zero must remain visible, distinct from unavailable data')
        day = datetime.now(timezone.utc).astimezone(ZoneInfo('America/Los_Angeles')).date().isoformat()
        for title in ('Daily trend — missing counts are N/A',
                      'Recommendation gap — missing counts are N/A'):
            rows = dom.rows(title)
            today = next(row for row in rows if row['Day']==day)
            for column in ('Candidate Count', 'Eligible Count'):
                if column in today:
                    with self.subTest(table=title, column=column):
                        self.assertEqual(today[column], '0')
            self.assertTrue(any(row.get('Eligible Count')=='N/A' for row in rows),
                            'Unknown legacy observations must stay N/A alongside measured zero')

    def test_job_table_calls_the_ack_evidence_delivery_without_claiming_processing_success(self):
        self.seed(status='failed')
        self.t.shell('push')
        table = next(table for table in html_fixture.HealthDOM(self.html()).tables
                     if table['heading']=='Operational job history')
        self.assertIn('Evidence Delivery', table['rows'][0])
        row = next(row for row in self.rows() if row.get('Job')=='arxiv')
        self.assertEqual((row['Processing'], row['Delivery']), ('failed', 'accepted'))

    def test_export_does_not_hold_mini_sqlite_write_lock_during_real_http_delivery(self):
        self.seed()
        def check_then_deliver(*args):
            connection = sqlite3.connect(self.t.mdb, timeout=0)
            try:
                connection.execute('BEGIN IMMEDIATE')
                connection.rollback()
            finally:
                connection.close()
            return request(*args)
        receipt = push(self.t.mdb, self.t.mini, self.t.state, self.t.url, transport=check_then_deliver)
        self.assertEqual(receipt['operational_version'], 1)
        self.assert_protected()

    def test_semantic_job_alias_filters_under_semantic_scholar_source(self):
        self.seed('semantic_scholar')
        self.seed('openalex')
        self.t.shell('push')
        rows = self.rows('semantic_scholar')
        self.assertTrue(any(row.get('Job')=='semantic' for row in rows))
        self.assertFalse(any(row.get('Job')=='openalex' for row in rows))

    def test_empty_legacy_batch_does_not_turn_unknown_funnel_totals_into_zero(self):
        self.publish_legacy_export()
        html = self.html('analytics')
        for title in ('Candidates', 'Eligible', 'Excluded'):
            with self.subTest(title=title):
                value = re.search(r'<h2>'+title+r'</h2><strong>(.*?)</strong>', html).group(1)
                self.assertEqual(value, 'N/A', 'No operational facts cannot establish a measured '+title+' zero')

    def test_imported_zero_eligible_scout_updates_zero_eligible_source_card(self):
        self.seed('arxiv')
        self.t.shell('push')
        html = self.html()
        value = re.search(r'<h2>Zero Eligible</h2><strong>(.*?)</strong>', html).group(1)
        self.assertIn('arxiv', value.lower(), 'Imported zero-eligible evidence must not be labeled none')

    def test_observed_scout_without_job_log_still_establishes_latest_run_time(self):
        self.seed()
        (self.t.mini/'data/job-events.jsonl').unlink()
        self.t.shell('push')
        self.assertEqual(self.published()['operational']['events'], [])
        self.assertTrue(self.published()['operational']['scout_runs'])
        value = re.search(r'<h2>Latest Run</h2><strong>(.*?)</strong>', self.html()).group(1)
        self.assertNotEqual(value, 'none', 'A real timestamped Scout record is run evidence even without a job log')

    def mini_database_dump(self):
        with exchange.connect_db(self.t.mdb) as conn:
            return '\n'.join(conn.iterdump())

    def business_rows(self):
        with exchange.connect_db(self.t.db) as conn:
            return {name:conn.execute('SELECT * FROM '+name+' ORDER BY rowid').fetchall()
                    for name in ('papers','paper_sources','scout_runs','scout_candidates',
                                 'curator_runs','curator_evaluations','recommendations',
                                 'raw_feedback','structured_feedback','profile_versions')}

    def test_publish_health_rejects_active_pipeline_without_touching_business_or_outboxes(self):
        self.seed()
        marker = (self.t.state/'active-cycle.json').read_bytes()
        before = self.mini_database_dump()
        self.t.shell('publish-health', success=False)
        self.assertEqual((self.t.state/'active-cycle.json').read_bytes(), marker)
        self.assertEqual(self.mini_database_dump(), before)
        self.assertFalse((self.t.state/'health-outbox.json').exists())


if __name__=='__main__':
    unittest.main(verbosity=2)

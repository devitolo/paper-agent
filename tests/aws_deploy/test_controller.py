"""Synthetic state and failure injection only. No Docker or production access."""
from contextlib import closing, contextmanager
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('aws_deploy', ROOT / 'scripts/aws_deploy.py')
a = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(a)
OLD = 'ghcr.io/devitolo/paper-agent@sha256:' + '1' * 64
NEW = 'ghcr.io/devitolo/paper-agent@sha256:' + '2' * 64


class FakeHost(a.Host):
    def __init__(self, fail=None, rollback_fail=False, phase='queued'):
        self.c = {'releases': {NEW: {}}, 'container': 'app'}
        self.t = {'id': 'a' * 32, 'phase': phase, 'previous': OLD, 'image': NEW,
                  'revision': '3' * 40, 'bundle': '4' * 64, 'config_hash': 'hash',
                  'schema': 'schema', 'state_identity': 'state'}
        self.events, self.fail, self.rollback_fail = [], fail, rollback_fail
        self.used_failure = False

    def event(self, name):
        self.events.append(name)
        if name == self.fail and not self.used_failure:
            self.used_failure = True
            raise a.Blocked('injected')
        if self.rollback_fail and name == 'replace-old':
            raise a.Blocked('rollback_failure')

    def save(self, phase):
        self.t['phase'] = phase
        self.events.append('phase-' + phase)

    def incident(self):
        self.save("incident")

    @contextmanager
    def registry_auth(self):
        yield

    def selected_image(self):
        return OLD

    def validate_compose(self, image):
        self.event('compose')

    def inspect(self):
        return {'Image': OLD}

    def image(self, image, *args):
        self.event('image')
        return image

    def healthy(self, image):
        self.event('health-new' if image == NEW else 'health-old')

    def preflight(self):
        self.event('preflight')

    def command(self, args):
        self.event('pull')

    def fence(self, action):
        self.event('fence-' + action)

    def backup(self, name):
        self.event('backup')
        return Path('/synthetic'), 'schema'

    def compatible(self, image, copied):
        self.event('compatible')

    def replace(self, image):
        self.event('replace-new' if image == NEW else 'replace-old')

    def select(self, image):
        self.event('select-new' if image == NEW else 'select-old')


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(a, 'digest', return_value='hash'),
                        patch.object(a, 'state_identity', return_value='state')]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def exercise(self, host):
        try:
            host.deploy()
        except Exception:
            host.recover()
        return host

    def test_success_order_and_canonical_image(self):
        h = self.exercise(FakeHost())
        self.assertEqual(h.t['phase'], 'succeeded')
        for before, after in [('pull', 'fence-acquire'), ('backup', 'replace-new'),
                              ('compatible', 'replace-new'), ('health-new', 'select-new'),
                              ('select-new', 'fence-release')]:
            self.assertLess(h.events.index(before), h.events.index(after))
        self.assertEqual(h.events.count('preflight'), 4)

    def test_preflight_failure_never_fences_or_replaces(self):
        h = self.exercise(FakeHost('preflight'))
        self.assertEqual(h.t['phase'], 'failed')
        self.assertNotIn('pull', h.events)
        self.assertNotIn('fence-acquire', h.events)
        self.assertNotIn('replace-new', h.events)

    def test_backup_failure_blocks_interruption(self):
        h = self.exercise(FakeHost('backup'))
        self.assertEqual(h.t['phase'], 'rolled_back')
        self.assertNotIn('replace-new', h.events)
        self.assertNotIn('replace-old', h.events)

    def test_schema_failure_blocks_interruption(self):
        h = self.exercise(FakeHost('compatible'))
        self.assertNotIn('replace-new', h.events)
        self.assertEqual(h.t['phase'], 'rolled_back')

    def test_health_failure_rolls_back_and_reports_failure(self):
        h = self.exercise(FakeHost('health-new'))
        self.assertEqual(h.t['phase'], 'rolled_back')
        self.assertIn('replace-old', h.events)
        self.assertNotIn('select-new', h.events)
        self.assertLess(h.events.index('replace-old'), h.events.index('select-old'))

    def test_failed_rollback_leaves_fence_and_incident(self):
        h = self.exercise(FakeHost('health-new', rollback_fail=True))
        self.assertEqual(h.t['phase'], 'incident')
        self.assertNotIn('fence-release', h.events)

    def test_crash_recovery_from_every_phase(self):
        for phase in ['replacing', 'verifying', 'committing', 'rolling_back']:
            with self.subTest(phase=phase):
                h = FakeHost(phase=phase)
                h.recover()
                self.assertEqual(h.t['phase'], 'rolled_back')
                self.assertIn('replace-old', h.events)

    def test_ambiguous_release_does_not_rollback_acknowledged_writes(self):
        for phase in ['releasing', 'releasing_rollback']:
            h = FakeHost(phase=phase)
            h.recover()
            self.assertEqual(h.t['phase'], 'incident')
            self.assertNotIn('replace-old', h.events)

    def test_unknown_writer_blocks_recovery_and_keeps_fence(self):
        h = FakeHost(fail='fence-status', phase='verifying')
        h.recover()
        self.assertEqual(h.t['phase'], 'incident')
        self.assertNotIn('replace-old', h.events)
        self.assertNotIn('fence-release', h.events)

    def test_schema_change_during_failed_release_blocks_rollback(self):
        h = FakeHost(phase='verifying')
        h.t['schema'] = 'changed'
        h.recover()
        self.assertEqual(h.t['phase'], 'incident')
        self.assertNotIn('replace-old', h.events)

    def test_current_state_never_restored_from_backup(self):
        h = FakeHost(phase='verifying')
        with patch.object(a, 'state_identity', return_value='new-feedback'):
            h.recover()
        self.assertEqual(h.t['phase'], 'incident')
        self.assertNotIn('replace-old', h.events)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / 'live-data'
        self.config = self.root / 'live-config'
        self.data.mkdir()
        self.config.mkdir()
        (self.config / 'secret.env').write_text('synthetic-private-value')
        (self.data / 'summary.md').write_text('synthetic-summary')
        with closing(sqlite3.connect(self.data / 'paper_agent.db')) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE feedback(id INTEGER PRIMARY KEY, text TEXT)')
            db.execute('INSERT INTO feedback VALUES(1, "acknowledged")')
            db.commit()
        self.target = self.root / 'backup'

    def test_consistent_backup_and_all_acknowledged_feedback_preserved(self):
        a.snapshot(self.data, self.config, self.target)
        with closing(sqlite3.connect(self.target / 'data/paper_agent.db')) as db:
            self.assertEqual(db.execute('SELECT text FROM feedback').fetchall(), [('acknowledged',)])
        self.assertEqual((self.target / 'config/secret.env').read_text(), 'synthetic-private-value')
        expected = json.loads((self.root / 'backup.manifest.json').read_text())
        self.assertEqual(expected['files'], a.manifest(self.target))
        self.assertEqual((self.data / 'summary.md').read_text(), 'synthetic-summary')

    def test_changed_feedback_changes_state_identity(self):
        a.snapshot(self.data, self.config, self.target)
        before = a.state_identity(self.target)
        with closing(sqlite3.connect(self.target / 'data/paper_agent.db')) as db:
            db.execute('INSERT INTO feedback VALUES(2, "new-acknowledged")')
            db.commit()
        self.assertNotEqual(before, a.state_identity(self.target))

    def test_symlink_or_existing_destination_rejected(self):
        (self.data / 'escape').symlink_to(self.config / 'secret.env')
        with self.assertRaises(a.Blocked):
            a.snapshot(self.data, self.config, self.target)
        self.assertFalse(self.target.exists())
        (self.data / 'escape').unlink()
        self.target.mkdir()
        with self.assertRaises(a.Blocked):
            a.snapshot(self.data, self.config, self.target)


class ContractTests(unittest.TestCase):
    def test_release_approval_not_just_digest_syntax(self):
        with self.assertRaises(a.Blocked):
            a.release({'releases': {}}, NEW, '3' * 40, '4' * 64, '42', '1')
        approved = {'revision': '3' * 40, 'bundle': '4' * 64, 'schema_compatible': True,
                    'rehearsal_evidence': 'approved-synthetic-evidence', 'storage_budget_bytes': 1, 'run_id': '42', 'run_attempt': '1',
                    'approved_by': 'reviewer', 'approval_id': 'a' * 32,
                    'approved_at': 1, 'expires_at': 9999999999}
        self.assertEqual(a.release({'releases': {NEW: approved}}, NEW, '3' * 40, '4' * 64, '42', '1'), approved)
        for image in [NEW + ';id', 'untrusted.example/repo@sha256:' + '2' * 64, 'ghcr.io/devitolo/paper-agent:latest']:
            with self.assertRaises(a.Blocked):
                a.release({'releases': {image: approved}}, image, '3' * 40, '4' * 64, '42', '1')

    def test_fence_requires_no_unknown_or_active_writers_and_durability(self):
        h = object.__new__(a.Host)
        h.t = {'id': 'transaction'}
        h.c = {'fence_adapter': '/trusted/fence', 'fence_sha256': 'hash', 'fence_timeout_seconds': 10}
        valid = {'transaction': 'transaction', 'fenced': True, 'durable': True,
                 'active_writers': 0, 'unknown_writers': False}
        with patch.object(a, 'protected', side_effect=lambda p: p), patch.object(a, 'digest', return_value='hash'):
            for change in [{}, {'durable': False}, {'active_writers': 1}, {'unknown_writers': True}, {'transaction': 'other'}]:
                with patch.object(h, 'command', return_value=json.dumps({**valid, **change})):
                    if change:
                        with self.assertRaises(a.Blocked):
                            h.fence('status')
                    else:
                        h.fence('status')

    def test_root_config_is_disabled_and_thresholds_are_not_guessed(self):
        config = json.loads((ROOT / 'deploy/aws/host.example.json').read_text())
        self.assertFalse(config['enabled'])
        self.assertIsNone(config['minimum_available_bytes'])
        self.assertIsNone(config['fence_adapter'])
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / 'config.json'
            p.write_text(json.dumps(config))
            with patch.object(a, 'CONFIG', p), patch.object(a, 'protected', side_effect=lambda p, **k: Path(p)):
                with self.assertRaises(a.Blocked):
                    a.load_config()

    def test_host_lock_excludes_second_deployment(self):
        with tempfile.TemporaryDirectory() as t:
            with patch.object(a, 'STATE', Path(t)), patch.object(a, 'protected'):
                with a.locked():
                    with self.assertRaises(a.Blocked):
                        with a.locked():
                            self.fail('lock bypass')

    def test_workflow_aws_job_has_no_checkout_or_docker_or_secrets(self):
        text = (ROOT / '.github/workflows/aws-production.yml').read_text()
        deploy = text.split('\n  deploy:', 1)[1]
        self.assertNotIn('uses:', deploy)
        self.assertNotIn('secrets.', deploy)
        self.assertNotIn('docker ', deploy)
        self.assertIn('environment: aws-production', deploy)
        self.assertIn('runs-on: [self-hosted, linux, x64, project-paper-aws-deploy]', deploy)
        self.assertIn("github.ref == 'refs/heads/aws-production'", deploy)
        self.assertIn('cancel-in-progress: false', text)
        self.assertIn('ExecStopPost=/usr/local/sbin/project-paper-aws-deploy recover',
                      (ROOT / 'deploy/aws/project-paper-aws-deploy.service').read_text())


class HostBoundaryTests(unittest.TestCase):
    def host(self, directory):
        h = object.__new__(a.Host)
        h.dir = Path(directory)
        h.registry_env = None
        h.t = {'id': 'a' * 32}
        h.c = {'registry_auth_adapter': '/trusted/auth', 'protected_files': {'/trusted/auth': 'hash'},
               'command_timeout_seconds': 1, 'private_health_url': 'https://synthetic.ts.net',
               'validator_memory_bytes': 100, 'validator_memory_swap_bytes': 200,
               'validator_cpus': 0.5, 'validator_pids': 16}
        return h

    def test_registry_auth_is_stdin_only_and_removed_even_on_pull_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            h = self.host(directory)
            credentials = {'username': 'readonly', 'password': 'synthetic-secret', 'expires_at': int(a.time.time()) + 60}
            with patch.object(a, 'protected', side_effect=lambda p: Path(p)), patch.object(a, 'digest', return_value='hash'), \
                 patch.object(h, 'command', return_value=json.dumps(credentials)), patch.object(a, 'run') as run:
                with self.assertRaises(a.Blocked):
                    with h.registry_auth():
                        self.assertTrue(Path(h.registry_env['DOCKER_CONFIG']).exists())
                        raise a.Blocked('pull_failed')
                args = run.call_args.args
                self.assertNotIn('synthetic-secret', repr(args[0]))
                self.assertEqual(args[3], 'synthetic-secret')
                self.assertIsNone(h.registry_env)
                self.assertFalse(list(Path(directory).glob('registry-auth-*')))

    def test_expired_or_wrong_run_approval_rejected(self):
        valid = {'revision': '3' * 40, 'bundle': '4' * 64, 'schema_compatible': True,
                 'rehearsal_evidence': 'evidence', 'storage_budget_bytes': 1, 'run_id': '42',
                 'run_attempt': '1', 'approved_by': 'operator', 'approval_id': 'a' * 32,
                 'approved_at': 1, 'expires_at': int(a.time.time()) + 60}
        for change in [{'expires_at': 1}, {'run_id': '43'}, {'run_attempt': '2'}, {'approved_by': ''}]:
            with self.subTest(change=change), self.assertRaises(a.Blocked):
                a.release({'releases': {NEW: {**valid, **change}}}, NEW, '3' * 40, '4' * 64, '42', '1')

    def test_private_https_requires_semantic_health(self):
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as directory:
            h = self.host(directory)
            response = MagicMock()
            response.status = 200
            response.read.return_value = b'{"ready":false}'
            response.__enter__.return_value = response
            opener = MagicMock()
            opener.open.return_value = response
            with patch.object(a.urllib.request, 'build_opener', return_value=opener), self.assertRaises(a.Blocked):
                h.private_health()
            self.assertEqual(opener.open.call_args.args[0], 'https://synthetic.ts.net/ready')
            response.read.return_value = b'{"ready":true,"mode":"serving-only"}'
            with patch.object(a.urllib.request, 'build_opener', return_value=opener):
                h.private_health()

    def test_incident_has_fallback_when_journal_write_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            h = self.host(directory)
            with patch.object(a, 'STATE', Path(directory)), patch.object(h, 'save', side_effect=OSError('disk')), \
                 patch.object(a.syslog, 'syslog') as log:
                h.incident()
            self.assertEqual(h.t['phase'], 'incident')
            self.assertEqual(len(list(Path(directory).glob('incident-*'))), 1)
            log.assert_called_once()

    def test_validator_cleanup_runs_on_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            h = self.host(directory)
            with patch.object(h, 'cleanup_validator') as cleanup, \
                 patch.object(h, 'command', side_effect=a.Blocked('timeout')):
                with self.assertRaises(a.Blocked):
                    h.compatible(NEW, Path('/synthetic-copy'))
            self.assertEqual(cleanup.call_count, 2)

    def test_low_memory_or_swap_pressure_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            h = self.host(directory)
            h.c.update(minimum_available_bytes=2000, minimum_free_swap_bytes=1,
                       maximum_memory_psi_avg10=1)
            def read(path):
                return {'/proc/meminfo': 'MemAvailable: 1 kB\nSwapFree: 10 kB\nSwapTotal: 20 kB',
                        '/proc/vmstat': 'oom_kill 0\npswpin 0\npswpout 0',
                        '/proc/pressure/memory': 'some avg10=0.0'}[str(path)]
            with patch.object(Path, 'read_text', read), self.assertRaises(a.Blocked):
                h.resource_sample()

    def test_external_secret_bind_is_not_copied_or_made_writable(self):
        with tempfile.TemporaryDirectory() as directory:
            h = self.host(directory)
            h.c.update(service='app', expected_mounts=[{'Name': 'existing-data', 'Destination': '/app/data', 'RW': True}],
                       secret_mounts=[{'Source': '/root/secret', 'Destination': '/run/secrets/gemini', 'RW': False}])
            model = {'services': {'app': {'image': NEW, 'volumes': [
                {'type': 'volume', 'source': 'data', 'target': '/app/data'}],
                'secrets': [{'source': 'gemini'}]}},
                'volumes': {'data': {'name': 'existing-data', 'external': True}},
                'secrets': {'gemini': {'file': '/root/secret'}}}
            with patch.object(h, 'compose', return_value=json.dumps(model)), patch.object(h, 'command'):
                h.validate_compose(NEW)
            model['secrets']['gemini']['file'] = '/root/changed-secret'
            with patch.object(h, 'compose', return_value=json.dumps(model)), patch.object(h, 'command'), self.assertRaises(a.Blocked):
                h.validate_compose(NEW)


class PreflightTests(unittest.TestCase):
    def exercise(self, *, available_disk=10000, journal_access=True):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            h = object.__new__(a.Host)
            h.dir = Path(directory)
            h.t = {'config_hash': 'hash', 'image': NEW}
            h.c = {'protected_files': {}, 'sample_seconds': 1, 'oom_lookback_seconds': 60,
                   'project_dir': directory, 'backup_root': directory, 'storage_roots': [directory],
                   'expected_mounts': [{'Source': directory}], 'releases': {NEW: {'storage_budget_bytes': 100}},
                   'minimum_free_disk_bytes': 100, 'minimum_free_inodes': 5}
            def command(args):
                if args[:2] == ['docker', 'info']:
                    return json.dumps(directory)
                if args[0] == 'journalctl':
                    return '{"MESSAGE":"boot"}' if '-n' in args and journal_access else ''
                return ''
            sample = {'oom': 0, 'swapin': 0, 'swapout': 0, 'swap_free': 10}
            with patch.object(a, 'digest', return_value='hash'), patch.object(a.time, 'sleep'), \
                 patch.object(Path, 'read_text', return_value='boot-id'), \
                 patch.object(h, 'resource_sample', return_value=sample), patch.object(h, 'command', side_effect=command), \
                 patch.object(a.os, 'statvfs', return_value=SimpleNamespace(f_bavail=available_disk, f_frsize=1, f_favail=10)):
                h.preflight()

    def test_quiet_recent_kernel_log_is_valid_when_access_proven(self):
        self.exercise()

    def test_missing_kernel_history_blocks(self):
        with self.assertRaisesRegex(a.Blocked, 'kernel_history_unavailable'):
            self.exercise(journal_access=False)

    def test_storage_budget_failure_blocks(self):
        with self.assertRaisesRegex(a.Blocked, 'insufficient_storage'):
            self.exercise(available_disk=150)

    def test_consumed_approval_cannot_be_reused(self):
        approval = {'revision': '3' * 40, 'bundle': '4' * 64, 'schema_compatible': True,
                    'rehearsal_evidence': 'evidence', 'storage_budget_bytes': 1, 'run_id': '42',
                    'run_attempt': '1', 'approved_by': 'operator', 'approval_id': 'a' * 32,
                    'approved_at': 1, 'expires_at': int(a.time.time()) + 60}
        with tempfile.TemporaryDirectory() as directory:
            args = ['helper', 'submit', NEW, '3' * 40, '4' * 64, '42', '1']
            with patch.object(a, 'STATE', Path(directory)), patch.object(a, 'protected', side_effect=lambda p, **kw: Path(p)), \
                 patch.object(a, 'load_config', return_value={'releases': {NEW: approval}}), \
                 patch.object(a.os, 'geteuid', return_value=0), patch.object(a.sys, 'argv', args), \
                 patch.object(a, 'run', return_value='inactive'), patch.object(a, 'digest', return_value='hash'), \
                 patch.object(a, 'Host'), patch('builtins.print'):
                self.assertEqual(a.main(), 0)
                with self.assertRaisesRegex(a.Blocked, 'approval_already_consumed'):
                    a.main()


class DurabilityAndAuthTests(unittest.TestCase):
    def test_command_pipes_token_to_subprocess_stdin_not_argv(self):
        from types import SimpleNamespace
        with patch.object(a.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='')) as command:
            a.run(['docker', 'login', '--password-stdin'], 10, {'DOCKER_CONFIG': '/private'}, 'synthetic-token')
        self.assertEqual(command.call_args.kwargs['input'], 'synthetic-token')
        self.assertTrue(command.call_args.kwargs['text'])
        self.assertNotIn('synthetic-token', command.call_args.args[0])

    def test_auth_cleanup_success_and_login_timeout(self):
        from types import SimpleNamespace
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                h = HostBoundaryTests().host(directory)
                credentials = {'username': 'readonly', 'password': 'synthetic', 'expires_at': int(a.time.time()) + 60}
                effect = a.subprocess.TimeoutExpired('docker', 1) if failure else None
                with patch.object(a, 'protected', side_effect=lambda p: Path(p)), patch.object(a, 'digest', return_value='hash'), \
                     patch.object(h, 'command', return_value=json.dumps(credentials)), \
                     patch.object(a.subprocess, 'run', side_effect=effect, return_value=SimpleNamespace(returncode=0, stdout='')):
                    if failure:
                        with self.assertRaises(a.Blocked):
                            with h.registry_auth():
                                self.fail('timeout accepted')
                    else:
                        with h.registry_auth():
                            self.assertIsNotNone(h.registry_env)
                self.assertIsNone(h.registry_env)
                self.assertFalse(list(Path(directory).glob('registry-auth-*')))

    def test_worker_entry_removes_abandoned_registry_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transaction = root / ('a' * 32)
            auth = transaction / 'registry-auth-abandoned'
            auth.mkdir(parents=True)
            (auth / 'config.json').write_text('synthetic-token')
            with patch.object(a, 'STATE', root):
                a.Host({}, {'id': 'a' * 32})
            self.assertFalse(auth.exists())

    def test_config_failure_in_recovery_records_incident_without_host_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            t = {'id': 'a' * 32, 'phase': 'verifying'}
            (root / t['id']).mkdir()
            (root / 'current.json').write_text(json.dumps(t))
            with patch.object(a, 'STATE', root), patch.object(a, 'protected', side_effect=lambda p, **kw: Path(p)), \
                 patch.object(a.os, 'geteuid', return_value=0), patch.object(a.sys, 'argv', ['helper', 'recover']), \
                 patch.object(a, 'load_config', side_effect=a.Blocked('config_drift')), \
                 patch.object(a, 'run') as command, patch.object(a.syslog, 'syslog'):
                with self.assertRaises(a.Blocked):
                    a.main()
            self.assertEqual(json.loads((root / 'current.json').read_text())['phase'], 'incident')
            command.assert_not_called()

    def test_failed_backup_fsync_never_publishes_completion_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data, config, dest = root / 'data', root / 'config', root / 'backup'
            data.mkdir(); config.mkdir()
            with closing(sqlite3.connect(data / 'paper_agent.db')) as db:
                db.execute('CREATE TABLE feedback(id INTEGER)')
            with patch.object(a.os, 'fsync', side_effect=OSError('synthetic disk failure')):
                with self.assertRaises(OSError):
                    a.snapshot(data, config, dest)
            self.assertFalse((root / 'backup.manifest.json').exists())


class ApprovedDigestWorkflowTests(unittest.TestCase):
    def test_manual_promotion_has_no_build_or_secret_on_runner(self):
        workflow=(ROOT/'.github/workflows/aws-production.yml').read_text()
        self.assertIn('workflow_dispatch:',workflow)
        self.assertNotIn('\n  push:',workflow)
        self.assertNotIn('\n  pull_request:',workflow)
        self.assertNotIn('build-push-action',workflow)
        self.assertNotIn('checkout',workflow)
        self.assertNotIn('secrets.',workflow)
        self.assertNotIn('vars.',workflow)
        self.assertIn('needs: validate',workflow)
        self.assertEqual(workflow.count("if: github.ref == 'refs/heads/aws-production'"),2)
        self.assertNotIn("refs/heads/main",workflow)
        self.assertIn('runs-on: ubuntu-24.04',workflow)
        self.assertIn('permissions: {}',workflow)
        self.assertIn('cancel-in-progress: false',workflow)
        self.assertIn('runs-on: [self-hosted, linux, x64, project-paper-aws-deploy]',workflow)
        self.assertIn('environment: aws-production',workflow)

    def test_exact_shell_validation_rejects_tags_injection_and_bad_provenance(self):
        workflow=(ROOT/'.github/workflows/aws-production.yml').read_text()
        lines=workflow.split('        run: |\n',1)[1].split('\n  deploy:',1)[0].splitlines()
        script='\n'.join(line[10:] for line in lines)
        import os
        import subprocess
        for image,revision,bundle,valid in ((NEW,'a'*40,'b'*64,True),
                ('ghcr.io/devitolo/paper-agent:latest','a'*40,'b'*64,False),
                (NEW+'\nextra','a'*40,'b'*64,False),
                (NEW,'A'*40,'b'*64,False),
                (NEW,'a'*40,'$(touch should-never-run)',False)):
            with self.subTest(image=image,revision=revision), tempfile.TemporaryDirectory() as directory:
                env={**os.environ,'IMAGE':image,'REVISION':revision,'BUNDLE':bundle,
                    'GITHUB_OUTPUT':str(Path(directory)/'output'),'GITHUB_STEP_SUMMARY':str(Path(directory)/'summary')}
                result=subprocess.run(['bash','--noprofile','--norc','-euo','pipefail','-c',script],env=env,cwd=directory,capture_output=True)
                self.assertEqual(result.returncode==0,valid)
                self.assertFalse((Path(directory)/'should-never-run').exists())


class SyncHealthControllerTests(unittest.TestCase):
    def test_candidate_probe_failure_fails_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            host=HostBoundaryTests().host(directory)
            host.c.update(container='app',health_timeout_seconds=1,health_stability_seconds=1)
            host.t['image']=NEW
            inspected={'Image':'candidate-id','RestartCount':0,'State':{'Running':True,'OOMKilled':False,'Health':{'Status':'healthy'}}}
            def command(args):
                if args[-1]==a.SYNC_HEALTH_PROBE: raise a.Blocked('probe_failure')
                return ''
            with patch.object(host,'image',return_value='candidate-id'), patch.object(host,'inspect',return_value=inspected), patch.object(host,'command',side_effect=command), patch.object(a.time,'monotonic',side_effect=[0,0,2]), patch.object(a.time,'sleep'):
                with self.assertRaisesRegex(a.Blocked,'health_verification_failed'):host.healthy(NEW)

class CopiedStateValidationTests(unittest.TestCase):
    def fixture(self,root):
        from paper_agents.db import init_db
        (root/'data').mkdir(); (root/'config').mkdir()
        init_db(root/'data/paper_agent.db',ROOT/'sql/schema.sql')
        (root/'data/profile.json').write_text(json.dumps({'interests':[],'positive_signals':[],'negative_signals':[],'feedback_history':[]}))
        (root/'config/topics.yaml').write_text('topics: []\n')
        (root/'config/industry.json').write_text('{}')
        with closing(sqlite3.connect(root/'data/paper_agent.db')) as db:
            db.execute("INSERT INTO papers(canonical_key,title) VALUES('retained','Private paper')")
            db.execute("INSERT INTO raw_feedback(paper_id,content,content_hash) VALUES(1,'retained feedback','retained')")
            db.commit()

    def validate(self,root):
        code=a.READ_ONLY_VALIDATOR.replace('root=Path("/app")','root=Path('+repr(str(root))+')')
        exec(compile(code,'<read-only-state-validator>','exec'),{})

    def test_existing_main_schema_validates_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();self.fixture(root)
            before={p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()}
            self.validate(root)
            self.assertEqual(before,{p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_unknown_schema_fails_without_migrating_or_seeding(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();self.fixture(root)
            with closing(sqlite3.connect(root/'data/paper_agent.db')) as db:
                db.execute('ALTER TABLE papers ADD COLUMN unapproved TEXT');db.commit()
            before=(root/'data/paper_agent.db').read_bytes()
            with self.assertRaisesRegex(AssertionError,'unapproved schema'):self.validate(root)
            self.assertEqual(before,(root/'data/paper_agent.db').read_bytes())
            (root/'data/paper_agent.db').unlink()
            with self.assertRaises(AssertionError):self.validate(root)
            self.assertFalse((root/'data/paper_agent.db').exists())



if __name__ == '__main__':
    unittest.main()

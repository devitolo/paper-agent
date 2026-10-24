"""Private env-file wiring through real Compose config; no daemon or deployment."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from paper_agents.scout import SemanticScholarSource
from paper_agents.scheduler import child_environment

ROOT = Path(__file__).resolve().parents[1]
KEY1 = 'qa-file-first-key'
KEY2 = 'qa-file-second-key'


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()


class SemanticScholarEnvWiringQA(unittest.TestCase):
    def setUp(self):
        self.docker = shutil.which('docker')
        if not self.docker:
            self.skipTest('Docker Compose CLI required; no daemon is used')
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.private = self.root / 'private configuration' / 'actual.env'
        self.private.parent.mkdir()
        self.values = {
            'PAPER_MIGRATION_PROJECT': 'qa-key-wiring',
            'PAPER_MIGRATION_APP_IMAGE': 'synthetic/app@sha256:' + 'a' * 64,
            'PAPER_MIGRATION_OLLAMA_IMAGE': 'synthetic/model@sha256:' + 'b' * 64,
            'PAPER_AGENT_GEMINI_ENABLED': '0', 'PAPER_AGENT_TELEMETRY': '0',
            'PAPER_PHOENIX_DNS': 'qa-phoenix',
            'PAPER_IMPORT_MANIFEST_FILE': '/synthetic/manifest.json',
            'PAPER_MIGRATION_PORT': '18080', 'PAPER_GEMINI_KEY_FILE': '/synthetic/gemini-key',
            'PAPER_AGENT_SYNC_SHARED_SECRET': 's' * 32,
            'SEMANTIC_SCHOLAR_API_KEY1': KEY1, 'SEMANTIC_SCHOLAR_API_KEY2': KEY2,
        }
        for kind in ('DATA', 'CONFIG', 'CONTROL', 'LOG', 'BACKUP', 'MODEL'):
            self.values['PAPER_MIGRATION_' + kind + '_VOLUME'] = 'qa-' + kind.lower()
        self.environment = {'PATH': os.environ['PATH'], 'HOME': os.environ['HOME']}
        self.write_env(self.private, self.values)

    def write_env(self, path, values):
        path.parent.mkdir(parents=True, exist_ok=True)
        # Single quotes preserve literal dollar signs in the actual Compose parser.
        path.write_text(''.join(f"{name}='{value}'\n" for name, value in values.items()))
        path.chmod(0o600)

    def render(self, env_file=None, environment=None):
        return subprocess.run([self.docker, 'compose', '--env-file', str(env_file or self.private),
            '-f', str(ROOT / 'docker-compose.mini-migration.yml'), '--profile', 'scheduler',
            'config', '--format', 'json'], env=environment or self.environment,
            capture_output=True, text=True, timeout=15)

    def test_private_file_supplies_both_keys_to_app_scheduler_and_packaged_source(self):
        result = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        services = json.loads(result.stdout)['services']
        for service in ('app', 'scheduler'):
            with self.subTest(service=service):
                env = services[service]['environment']
                self.assertEqual(env['SEMANTIC_SCHOLAR_API_KEY1'], KEY1)
                self.assertEqual(env['SEMANTIC_SCHOLAR_API_KEY2'], KEY2)
                self.assertNotIn('SEMANTIC_SCHOLAR_API_KEY', env)
                with patch.dict(os.environ, env, clear=True):
                    source = SemanticScholarSource(verbose=False)
                self.assertEqual(source.api_keys, (KEY1, KEY2))
                self.assertEqual(source._headers()['x-api-key'], KEY1)

    def test_missing_either_key_is_rejected_by_compose_without_echoing_other_key(self):
        for missing in ('SEMANTIC_SCHOLAR_API_KEY1', 'SEMANTIC_SCHOLAR_API_KEY2'):
            with self.subTest(missing=missing):
                values = dict(self.values); values[missing] = ''
                self.write_env(self.private, values)
                result = self.render()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(missing, result.stderr)
                for secret in (KEY1, KEY2):
                    self.assertNotIn(secret, result.stdout + result.stderr)

    def test_duplicate_keys_pass_compose_but_packaged_source_rejects_safely(self):
        values = dict(self.values); values['SEMANTIC_SCHOLAR_API_KEY2'] = KEY1
        self.write_env(self.private, values)
        result = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        env = json.loads(result.stdout)['services']['app']['environment']
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError) as failure:
                SemanticScholarSource(verbose=False)
        self.assertNotIn(KEY1, str(failure.exception))

    def test_scheduler_child_and_actual_source_wrapper_preserve_both_keys(self):
        bin_dir = self.root / 'bin'; bin_dir.mkdir()
        capture = self.root / 'wrapper.json'
        python = bin_dir / 'python3'
        python.write_text('#!' + sys.executable + '\n' + '''import hashlib,json,os,sys
names=('SEMANTIC_SCHOLAR_API_KEY1','SEMANTIC_SCHOLAR_API_KEY2')
value={'keys':{name:hashlib.sha256(os.environ[name].encode()).hexdigest() for name in names},'args':sys.argv[1:]}
open(os.environ['QA_CAPTURE'],'w').write(json.dumps(value))
''')
        python.chmod(0o700)
        flock = bin_dir / 'flock'; flock.write_text('#!/bin/sh\nexit 0\n'); flock.chmod(0o700)
        parent = dict(self.environment, PATH=str(bin_dir) + os.pathsep + self.environment['PATH'],
            SEMANTIC_SCHOLAR_API_KEY1=KEY1, SEMANTIC_SCHOLAR_API_KEY2=KEY2, QA_CAPTURE=str(capture))
        env = child_environment(self.root, self.root / 'locks', parent)
        result = subprocess.run(['bash', str(ROOT / 'scripts/semantic_scholar_pipeline.sh')],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(capture.read_text())
        self.assertEqual(value['keys'], {name: fingerprint(parent[name]) for name in
                         ('SEMANTIC_SCHOLAR_API_KEY1', 'SEMANTIC_SCHOLAR_API_KEY2')})
        self.assertEqual(value['args'][:3], ['-m', 'paper_agents.cli', 'pipeline-daily'])
        self.assertEqual(value['args'][value['args'].index('--source') + 1], 'semantic_scholar')
        self.assertNotIn(KEY1, result.stdout + result.stderr)
        self.assertNotIn(KEY2, result.stdout + result.stderr)

    def run_workflow_updater_until_first_compose(self, *, key2=KEY2):
        workflow = (ROOT / '.github/workflows/mini-production.yml').read_text()
        step = workflow.split('- name: Deploy exact image to Mini', 1)[1]
        self.assertRegex(step, r'PAPER_MINI_ENV_FILE:\s*\$\{\{\s*vars\.PAPER_MINI_ENV_FILE\s*\}\}')
        command = re.search(r'(?m)^\s*run:\s*(bash scripts/mini_production_update.sh)\s*$', step)
        self.assertIsNotNone(command)
        values = dict(self.values); values['SEMANTIC_SCHOLAR_API_KEY2'] = key2
        self.write_env(self.private, values)
        final = self.root / 'default final'
        decoy = dict(self.values, SEMANTIC_SCHOLAR_API_KEY1='decoy-one', SEMANTIC_SCHOLAR_API_KEY2='decoy-two')
        self.write_env(final / 'production.env', decoy)
        candidate = self.root / 'candidate'; candidate.mkdir()
        overlay = self.root / 'overlay.yml'; overlay.write_text('services: {}\n')
        bin_dir = self.root / 'updater-bin'; bin_dir.mkdir()
        for name in ('flock',):
            tool = bin_dir / name; tool.write_text('#!/bin/sh\nexit 0\n'); tool.chmod(0o700)
        capture = self.root / 'updater.json'
        docker = bin_dir / 'docker'
        docker.write_text('#!' + sys.executable + '\n' + '''import hashlib,json,os,subprocess,sys
args=sys.argv[1:]
assert args[-1]=='ps', 'Unexpected updater mutation attempt'
run=subprocess.run([os.environ['QA_REAL_DOCKER']]+args[:-1]+['config','--format','json'],capture_output=True,text=True,timeout=15)
keys={}
if run.returncode==0:
 env=json.loads(run.stdout)['services']['app']['environment']
 keys={name:hashlib.sha256(env[name].encode()).hexdigest() for name in ('SEMANTIC_SCHOLAR_API_KEY1','SEMANTIC_SCHOLAR_API_KEY2')}
value={'env_file':args[args.index('--env-file')+1],'keys':keys,'ambient_keys':[name for name in ('SEMANTIC_SCHOLAR_API_KEY','SEMANTIC_SCHOLAR_API_KEY1','SEMANTIC_SCHOLAR_API_KEY2') if name in os.environ],'config_status':run.returncode}
open(os.environ['QA_CAPTURE'],'w').write(json.dumps(value))
sys.exit(73 if run.returncode==0 else run.returncode)
''')
        docker.chmod(0o700)
        env = dict(self.environment, PATH=str(bin_dir) + os.pathsep + self.environment['PATH'],
            PAPER_MINI_ROOT=str(self.root / 'mini'), PAPER_MINI_FINAL_ROOT=str(final),
            PAPER_MINI_ENV_FILE=str(self.private), PAPER_MINI_PRODUCTION_OVERLAY=str(overlay),
            PAPER_MINI_CANDIDATE_DIR=str(candidate), PAPER_MINI_DEPLOY_LOCK_FILE=str(self.root / 'deploy.lock'),
            PAPER_MINI_APP_IMAGE=self.values['PAPER_MIGRATION_APP_IMAGE'],
            SEMANTIC_SCHOLAR_API_KEY='stale-legacy', SEMANTIC_SCHOLAR_API_KEY1='stale-first',
            SEMANTIC_SCHOLAR_API_KEY2='stale-second', QA_REAL_DOCKER=self.docker, QA_CAPTURE=str(capture))
        result = subprocess.run(command.group(1).split(), cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertTrue(capture.exists(), result.stderr)
        return result, json.loads(capture.read_text())

    def test_workflow_env_path_and_updater_override_stale_ambient_and_default_file(self):
        result, captured = self.run_workflow_updater_until_first_compose()
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertEqual(captured['env_file'], str(self.private))
        self.assertEqual(captured['ambient_keys'], [])
        self.assertEqual(captured['keys'], {'SEMANTIC_SCHOLAR_API_KEY1': fingerprint(KEY1),
                                           'SEMANTIC_SCHOLAR_API_KEY2': fingerprint(KEY2)})
        for secret in (KEY1, KEY2, 'stale-first', 'stale-second'):
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_updater_cannot_fill_blank_private_key_with_stale_ambient_key(self):
        result, captured = self.run_workflow_updater_until_first_compose(key2='')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual(result.returncode, 73)
        self.assertNotEqual(captured['config_status'], 0)
        self.assertEqual(captured['ambient_keys'], [])
        self.assertEqual(captured['keys'], {})

    def test_protected_workflow_runs_failover_and_env_wiring_regressions(self):
        workflow = (ROOT / '.github/workflows/mini-production.yml').read_text()
        tests_job = workflow.split('\n  tests:', 1)[1].split('\n  build:', 1)[0]
        complete_discovery = 'qa/run_regression.py' in tests_job and '--match' not in tests_job
        if not complete_discovery:
            for module in ('tests.test_semantic_scholar_key_failover',
                           'tests.test_semantic_scholar_failover_qa',
                           'tests.test_semantic_scholar_env_wiring_qa',
                           'tests.test_semantic_scholar_cooldown',
                           'tests.test_semantic_scholar_progress',
                           'tests.test_scout_diagnostics',
                           'tests.test_sync_health',
                           'tests.test_mini_health_trend'):
                with self.subTest(module=module):
                    self.assertIn(module, tests_job,
                                  'Protected Mini build must execute the source and env wiring regressions')

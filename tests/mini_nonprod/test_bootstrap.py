import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('mini_fixture', ROOT/'scripts/mini_nonprod_fixture.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)

class BootstrapTests(unittest.TestCase):
    def test_fixture_serving_startup_and_persistence(self):
        from paper_agents.package_runtime import initialize
        from paper_agents.mini_nonprod_state import validate
        from paper_agents.migration_lifecycle import lease
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()/'state'
            fixture.create(root)
            env = {'PAPER_AGENT_STARTUP_MODE':'serving','PAPER_AGENT_PACKAGED':'1',
                   'PAPER_NON_PRODUCTION':'1',
                   'PAPER_AGENT_SERVING_ONLY':'1','PAPER_AGENT_SCHEDULER_ENABLED':'0',
                   'PAPER_AGENT_GEMINI_ENABLED':'0','PAPER_AGENT_TELEMETRY':'0',
                   'PAPER_AGENT_MINILM_ENABLED':'0'}
            with patch.dict(os.environ, env), lease(root/'control', exclusive=True):
                initialize(root)
                self.assertEqual(validate(root)['papers'],1)
            with sqlite3.connect(root/'data/paper_agent.db') as db:
                db.execute("UPDATE feedback SET notes='persistent synthetic feedback'")
                db.commit()
            with patch.dict(os.environ,env): initialize(root)
            with sqlite3.connect(root/'data/paper_agent.db') as db:
                self.assertEqual(db.execute('SELECT notes FROM feedback').fetchone()[0],
                                 'persistent synthetic feedback')
            with self.assertRaises(ValueError): fixture.create(root)

    def test_compose_has_all_runtime_mounts_and_no_shared_services(self):
        c = json.loads(subprocess.check_output(['ruby','-ryaml','-rjson','-e',
            'puts JSON.generate(YAML.load_file(ARGV[0]))',
            str(ROOT/'deploy/mini-nonprod/compose.bootstrap.yml')],text=True))
        a=c['services']['app']; e=a['environment']
        self.assertEqual(c['name'],'paper-mini-nonprod')
        self.assertEqual(set(c['services']),{'app'})
        self.assertEqual(a['ports'],['127.0.0.1:18080:8000'])
        self.assertEqual(a['expose'],['8000'])
        self.assertEqual(e['PAPER_AGENT_LIFECYCLE_DIR'],'/runtime-control')
        self.assertIn('control:/runtime-control',a['volumes'])
        self.assertEqual(e['PAPER_AGENT_SCHEDULER_ENABLED'],'0')
        self.assertEqual(e['PAPER_AGENT_SYNC_SHARED_SECRET'],'')
        self.assertTrue(c['networks']['isolated']['internal'])
        self.assertEqual(set(c['volumes']),{'data','config','logs','control','generated'})
        self.assertTrue(all(v['external'] for v in c['volumes'].values()))
        self.assertFalse({'secrets','extra_hosts','build'} & a.keys())
        self.assertNotIn('PAPER_SHARED_PDF_DIR',e)
        self.assertEqual(a['pull_policy'],'never')
        self.assertEqual(a['restart'],'no')

    def test_existing_watermark_is_visible_and_escaped(self):
        from paper_agents.web import render_app_header
        with patch.dict(os.environ,{'PAPER_NON_PRODUCTION':'1','PAPER_UI_ENV_LABEL':'MINI <script>'}):
            html=render_app_header('Test','','','review')
            self.assertIn('NON-PRODUCTION',html)
            self.assertIn('role="status">NON-PROD',html)
            self.assertIn('MINI &lt;script&gt;',html)
            self.assertNotIn('MINI <script>',html)

    def test_pipeline_is_branch_scoped_and_never_targets_production(self):
        workflow = (ROOT/'.github/workflows/mini-nonprod.yml').read_text()
        for expected in ('mini-nonprod', 'linux/amd64', 'mini_nonprod_update.sh',
                         'PAPER_MINI_NONPROD_IMAGE', 'environment: mini-nonprod',
                         'PAPER_AGENT_SCHEDULER_ENABLED=0'):
            self.assertIn(expected, workflow)
        self.assertNotIn('mini-production', workflow)
        self.assertNotIn('aws-production', workflow)

    def test_deploy_script_has_nonprod_guard_and_rollback(self):
        script = (ROOT/'scripts/mini_nonprod_update.sh').read_text()
        for expected in ('NONPROD_ONLY', 'paper-mini-nonprod', 'NON-PROD', 'rollback'):
            self.assertIn(expected, script)
        self.assertNotIn('crontab', script)
        self.assertNotIn('mini_production_update', script)

if __name__=='__main__': unittest.main()

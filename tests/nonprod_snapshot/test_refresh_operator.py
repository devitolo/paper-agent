"""Offline controller boundary and rollback tests; never contact Docker or Mini."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('refresh', Path(__file__).resolve().parents[2]/'scripts/mini_nonprod_refresh.py')
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)


class OperatorTests(unittest.TestCase):
    def test_production_unknown_and_cross_role_volumes_refused(self):
        labels={'org.projectpaper.managed-by':refresh.MANAGED,
                'org.projectpaper.role':'ui','org.projectpaper.environment':'mini-nonproduction'}
        for name in ('paper-mini-rehearsal-production-data','unknown',
                     'paper-mini-nonprod-pipeline-data-r20261020t123456123456z'):
            self.assertFalse(refresh.valid_volume('ui','data',name,labels))
        self.assertTrue(refresh.valid_volume('ui','data','paper-mini-nonprod-ui-data-r20261020t123456123456z',labels))
        self.assertFalse(refresh.valid_volume('ui','data','paper-mini-nonprod-ui-data-r20261020t123456123456z',{}))

    def test_helper_cannot_write_source_or_retained_volumes(self):
        op=refresh.Operator(SimpleNamespace())
        op.image='unused'
        op.allowed_volumes={'prod','old','new'}
        op.created=['new']
        with patch.object(refresh,'docker') as docker:
            for name in ('prod','old','unknown'):
                with self.assertRaises(RuntimeError):
                    op.helper('bad',[(name,'/destination',False)],'pass')
            docker.assert_not_called()

    def test_generated_roles_are_distinct_and_external_actions_disabled(self):
        configs=[]
        for role in ('ui','pipeline'):
            volumes={k:'paper-mini-nonprod-'+role+'-'+k for k in refresh.PATHS}
            config=refresh.role_config(role,'immutable-image',volumes,'test')
            service=config['services'][refresh.ROLES[role][1]]
            self.assertNotIn('ports',service)
            self.assertEqual(service['networks'],['isolated'])
            env=service['environment']
            self.assertEqual(env['PAPER_AGENT_STARTUP_MODE'],'serving-snapshot')
            for name in ('SCHEDULER_ENABLED','GEMINI_ENABLED','MINILM_ENABLED','TELEMETRY'):
                self.assertEqual(env['PAPER_AGENT_'+name],'0')
            self.assertEqual(env['PAPER_AGENT_SYNC_SHARED_SECRET'],'')
            configs.append({v['name'] for v in config['volumes'].values()})
        self.assertTrue(configs[0].isdisjoint(configs[1]))

    def test_actual_rollback_restores_recorded_role_not_production(self):
        op=refresh.Operator(SimpleNamespace())
        op.stopped=['ui'];op.switched=['ui']
        op.oldproxy=None
        previous={'Id':'old-ui','Config':{'Image':'previous-image'},
                  'Mounts':[{'Type':'volume','Name':'retained-data','Destination':'/app/data'}]}
        restored={**previous,'Id':'restored-ui'}
        op.old={'ui':previous}
        stack={'project':'paper-mini-nonprod','hashes':{}}
        op.oldstacks={'ui':stack}
        with patch.object(refresh,'docker',return_value='') as docker,              patch.object(refresh,'inspect',return_value=restored), patch.object(refresh,'compose') as compose:
            op.rollback()
        compose.assert_called_once_with(stack,'create','--no-build','--pull','never','--force-recreate','app')
        docker.assert_any_call('start','restored-ui')
        self.assertNotIn(refresh.PROD,str(docker.call_args_list))
        self.assertIn('previous nonprod',op.report['rollback'])

    def test_verification_failure_rolls_back_and_persists_failure(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(refresh,'ROOT',Path(temp)):
            op=refresh.Operator(SimpleNamespace(apply=True))
            op.created=[]
            with patch.object(op,'preflight'), patch.object(op,'acquire'), \
                 patch.object(op,'capture'), patch.object(op,'stage'), patch.object(op,'switch'), \
                 patch.object(op,'verify',side_effect=RuntimeError('private URL failed')), \
                 patch.object(op,'rollback') as rollback, patch.object(op,'release_fence') as fence:
                with self.assertRaisesRegex(RuntimeError,'Refresh failed'):
                    op.execute()
                rollback.assert_called_once()
                fence.assert_called_once()
            report=json.loads((op.out/'result.json').read_text())
            self.assertEqual(report['status'],'failed')
            self.assertEqual(report['error'],'private URL failed')
            self.assertFalse((Path(temp)/'current-refresh.json').exists())

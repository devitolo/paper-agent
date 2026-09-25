"""Synthetic image inspection and config-only Compose checks; no image operations."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from paper_agents import migration_lifecycle as lifecycle, rehearsal_images as images

ROOT = Path(__file__).resolve().parents[1]
APP = 'sha256:'+'a'*64
OLLAMA = 'sha256:'+'b'*64
REVISION = 'c'*40


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root/'candidate.tar.gz'; self.bundle.write_bytes(b'synthetic source bundle')
        self.sha = hashlib.sha256(self.bundle.read_bytes()).hexdigest()
        self.receipt = self.root/'identity.json'
        self.inventory = self.root/'runtime.json'
        self.record = {'format':1,'identity_mode':'local-rehearsal','app_image':APP,
                       'ollama_image':OLLAMA,'source_bundle_sha256':self.sha,'source_revision':REVISION}
        self.receipt.write_text(json.dumps(self.record))
        self.inventory.write_text(json.dumps({'source_bundle_sha256':self.sha,'source_revision':REVISION}))
        self.env = {'PAPER_MIGRATION_IDENTITY_MODE':'local-rehearsal','PAPER_APP_IMAGE':APP,
                    'PAPER_MIGRATION_OLLAMA_IMAGE':OLLAMA,'PAPER_REHEARSAL_SOURCE_SHA256':self.sha}

    def validate(self, env=None):
        with patch.dict(os.environ, self.env if env is None else env, clear=True), \
                patch.multiple(images, RECEIPT=self.receipt, INVENTORY=self.inventory):
            lifecycle.validate_container_contract()

    def test_production_registry_contract_is_unchanged_and_default(self):
        for mode in (None,'registry'):
            env = {'PAPER_APP_IMAGE':'example/app@'+APP,'PAPER_MIGRATION_OLLAMA_IMAGE':'example/ollama@'+OLLAMA}
            if mode: env['PAPER_MIGRATION_IDENTITY_MODE']=mode
            self.validate(env)
            for name in ('PAPER_APP_IMAGE','PAPER_MIGRATION_OLLAMA_IMAGE'):
                for bad in (APP, 'mutable:latest', 'example/app@sha256:abc'):
                    with self.subTest(mode=mode,name=name,bad=bad), self.assertRaises(ValueError):
                        self.validate({**env,name:bad})
        with self.assertRaises(ValueError): self.validate({**self.env,'PAPER_MIGRATION_IDENTITY_MODE':'typo'})

    def test_opt_in_accepts_only_matching_full_ids_receipt_and_baked_provenance(self):
        self.validate()
        for name in ('PAPER_APP_IMAGE','PAPER_MIGRATION_OLLAMA_IMAGE'):
            for bad in ('mutable:latest','example/app@'+APP,'sha256:abc','sha256:'+'d'*64):
                with self.subTest(name=name,bad=bad), self.assertRaises(ValueError):
                    self.validate({**self.env,name:bad})
        for value in ('', 'd'*64):
            with self.assertRaises(ValueError): self.validate({**self.env,'PAPER_REHEARSAL_SOURCE_SHA256':value})
        for field, value in [('source_bundle_sha256','d'*64),('source_revision','d'*40),('app_image',OLLAMA),('format',2)]:
            self.receipt.write_text(json.dumps({**self.record,field:value}))
            with self.subTest(field=field), self.assertRaises(ValueError): self.validate()
        self.receipt.write_text(json.dumps(self.record))
        for value in ({}, {'source_bundle_sha256':self.sha,'source_revision':'d'*40}, []):
            self.inventory.write_text(json.dumps(value))
            with self.assertRaises(ValueError): self.validate()
        self.inventory.unlink()
        with self.assertRaises(ValueError): self.validate()

    def test_missing_malformed_oversize_receipt_fails_closed(self):
        for text in ('not-json','[]','{}','x'*65537):
            self.receipt.write_text(text)
            with self.assertRaises(ValueError): self.validate()
        self.receipt.unlink()
        with self.assertRaises(ValueError): self.validate()

    def inspect_outputs(self):
        return [subprocess.CompletedProcess([],0,json.dumps({'Id':APP,'Config':{'Labels':{
                    images.BUNDLE_LABEL:self.sha, images.REVISION_LABEL:REVISION}}}),''),
                subprocess.CompletedProcess([],0,json.dumps({'Id':OLLAMA}), '')]

    def test_capture_inspects_exact_loaded_ids_and_binds_actual_archive_bytes(self):
        with patch.object(subprocess,'run',side_effect=self.inspect_outputs()) as inspect:
            self.assertEqual(images.capture(self.bundle,APP,OLLAMA),self.record)
            self.assertEqual([call.args[0] for call in inspect.call_args_list],
                [['docker','image','inspect',identity,'--format','{{json .}}'] for identity in (APP,OLLAMA)])
        with patch.object(subprocess,'run') as inspect:
            with self.assertRaises(ValueError): images.capture(self.bundle,'moving:tag',OLLAMA)
            inspect.assert_not_called()
        self.bundle.write_bytes(b'refreshed bundle must invalidate stale image/receipt')
        with patch.object(subprocess,'run',side_effect=self.inspect_outputs()):
            with self.assertRaises(ValueError): images.capture(self.bundle,APP,OLLAMA)

    def test_missing_source_labels_and_receipt_overwrite_are_rejected(self):
        for config in (None, [], {}, {'Labels':None}):
            outputs=self.inspect_outputs()
            outputs[0].stdout=json.dumps({'Id':APP,'Config':config})
            with patch.object(subprocess,'run',side_effect=outputs):
                with self.assertRaises(ValueError): images.capture(self.bundle,APP,OLLAMA)
        output=self.root/'new-receipt.json'
        argv=['rehearsal_images','--source-bundle',str(self.bundle),'--app-image',APP,
              '--ollama-image',OLLAMA,'--output',str(output)]
        with patch('sys.argv',argv), patch.object(subprocess,'run',side_effect=self.inspect_outputs()):
            images.main()
        self.assertEqual(json.loads(output.read_text()),self.record)
        self.assertEqual(output.stat().st_mode & 0o777,0o444)
        with patch('sys.argv',argv), patch.object(subprocess,'run',side_effect=self.inspect_outputs()):
            with self.assertRaises(SystemExit) as raised: images.main()
            self.assertEqual(raised.exception.code,1)
        self.assertEqual(json.loads(output.read_text()),self.record)

    def test_inspect_missing_or_wrong_image_and_revision_are_rejected(self):
        for value in ('[]','not-json',json.dumps({'Id':OLLAMA})):
            with patch.object(subprocess,'run',return_value=subprocess.CompletedProcess([],0,value,'')):
                with self.assertRaises(ValueError): images.inspect_image(APP)
        for error in (FileNotFoundError(),subprocess.TimeoutExpired('docker',15),subprocess.CalledProcessError(1,'docker')):
            with patch.object(subprocess,'run',side_effect=error):
                with self.assertRaises(ValueError): images.inspect_image(APP)
        outputs=self.inspect_outputs()
        value=json.loads(outputs[0].stdout);value['Config']['Labels'][images.REVISION_LABEL]='unknown'
        outputs[0].stdout=json.dumps(value)
        with patch.object(subprocess,'run',side_effect=outputs):
            with self.assertRaises(ValueError): images.capture(self.bundle,APP,OLLAMA)


class ComposeIdentityTests(unittest.TestCase):
    def test_explicit_overlay_preserves_never_pull_and_same_exact_service_ids(self):
        if not shutil.which('docker'): self.skipTest('Compose CLI unavailable')
        env={**os.environ,'PAPER_MIGRATION_PROJECT':'paper-synthetic-rehearsal',
            'PAPER_MIGRATION_APP_IMAGE':APP,'PAPER_MIGRATION_OLLAMA_IMAGE':OLLAMA,
            'PAPER_AGENT_GEMINI_ENABLED':'1','PAPER_AGENT_TELEMETRY':'1',
            'PAPER_PHOENIX_DNS':'synthetic-phoenix','PAPER_IMPORT_MANIFEST_FILE':'/synthetic/manifest.json',
            'PAPER_MIGRATION_PORT':'18080','PAPER_GEMINI_KEY_FILE':'/synthetic/key',
            'SEMANTIC_SCHOLAR_API_KEY':'synthetic','PAPER_REHEARSAL_SOURCE_SHA256':'d'*64,
            'PAPER_REHEARSAL_IDENTITY_FILE':'/synthetic/identity.json'}
        for kind in ('DATA','CONFIG','CONTROL','LOG','BACKUP','MODEL'):
            env['PAPER_MIGRATION_'+kind+'_VOLUME']='synthetic-'+kind.lower()
        command=['docker','compose','-f',str(ROOT/'docker-compose.mini-migration.yml'),
                 '-f',str(ROOT/'docker-compose.mini-rehearsal.yml'),
                 '--profile','scheduler','--profile','model-verification','config','--format','json']
        result=subprocess.run(command,env=env,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        services=json.loads(result.stdout)['services']
        for name in ('app','scheduler','prepare-model','ollama'):
            value=services[name]
            self.assertEqual(value['pull_policy'],'never')
            self.assertEqual(value['image'],OLLAMA if name=='ollama' else APP)
            if name!='ollama':
                self.assertEqual(value['environment']['PAPER_MIGRATION_IDENTITY_MODE'],'local-rehearsal')
                self.assertEqual(value['environment']['PAPER_APP_IMAGE'],APP)
                mount=next(item for item in value['volumes'] if item['target']=='/rehearsal/image-identity.json')
                self.assertTrue(mount['read_only'])
                self.assertFalse(mount['bind']['create_host_path'])
        for name in ('PAPER_REHEARSAL_SOURCE_SHA256','PAPER_REHEARSAL_IDENTITY_FILE'):
            invalid={**env};invalid.pop(name)
            self.assertNotEqual(subprocess.run(command,env=invalid,capture_output=True,timeout=10).returncode,0)


if __name__=='__main__': unittest.main()

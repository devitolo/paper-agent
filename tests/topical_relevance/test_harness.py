import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from experiments.topical_relevance import contracts, dataset, freeze, runner, evaluate
from experiments.topical_relevance.adapters import score
from experiments.topical_relevance.supervisor import Worker, ExecutionFailure
from experiments.topical_relevance.text_policy import select

POLICY = {'title_chars': 120, 'abstract_chars': 300, 'min_abstract_chars': 40}


def record(identity='one', title='Incident response and recovery'):
    return {'id': identity, 'synthetic': True, 'title': title,
            'abstract': 'This synthetic abstract describes incident response and recovery with human oversight in cloud systems.',
            'abstract_kind': 'synthetic_source_abstract',
            'provenance': {'source':'PRIVATE_SOURCE','query':'PRIVATE_QUERY'}}


def config(kind='fake', behavior='normal'):
    return {'interests': list(contracts.INTERESTS), 'aggregation':'max',
            'adapter': {'kind':kind, 'revision':'synthetic-v1', **({'behavior':behavior,'score':.5} if kind=='fake' else {'phrases':[['architecture'],['incident response'],['recovery','human oversight']]})},
            'limits': {'load_seconds':5,'pair_seconds':2,'run_seconds':30,'max_rss_bytes':1024**3,
                       'response_bytes':262144,'require_memory_measurement':False},
            'tie_seed':'frozen-synthetic-seed', 'metrics':{'k':[3,5],'gains':{'relevant':1,'partial':.5,'unrelated':0}}}


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def prepared(self, rows=None, cfg=None, suffix=''):
        manifest = dataset.prepare(rows or [record()], POLICY, self.root/('data'+suffix))
        conf = self.root/('config'+suffix+'.json');conf.write_text(json.dumps(cfg or config()))
        frozen = freeze.create(manifest,conf,self.root/('freeze'+suffix))
        return manifest,frozen

    def test_label_and_provenance_isolation(self):
        original = record()
        alternate = {**original,'id':'other-id','decision':'SECRET_LABEL','review':'SECRET_REVIEW','score':99,
                     'negative_signals':['SECRET_NEGATIVE'],'provenance':{'source':'OTHER','query':'OTHER_QUERY'}}
        self.assertEqual(select(original,POLICY), select(alternate,POLICY))
        manifest,frozen = self.prepared([alternate])
        seen=[]
        class CaptureWorker:
            calls=0;peak_rss=None;measurement_missing=True;load_seconds=0
            def __init__(self,*args):pass
            def start(self):pass
            def close(self):pass
            def request(self,request,timeout):
                seen.append(request)
                return {'seq':request['seq'],'score':.2,'payload_sha256':contracts.digest(request['payload']),'cpu_seconds':0}
        runner.run(frozen,output=self.root/'run',worker_factory=CaptureWorker)
        text=json.dumps(seen)
        for forbidden in ['SECRET','OTHER','other-id','decision','provenance','negative','score": 99']:
            self.assertNotIn(forbidden,text)
        self.assertEqual(len(seen),3)
        self.assertEqual({tuple(sorted(x['payload'])) for x in seen},{('abstract','interest','title')})

    def test_missing_abstract_never_starts_worker(self):
        manifest,frozen=self.prepared([{**record(),'abstract':None}])
        with patch.object(runner,'Worker'):
            report=runner.run(frozen,output=self.root/'run',worker_factory=lambda *a:self.fail('worker started'))
        self.assertEqual(report['counts']['insufficient_metadata'],1)
        self.assertEqual(report['adapter_pair_calls'],0)

    def test_summary_is_not_a_valid_abstract(self):
        self.assertEqual(select({**record(),'abstract_kind':'generated_summary'},POLICY)['status'],'insufficient_metadata')

    def test_truncation_is_common_and_explicit(self):
        row={**record(),'abstract':record()['abstract']*10}
        selected=select(row,POLICY)
        self.assertTrue(selected['truncated'])
        self.assertLessEqual(len(selected['payload']['abstract']),300)
        self.assertEqual(selected['abstract_span'],[0,len(selected['payload']['abstract'])])
        self.assertEqual(selected,select({**row,'model':'other'},POLICY))

    def test_truncation_cannot_create_tiny_ready_abstract(self):
        policy = {'title_chars': 120, 'abstract_chars': 40, 'min_abstract_chars': 40}
        row = {**record(), 'abstract': 'Hi ' + 'a' * 80}
        selected = select(row, policy)
        self.assertEqual(selected['status'], 'insufficient_metadata')
        self.assertEqual(selected['reason'], 'unusable_after_truncation')

    def test_duplicate_full_metadata_is_excluded(self):
        path=dataset.prepare([record('a'),record('b')],POLICY,self.root/'data')
        value=contracts.read_json(path)
        self.assertEqual(len(value['candidates']),1)
        self.assertEqual(value['excluded'],[{'id':'b','reason':'duplicate_normalized_metadata'}])

    def test_real_data_and_models_fail_closed(self):
        with self.assertRaisesRegex(contracts.ContractError,'real_corpus'):
            dataset.prepare([{**record(),'synthetic':False}],POLICY,self.root/'data')
        for model in ['minilm','smollm2','bge']:
            bad=config();bad['adapter']['kind']=model
            with self.assertRaisesRegex(contracts.ContractError,'real_runtime'):freeze.validate_config(bad)

    def test_interests_and_max_cannot_silently_change(self):
        bad=config();bad['aggregation']='mean'
        with self.assertRaises(contracts.ContractError):freeze.validate_config(bad)
        bad=config();bad['interests'][0]='Use prior reviews'
        with self.assertRaises(contracts.ContractError):freeze.validate_config(bad)

    def test_private_output_rejects_git_and_symlink_alias(self):
        git=self.root/'checkout';git.mkdir();(git/'.git').write_text('gitdir: elsewhere')
        link=self.root/'alias';link.symlink_to(git,target_is_directory=True)
        with self.assertRaisesRegex(contracts.ContractError,'inside_git'):contracts.new_directory(link/'private')

    def test_atomic_publish_does_not_overwrite(self):
        path=self.root/'record.json';contracts.publish(path,{'a':1})
        with self.assertRaises(FileExistsError):contracts.publish(path,{'a':2})
        self.assertEqual(contracts.read_json(path),{'a':1})

    def test_keyword_max_and_duplicate_invariance(self):
        cfg=config('keyword')['adapter'];payload={**select(record(),POLICY)['payload'],'interest':'unused here'}
        values=[score(cfg,payload,i) for i in range(3)]
        self.assertEqual(values,[0.,1.,2.])
        doubled={**payload,'abstract':payload['abstract']*2}
        self.assertEqual([score(cfg,doubled,i) for i in range(3)],values)
        self.assertEqual(max(values),max(reversed(values)))
        self.assertEqual(max(values+[0]),max(values))

    def test_ties_are_stable_and_share_rank(self):
        records=[{'id':x,'status':'complete','score':1} for x in ['a','b','c']]
        self.assertEqual(runner.rankings(records,'seed'),runner.rankings(list(reversed(records)),'seed'))
        self.assertEqual([r['rank'] for r in runner.rankings(records,'seed')],[1,1,1])

    def test_keyword_end_to_end_resume_does_not_repeat(self):
        manifest,frozen=self.prepared(cfg=config('keyword'))
        report=runner.run(frozen,output=self.root/'run')
        self.assertEqual(report['rankings'][0]['score'],2)
        path=self.root/'run'/runner.filename('one');before=path.read_bytes()
        resumed=runner.run(frozen,resume=self.root/'run',worker_factory=lambda *a:self.fail('repeated inference'))
        self.assertEqual(resumed['rankings'],report['rankings'])
        self.assertEqual(path.read_bytes(),before)
        self.assertTrue((self.root/'run'/'summary-2.json').exists())

    def test_checkpoint_and_freeze_tampering_rejected(self):
        manifest,frozen=self.prepared();runner.run(frozen,output=self.root/'run')
        path=self.root/'run'/runner.filename('one');value=contracts.read_json(path);value['score']=100;path.write_text(json.dumps(value))
        with self.assertRaisesRegex(contracts.ContractError,'checkpoint_hash'):runner.run(frozen,resume=self.root/'run')
        value=contracts.read_json(frozen);value['config']['tie_seed']='changed';Path(frozen).write_text(json.dumps(value))
        with self.assertRaisesRegex(contracts.ContractError,'freeze_hash'):runner.run(frozen,output=self.root/'other')

    def test_inflight_attempt_refuses_implicit_retry(self):
        manifest,frozen=self.prepared();folder=contracts.new_directory(self.root/'run')
        contracts.publish(folder/'freeze.json',contracts.read_json(frozen))
        contracts.publish(folder/('attempt-'+runner.filename('one')),{'id':'one'})
        with self.assertRaisesRegex(contracts.ContractError,'inflight'):runner.run(frozen,resume=folder)

    def test_memory_limit_kills_worker(self):
        cfg=config();worker=Worker(cfg['adapter'],cfg['limits'],time.monotonic()+10,sampler=lambda _:2*1024**3)
        try:
            with self.assertRaisesRegex(ExecutionFailure,'memory_limit'):worker.start()
        finally:worker.close()
        self.assertIsNotNone(worker.process.returncode)

    def test_unknown_memory_can_fail_closed(self):
        cfg=config();cfg['limits']['require_memory_measurement']=True
        worker=Worker(cfg['adapter'],cfg['limits'],time.monotonic()+10,sampler=lambda _:None)
        try:
            with self.assertRaisesRegex(ExecutionFailure,'memory_measurement_unavailable'):worker.start()
        finally:worker.close()

    def test_failure_stops_batch_and_sanitizes_output(self):
        for behavior in ['error','malformed','flood','hang','resistant_child']:
            with self.subTest(behavior=behavior):
                cfg=config(behavior=behavior);cfg['limits']['pair_seconds']=.3
                manifest,frozen=self.prepared([record(),record('two','Another title')],cfg,suffix=behavior)
                start=time.monotonic();folder=self.root/('run'+behavior)
                report=runner.run(frozen,output=folder)
                self.assertLess(time.monotonic()-start,5)
                self.assertEqual(report['counts']['failed'],1)
                self.assertEqual(report['pending'],1)
                self.assertNotIn('SECRET', ''.join(p.read_text() for p in folder.glob('paper-*.json')))
                with self.assertRaisesRegex(contracts.ContractError,'failed_run'):runner.run(frozen,resume=folder)

    def test_input_database_bytes_unchanged(self):
        import sqlite3
        path=self.root/'production.db'
        with sqlite3.connect(path) as connection:connection.execute('create table sentinel (value text)')
        before=path.read_bytes();manifest,frozen=self.prepared();runner.run(frozen,output=self.root/'run')
        self.assertEqual(path.read_bytes(),before)

    def test_blind_sheet_and_post_inference_metrics(self):
        manifest,frozen=self.prepared([record('a'),record('b','A different topic'),record('c','Third title')])
        evaluate.blind_sheet(manifest,self.root/'labels')
        path=self.root/'labels'/'blind-sheet.json';labels=contracts.read_json(path)
        self.assertNotIn('PRIVATE',json.dumps(labels))
        for row in labels['rows']:row['label']='relevant'
        labels['blind_protocol_acknowledged']=True;path.write_text(json.dumps(labels))
        folder=self.root/'run';folder.mkdir();contracts.publish(folder/'freeze.json',contracts.read_json(frozen))
        with self.assertRaisesRegex(contracts.ContractError,'inference_incomplete'):evaluate.evaluate([folder],path,self.root/'metrics')
        runner.run(frozen,resume=folder)
        result=evaluate.evaluate([folder],path,self.root/'metrics')
        self.assertEqual(result['comparisons'][0]['metrics']['3']['precision'],1)
        self.assertEqual(result['comparisons'][0]['metrics']['5']['precision'],.6)
        self.assertFalse(result['quality_claim_ready'])

        contracts.publish(folder/'cleanup-failed.json', {'status':'cleanup_incomplete'})
        with self.assertRaisesRegex(contracts.ContractError, 'cleanup_failure_invalidates_run'):
            evaluate.evaluate([folder],path,self.root/'invalid-cleanup-metrics')

    def test_metric_tie_sensitivity_and_missing_metadata(self):
        rows=[{'id':str(i),'score':1,'rank':1} for i in range(5)]
        labels={'0':'relevant','1':'unrelated','2':'partial','3':'insufficient_metadata','4':'relevant'}
        value=evaluate.metric(rows,labels,3)
        self.assertEqual(value['tie_precision_range'],[0,2/3])
        self.assertEqual(value['missed_relevant'],1)
        self.assertEqual(value['promoted_unrelated'],1)


    def test_resume_at_published_boundary_only_runs_remaining(self):
        import shutil
        rows=[record('a'),record('b','Second distinct title')]
        manifest,frozen=self.prepared(rows)
        runner.run(frozen,output=self.root/'complete')
        folder=contracts.new_directory(self.root/'partial')
        shutil.copyfile(frozen,folder/'freeze.json')
        shutil.copyfile(self.root/'complete'/runner.filename('a'),folder/runner.filename('a'))
        before=(folder/runner.filename('a')).read_bytes()
        report=runner.run(frozen,resume=folder)
        self.assertEqual(report['counts']['complete'],2)
        self.assertEqual((folder/runner.filename('a')).read_bytes(),before)
        self.assertEqual(len(list(folder.glob('attempt-*.json'))),1)

    def test_cleanup_of_resistant_descendant(self):
        cfg=config(behavior='resistant_child');cfg['limits']['pair_seconds']=.3
        worker=Worker(cfg['adapter'],cfg['limits'],time.monotonic()+10,sampler=lambda _:0)
        try:
            worker.start()
            with self.assertRaisesRegex(ExecutionFailure,'wall_deadline'):
                worker.request({'seq':0,'interest_index':0,'payload':{'title':'t','abstract':'a','interest':'i'}},.3)
        finally:worker.close()
        inspect=subprocess.run(['ps','-axo','pgid=,stat='],capture_output=True,text=True,timeout=1)
        if inspect.returncode:
            self.fail('Local ps permission required to verify descendant cleanup')
        active=[line for line in inspect.stdout.splitlines() if (parts:=line.split()) and int(parts[0])==worker.process.pid and not parts[1].startswith('Z')]
        self.assertEqual(active,[])

    def test_config_file_matches_approved_interests(self):
        path=Path(freeze.__file__).parent/'config'/'approved-design.json'
        self.assertEqual(contracts.read_json(path)['interests'],list(contracts.INTERESTS))

    def test_run_lock_prevents_concurrent_writer(self):
        manifest,frozen=self.prepared();folder=contracts.new_directory(self.root/'run')
        contracts.publish(folder/'freeze.json',contracts.read_json(frozen))
        (folder/'run.lock').write_text('another-process')
        with self.assertRaises(FileExistsError):runner.run(frozen,resume=folder)
        self.assertEqual((folder/'run.lock').read_text(),'another-process')

    def test_frozen_payload_parity_between_adapters(self):
        manifest,frozen=self.prepared()
        conf=self.root/'keyword.json';conf.write_text(json.dumps(config('keyword')))
        other=freeze.create(manifest,conf,self.root/'other-freeze')
        self.assertEqual(contracts.read_json(frozen)['manifest'],contracts.read_json(other)['manifest'])
        self.assertNotEqual(contracts.read_json(frozen)['sha256'],contracts.read_json(other)['sha256'])


if __name__=='__main__':unittest.main()

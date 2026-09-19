"""Hermetic adapter contracts: fake tokenizer, ONNX session, and artifact bytes."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from experiments.topical_relevance import contracts, dataset, freeze
from experiments.topical_relevance.adapters import validate_spec
from experiments.topical_relevance.adapters import minilm

CONFIG = Path(freeze.__file__).parent / 'config'


class Tokenizer:
    model_max_length = 512
    def __init__(self, count=71):
        self.count, self.calls = count, []
    def __call__(self, interest, document, **kwargs):
        self.calls.append((interest, document, kwargs))
        ids = [1] * self.count
        if kwargs['return_tensors']:
            ids = [ids]
        return {'input_ids': ids, 'attention_mask': ids, 'token_type_ids': ids}


class Logits:
    shape = (1, 1)
    def __getitem__(self, index):
        return [-2.5]


class Session:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs, self.calls = args, kwargs, []
    def get_providers(self): return ['CPUExecutionProvider']
    def get_inputs(self): return [SimpleNamespace(name=n) for n in ('input_ids', 'attention_mask', 'token_type_ids')]
    def get_outputs(self): return [SimpleNamespace(name='logits')]
    def run(self, output, inputs):
        self.calls.append(inputs)
        return [Logits()]


class MiniLMTests(unittest.TestCase):
    def setUp(self):
        self.spec = contracts.read_json(CONFIG / 'minilm-smoke.json')['adapter']
        self.payload = {'title':'Synthetic title', 'abstract':'Synthetic abstract with sufficient information for topical relevance.', 'interest':contracts.INTERESTS[0]}

    def test_only_exact_adapter_is_enabled(self):
        validate_spec(self.spec)
        for field, value in [('revision','main'), ('model_sha256','0'*64), ('model_dir','/tmp/model'), ('kind','bge')]:
            with self.subTest(field=field), self.assertRaises(contracts.ContractError):
                validate_spec({**self.spec, field:value})

    def test_hash_validation_and_tokenizer_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in minilm.FILES:
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(name.encode())
            with self.assertRaisesRegex(contracts.ContractError, 'model_artifact_sha256'):
                minilm.artifact_hashes(root)
            expected = hashlib.sha256(b'onnx/model.onnx').hexdigest()
            with patch.object(minilm, 'MODEL_SHA256', expected):
                before = minilm.artifact_hashes(root)
                (root/'vocab.txt').write_text('modified vocabulary')
                self.assertNotEqual(before, minilm.artifact_hashes(root))

    def test_payload_order_no_truncation_and_token_limit(self):
        tokenizer = Tokenizer()
        _, count = minilm.encode(tokenizer, self.payload, 0)
        self.assertEqual(count, 71)
        self.assertEqual(tokenizer.calls[0][:2], (self.payload['interest'], 'Synthetic title\n'+self.payload['abstract']))
        self.assertEqual(tokenizer.calls[0][2], dict(truncation=False,padding=False,add_special_tokens=True,return_tensors=None))
        for count in [513, 0]:
            with self.assertRaisesRegex(contracts.ContractError, 'pair_exceeds_token_limit'):
                minilm.encode(Tokenizer(count), self.payload, 0)
        for payload, index in [({**self.payload,'label':'relevant'},0),(self.payload,1),(self.payload,-1),(self.payload,True)]:
            with self.assertRaises(contracts.ContractError): minilm.encode(tokenizer,payload,index)

    def test_adapter_fake_runtime_cpu_threads_raw_logit(self):
        runtime = SimpleNamespace(SessionOptions=lambda:SimpleNamespace(),
            ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL='sequential'), InferenceSession=Session)
        tokenizer = Tokenizer()
        with patch.object(minilm,'fingerprint',return_value={'fixture':True}), patch.object(minilm,'load_tokenizer',return_value=tokenizer), patch.dict('sys.modules',{'onnxruntime':runtime}):
            adapter = minilm.Adapter(self.spec)
        options = adapter.session.kwargs['sess_options']
        self.assertEqual((options.intra_op_num_threads,options.inter_op_num_threads,options.execution_mode),(2,1,'sequential'))
        self.assertEqual(adapter.session.kwargs['providers'],['CPUExecutionProvider'])
        self.assertEqual(adapter.score(self.payload,0),(-2.5,71))
        tokenizer.count = 513
        with self.assertRaisesRegex(contracts.ContractError,'pair_exceeds_token_limit'):adapter.score(self.payload,0)
        self.assertEqual(len(adapter.session.calls),1)
        tokenizer.count = 71
        with patch.object(Logits,'shape',(1,2)):
            with self.assertRaisesRegex(contracts.ContractError,'unexpected_logit_shape'):adapter.score(self.payload,0)
        with patch.object(Logits,'__getitem__',return_value=[float('nan')]):
            with self.assertRaisesRegex(contracts.ContractError,'nonfinite_logit'):adapter.score(self.payload,0)

    def test_frozen_nine_pair_preflight_and_artifact_change(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = dataset.prepare(contracts.read_json(CONFIG/'minilm-smoke-papers.json'),contracts.read_json(CONFIG/'minilm-smoke-text-policy.json'),root/'data')
            tokenizer = Tokenizer()
            with patch.object(minilm,'fingerprint',return_value={'model_revision':minilm.REVISION,'tokenizer_revision':minilm.REVISION,'artifacts':'fixture'}), patch.object(minilm,'load_tokenizer',return_value=tokenizer):
                path = freeze.create(manifest,CONFIG/'minilm-smoke.json',root/'freeze')
                frozen = contracts.read_json(path)
                freeze.verify(frozen)
                self.assertEqual(len(tokenizer.calls),9)
                self.assertEqual(len(frozen['token_counts']),9)
                self.assertTrue(frozen['token_fit_verified'])
                self.assertEqual(len(set(row['selection']['payload']['abstract'] for row in frozen['manifest']['candidates'])),3)
                with patch.object(minilm,'fingerprint',return_value={'changed':True}):
                    with self.assertRaisesRegex(contracts.ContractError,'learned_artifacts_or_runtime_changed'):freeze.verify(frozen)
                tokenizer.count = 513
                with self.assertRaisesRegex(contracts.ContractError,'pair_exceeds_token_limit'):freeze.create(manifest,CONFIG/'minilm-smoke.json',root/'bad-freeze')
                self.assertFalse((root/'bad-freeze').exists())
                bad = copy.deepcopy(frozen['config']);bad['limits']['run_seconds']=901
                with self.assertRaisesRegex(contracts.ContractError,'900_seconds'):freeze.validate_smoke(frozen['manifest'],bad)
                bad_manifest = copy.deepcopy(frozen['manifest']);bad_manifest['candidates'].pop()
                with self.assertRaisesRegex(contracts.ContractError,'three_distinct'):freeze.validate_smoke(bad_manifest,frozen['config'])

    def test_runtime_versions_image_and_offline_required(self):
        env = {'PAPER_SMOKE_IMAGE_ID':minilm.IMAGE_ID,'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1'}
        with patch.object(minilm.sys,'version_info',(3,11,16)), patch.object(minilm,'version',side_effect=lambda n:minilm.VERSIONS[n]), patch.object(minilm,'artifact_hashes',return_value={'fake':'hash'}), patch.dict(minilm.os.environ,env):
            self.assertEqual(minilm.fingerprint(self.spec)['image_id'],env['PAPER_SMOKE_IMAGE_ID'])
            with patch.dict(minilm.os.environ,{'HF_HUB_OFFLINE':'0'}):
                with self.assertRaisesRegex(contracts.ContractError,'offline_environment'):minilm.fingerprint(self.spec)
            with patch.dict(minilm.os.environ,{'PAPER_SMOKE_IMAGE_ID':'tag'}):
                with self.assertRaisesRegex(contracts.ContractError,'resolved_image'):minilm.fingerprint(self.spec)
            with patch.object(minilm,'version',return_value='other'):
                with self.assertRaisesRegex(contracts.ContractError,'unapproved_learned_runtime'):minilm.fingerprint(self.spec)

    def test_linux_memory_sampling_without_ps(self):
        from experiments.topical_relevance.supervisor import linux_process_group_rss
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for pid, group, rss in [(1, 42, 10), (2, 42, 7), (3, 99, 200)]:
                path = root / str(pid); path.mkdir()
                (path/'stat').write_text(f'{pid} (weird ) process) S 0 {group} 0')
                (path/'statm').write_text(f'1000 {rss} 0')
            self.assertEqual(linux_process_group_rss(42, root, 4096), 17*4096)
            self.assertIsNone(linux_process_group_rss(900, root, 4096))
            (root/'1'/'statm').write_text('broken')
            self.assertIsNone(linux_process_group_rss(42, root, 4096))

    def test_nine_pair_runner_counts_and_token_contract(self):
        from experiments.topical_relevance import runner
        class FakeLearnedWorker:
            calls=0; peak_rss=1024; measurement_missing=False; load_seconds=0
            def __init__(self,*args): pass
            def start(self): pass
            def close(self): pass
            def request(self, request, seconds):
                return {'seq':request['seq'], 'score':float(request['interest_index']),
                        'payload_sha256':contracts.digest(request['payload']), 'cpu_seconds':0, 'tokens':71}
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            manifest=dataset.prepare(contracts.read_json(CONFIG/'minilm-smoke-papers.json'),contracts.read_json(CONFIG/'minilm-smoke-text-policy.json'),root/'data')
            with patch.object(minilm,'fingerprint',return_value={'model_revision':minilm.REVISION,'tokenizer_revision':minilm.REVISION}), patch.object(minilm,'load_tokenizer',return_value=Tokenizer()):
                frozen=freeze.create(manifest,CONFIG/'minilm-smoke.json',root/'freeze')
                result=runner.run(frozen,output=root/'run',worker_factory=FakeLearnedWorker)
                self.assertEqual(result['live_model_calls'],9)
                self.assertEqual(result['counts']['complete'],3)
                self.assertEqual([row['score'] for row in result['rankings']],[2.,2.,2.])
                original=FakeLearnedWorker.request
                def mismatch(self,request,seconds):
                    return {**original(self,request,seconds),'tokens':72}
                with patch.object(FakeLearnedWorker,'request',mismatch):
                    failed=runner.run(frozen,output=root/'bad',worker_factory=FakeLearnedWorker)
                    self.assertEqual(failed['counts']['failed'],1)
                    self.assertEqual(failed['pending'],2)

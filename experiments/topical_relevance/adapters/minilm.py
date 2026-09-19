"""Pinned offline CPU ONNX MiniLM; synthetic integration smoke only."""
import hashlib
from importlib.metadata import version
import math
import os
from pathlib import Path
import re
import sys
from ..contracts import INTERESTS, require

REVISION = '233902d25c440f23af6f7d6e94d2946bac0bee0a'
MODEL_SHA256 = '5d3e70fd0c9ff14b9b5169a51e957b7a9c74897afd0a35ce4bd318150c1d4d4a'
IMAGE_ID = 'sha256:7d8b960220e3c6f60292e6d40a8f300ff19c5ee05cd97cf5f725a76673e2d5c2'
MODEL_ID = 'cross-encoder/ms-marco-MiniLM-L6-v2'
FILES = ('onnx/model.onnx', 'config.json', 'tokenizer.json', 'tokenizer_config.json', 'special_tokens_map.json', 'vocab.txt')
VERSIONS = {'onnxruntime': '1.23.2', 'transformers': '4.57.6'}


def validate_spec(spec):
    require(set(spec) == {'kind', 'revision', 'model_sha256', 'model_dir'}, 'invalid_minilm_spec')
    require(spec['kind'] == 'minilm_onnx' and spec['revision'] == REVISION, 'unapproved_minilm_revision')
    require(spec['model_sha256'] == MODEL_SHA256, 'unapproved_model_sha256')
    require(spec['model_dir'] == '/models/minilm', 'unapproved_model_path')


def artifact_hashes(root):
    root = Path(root).resolve(strict=True)
    hashes = {}
    for name in FILES:
        path = (root / name).resolve(strict=True)
        require(path.is_relative_to(root) and path.is_file(), 'artifact_outside_model_directory')
        sha = hashlib.sha256()
        with path.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                sha.update(chunk)
        hashes[name] = sha.hexdigest()
    require(hashes['onnx/model.onnx'] == MODEL_SHA256, 'model_artifact_sha256_mismatch')
    return hashes


def fingerprint(spec):
    validate_spec(spec)
    require(sys.version_info[:2] == (3, 11), 'unapproved_python_runtime')
    versions = {name: version(name) for name in VERSIONS}
    require(versions == VERSIONS, 'unapproved_learned_runtime')
    image = os.environ.get('PAPER_SMOKE_IMAGE_ID', '')
    require(re.fullmatch(r'sha256:[0-9a-f]{64}', image) is not None, 'missing_resolved_image_id')
    require(image == IMAGE_ID, 'unapproved_container_image')
    require(os.environ.get('HF_HUB_OFFLINE') == '1' and os.environ.get('TRANSFORMERS_OFFLINE') == '1', 'offline_environment_required')
    return {'packages': versions, 'image_id': image, 'model_id': MODEL_ID,
            'model_revision': REVISION, 'tokenizer_revision': REVISION,
            'artifacts_sha256': artifact_hashes(spec['model_dir']),
            'provider': 'CPUExecutionProvider', 'dtype': 'float32',
            'intra_op_threads': 2, 'inter_op_threads': 1,
            'execution_mode': 'sequential', 'max_pair_tokens': 512,
            'score': 'raw_single_logit', 'pair_order': 'interest,title-newline-abstract'}


def load_tokenizer(spec):
    from transformers import AutoTokenizer
    return AutoTokenizer.from_pretrained(spec['model_dir'], local_files_only=True,
                                         trust_remote_code=False, use_fast=True)


def encode(tokenizer, payload, interest_index, tensors=None):
    require(set(payload) == {'title', 'abstract', 'interest'}, 'adapter_payload_not_allowlisted')
    require(type(interest_index) is int and 0 <= interest_index < 3, 'invalid_interest_index')
    require(all(isinstance(v, str) and v.strip() for v in payload.values()), 'invalid_pair_text')
    require(payload['interest'] == INTERESTS[interest_index], 'unapproved_pair_interest')
    require(len(payload['title']) <= 16000 and len(payload['abstract']) <= 16000, 'pair_text_budget_exceeded')
    encoded = tokenizer(payload['interest'], payload['title'] + '\n' + payload['abstract'],
                        truncation=False, padding=False, add_special_tokens=True,
                        return_tensors=tensors)
    ids = encoded['input_ids']
    count = len(ids[0]) if tensors else len(ids)
    limit = min(512, tokenizer.model_max_length)
    require(type(limit) is int and 0 < count <= limit, 'pair_exceeds_token_limit')
    return encoded, count


def preflight(spec, manifest):
    tokenizer = load_tokenizer(spec)
    counts = []
    for row in manifest['candidates']:
        selected = row['selection']
        if selected['status'] != 'ready':
            continue
        for index, interest in enumerate(INTERESTS):
            _, count = encode(tokenizer, {**selected['payload'], 'interest': interest}, index)
            counts.append({'id': row['id'], 'interest_index': index, 'tokens': count})
    return counts


class Adapter:
    def __init__(self, spec):
        self.runtime = fingerprint(spec)
        self.tokenizer = load_tokenizer(spec)
        import onnxruntime as ort
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self.session = ort.InferenceSession(str(Path(spec['model_dir']) / 'onnx/model.onnx'),
            sess_options=options, providers=['CPUExecutionProvider'])
        require(self.session.get_providers() == ['CPUExecutionProvider'], 'unexpected_execution_provider')
        self.inputs = [item.name for item in self.session.get_inputs()]
        require({'input_ids', 'attention_mask'} <= set(self.inputs) <=
                {'input_ids', 'attention_mask', 'token_type_ids'}, 'unexpected_onnx_inputs')
        require(len(self.session.get_outputs()) == 1, 'unexpected_onnx_outputs')

    def score(self, payload, interest_index):
        encoded, count = encode(self.tokenizer, payload, interest_index, tensors='np')
        require(set(self.inputs) <= set(encoded), 'missing_tokenizer_inputs')
        result = self.session.run(None, {name: encoded[name] for name in self.inputs})
        require(len(result) == 1 and tuple(result[0].shape) == (1, 1), 'unexpected_logit_shape')
        score = float(result[0][0][0])
        require(math.isfinite(score), 'nonfinite_logit')
        return score, count

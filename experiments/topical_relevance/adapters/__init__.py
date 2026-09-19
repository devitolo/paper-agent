"""Explicit adapters; only the pinned ONNX MiniLM synthetic smoke is enabled."""
import re
from ..contracts import require

DISABLED_MODELS = {
    'minilm': 'cross-encoder/ms-marco-MiniLM-L6-v2',
    'smollm2': 'SmolLM2-1.7B-Instruct Q4_K_M',
    'bge': 'BAAI/bge-reranker-base',
}


def validate_spec(spec):
    require(isinstance(spec, dict), 'invalid_adapter')
    if spec.get('kind') == 'minilm_onnx':
        from .minilm import validate_spec as validate_minilm
        validate_minilm(spec)
        return
    require(spec.get('kind') in {'keyword', 'fake'}, 'real_runtime_not_authorized')
    require(spec.get('revision') == 'synthetic-v1', 'invalid_adapter_revision')
    if spec['kind'] == 'keyword':
        require(set(spec) == {'kind', 'revision', 'phrases'}, 'invalid_keyword_spec')
        require(isinstance(spec['phrases'], list) and len(spec['phrases']) == 3, 'three_keyword_sets_required')
        for phrases in spec['phrases']:
            require(isinstance(phrases, list) and all(isinstance(x, str) and x.strip() for x in phrases), 'invalid_keywords')
    else:
        require(set(spec) <= {'kind', 'revision', 'behavior', 'score'}, 'invalid_fake_spec')
        require(spec.get('behavior', 'normal') in {'normal', 'hang', 'error', 'malformed', 'flood', 'resistant_child'}, 'invalid_fake_behavior')
        require(type(spec.get('score', 0.5)) in (int, float) and 0 <= spec.get('score', 0.5) <= 1, 'invalid_fake_score')


def score(spec, payload, interest_index):
    require(set(payload) == {'title', 'abstract', 'interest'}, 'adapter_payload_not_allowlisted')
    if spec['kind'] == 'fake':
        return float(spec.get('score', 0.5))
    text = (payload['title'] + '\n' + payload['abstract']).casefold()
    phrases = set(x.casefold().strip() for x in spec['phrases'][interest_index])
    # Unique literal phrase hits; no learned profile, evidence, recency, or negatives.
    return float(sum(bool(re.search(r'(?<!\w)' + re.escape(term) + r'(?!\w)', text)) for term in phrases))

import platform
import sys
from pathlib import Path
from .contracts import INTERESTS, digest, require, read_json, publish, new_directory
from .adapters import validate_spec


def code_fingerprint():
    root = Path(__file__).parent
    import hashlib
    return digest({str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted(root.rglob('*.py'))})


def validate_manifest(manifest):
    require(manifest.get('synthetic_only') is True, 'real_corpus_not_authorized')
    body = {k: v for k, v in manifest.items() if k != 'sha256'}
    require(digest(body) == manifest.get('sha256'), 'manifest_hash_mismatch')
    require(0 < len(manifest['candidates']) <= 30, 'invalid_candidates')
    ids = [r['id'] for r in manifest['candidates']]
    require(len(set(ids)) == len(ids), 'duplicate_ids')
    for row in manifest['candidates']:
        s = row['selection']
        require(s['status'] in {'ready', 'insufficient_metadata'}, 'invalid_selection_status')
        if s['status'] == 'ready':
            require(set(s['payload']) == {'title', 'abstract'}, 'invalid_payload_fields')
            require(all(isinstance(x, str) and x.strip() for x in s['payload'].values()), 'invalid_selected_text')
            require(digest(s['payload']) == s['payload_sha256'], 'payload_hash_mismatch')
            require(len(s['payload']['title']) <= manifest['text_policy']['title_chars'] and
                    len(s['payload']['abstract']) <= manifest['text_policy']['abstract_chars'], 'text_budget_exceeded')


def validate_config(config):
    require(set(config) == {'interests', 'aggregation', 'adapter', 'limits', 'tie_seed', 'metrics'}, 'invalid_config_fields')
    require(config['interests'] == list(INTERESTS) and config['aggregation'] == 'max', 'unapproved_interests_or_aggregation')
    validate_spec(config['adapter'])
    limits = config['limits']
    caps = {'load_seconds': 120, 'pair_seconds': 60, 'run_seconds': 6000,
            'max_rss_bytes': 4 * 1024**3, 'response_bytes': 262144}
    require(set(limits) == set(caps) | {'require_memory_measurement'}, 'invalid_limits')
    for key, cap in caps.items():
        require(type(limits[key]) in (int, float) and 0 < limits[key] <= cap, 'invalid_limit')
    require(type(limits['response_bytes']) is int, 'invalid_byte_limit')
    require(type(limits['require_memory_measurement']) is bool, 'invalid_memory_policy')
    require(isinstance(config['tie_seed'], str) and bool(config['tie_seed']), 'missing_tie_seed')
    require(config['metrics'] == {'k': [3, 5], 'gains': {'relevant': 1, 'partial': 0.5, 'unrelated': 0}}, 'metrics_not_frozen')


def create(manifest_path, config_path, output):
    manifest, config = read_json(manifest_path), read_json(config_path)
    validate_manifest(manifest)
    validate_config(config)
    learned = config['adapter']['kind'] == 'minilm_onnx'
    learned_runtime, token_counts = None, []
    if learned:
        validate_smoke(manifest, config)
        from .adapters.minilm import fingerprint, preflight
        learned_runtime = fingerprint(config['adapter'])
        token_counts = preflight(config['adapter'], manifest)
    frozen = {'schema_version': 1, 'synthetic_only': True, 'manifest': manifest, 'config': config,
              'code_sha256': code_fingerprint(), 'runtime': {'python': sys.version, 'platform': platform.platform()},
              'learned_runtime': learned_runtime,
              'model_revision': learned_runtime['model_revision'] if learned else None,
              'tokenizer_revision': learned_runtime['tokenizer_revision'] if learned else None,
              'token_fit_verified': learned, 'token_counts': token_counts, 'hardware_gate_passed': False,
              'disclosure': 'Synthetic engineering only; no real-corpus evaluation or quality claims.'}
    frozen['sha256'] = digest(frozen)
    folder = new_directory(output)
    publish(folder / 'freeze.json', frozen)
    return folder / 'freeze.json'


def verify(frozen):
    require(digest({k: v for k, v in frozen.items() if k != 'sha256'}) == frozen.get('sha256'), 'freeze_hash_mismatch')
    require(frozen.get('synthetic_only') is True, 'real_execution_not_authorized')
    validate_manifest(frozen['manifest'])
    validate_config(frozen['config'])
    require(frozen['code_sha256'] == code_fingerprint(), 'code_changed_since_freeze')
    if frozen['config']['adapter']['kind'] == 'minilm_onnx':
        validate_smoke(frozen['manifest'], frozen['config'])
        from .adapters.minilm import fingerprint
        require(frozen['learned_runtime'] == fingerprint(frozen['config']['adapter']), 'learned_artifacts_or_runtime_changed')
        require(frozen['token_fit_verified'] is True, 'token_fit_not_verified')
        expected = [(row['id'], i) for row in frozen['manifest']['candidates'] if row['selection']['status'] == 'ready' for i in range(3)]
        require([(r['id'], r['interest_index']) for r in frozen['token_counts']] == expected
                and all(type(r['tokens']) is int and 0 < r['tokens'] <= 512 for r in frozen['token_counts']), 'invalid_token_fit_record')
    require(frozen['runtime'] == {'python': sys.version, 'platform': platform.platform()}, 'runtime_changed_since_freeze')


def validate_smoke(manifest, config):
    require(len(manifest['candidates']) == 3 and not manifest['excluded'], 'minilm_smoke_requires_three_distinct_candidates')
    require(config['limits']['run_seconds'] <= 900, 'minilm_smoke_max_900_seconds')


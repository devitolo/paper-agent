"""Only synthetic fixture assembly is enabled by the current approval gate."""
from .contracts import digest, require, new_directory, publish
from .text_policy import select, normalize


def prepare(records, policy, output):
    require(isinstance(records, list) and 0 < len(records) <= 30, 'invalid_candidate_count')
    prepared, seen_ids, seen_content, excluded = [], set(), set(), []
    for row in records:
        require(isinstance(row, dict) and row.get('synthetic') is True, 'real_corpus_not_authorized')
        identity = row.get('id')
        require(isinstance(identity, str) and 0 < len(identity) <= 100 and identity not in seen_ids,
                'invalid_or_duplicate_id')
        seen_ids.add(identity)
        selection = select(row, policy)
        # Deduplicate the full normalized metadata, not a potentially identical prefix.
        fingerprint = digest([normalize(str(row.get('title') or '')), normalize(str(row.get('abstract') or ''))])
        if fingerprint in seen_content:
            excluded.append({'id': identity, 'reason': 'duplicate_normalized_metadata'})
            continue
        seen_content.add(fingerprint)
        provenance = row.get('provenance') or {}
        require(isinstance(provenance, dict), 'invalid_provenance')
        prepared.append({'id': identity, 'input_sha256': fingerprint, 'selection': selection,
                         'provenance': {k: provenance.get(k) for k in ('source', 'query', 'capture_date', 'record_key')}})
    manifest = {'schema_version': 1, 'synthetic_only': True, 'text_policy': policy,
                'candidates': prepared, 'excluded': excluded,
                'pre_filter_sampling_verified': False, 'sampling_scope': 'synthetic fixtures only'}
    manifest['sha256'] = digest(manifest)
    directory = new_directory(output)
    publish(directory / 'manifest.json', manifest)
    return directory / 'manifest.json'

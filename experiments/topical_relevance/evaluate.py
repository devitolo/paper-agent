"""Post-inference synthetic label comparison; never imported by worker/adapters."""
from .contracts import LABELS, digest, require, read_json, publish, new_directory
from .freeze import validate_manifest, verify
from .runner import filename, rankings, validate_run_directory


def label_key(manifest, identity):
    return digest(['blind-sheet-v1', manifest['sha256'], identity])[:16]


def blind_sheet(manifest_path, output):
    manifest = read_json(manifest_path)
    validate_manifest(manifest)
    rows = []
    for row in manifest['candidates']:
        selection = row['selection']
        text = selection.get('payload', {'title': None, 'abstract': None})
        rows.append({'key': label_key(manifest, row['id']), **text, 'label': None,
                     'truncated': selection.get('truncated', False)})
    rows.sort(key=lambda r: r['key'])
    folder = new_directory(output)
    publish(folder / 'blind-sheet.json', {'manifest_sha256': manifest['sha256'], 'rows': rows,
             'blind_protocol_acknowledged': False,
             'instructions': 'Assign relevant/partial/unrelated/insufficient_metadata without viewing model outputs; acknowledge protocol after labeling.'})


def metric(ordered, labels, k):
    gains = {'relevant': 1, 'partial': .5, 'unrelated': 0, 'insufficient_metadata': 0}
    top = ordered[:k]
    top_ids = {r['id'] for r in top}
    relevant = sum(labels[r['id']] == 'relevant' for r in top)
    assessed = [r for r in top if labels[r['id']] != 'insufficient_metadata']
    result = {'precision': relevant/k, 'graded_relevance': sum(gains[labels[r['id']]] for r in top)/k,
        'denominator': k, 'returned': len(top), 'conditional_assessed_denominator': len(assessed),
        'conditional_assessed_precision': relevant/len(assessed) if assessed else None,
        'missed_relevant': sum(label == 'relevant' and identity not in top_ids for identity, label in labels.items()),
        'promoted_unrelated': sum(labels[r['id']] == 'unrelated' for r in top),
        'insufficient_metadata_in_top': sum(labels[r['id']] == 'insufficient_metadata' for r in top)}
    if top:
        boundary = top[-1]['score']
        tied = [r for r in ordered if r['score'] == boundary]
        above = [r for r in ordered if r['score'] > boundary]
        slots = min(k-len(above), len(tied))
        positive = sum(labels[r['id']] == 'relevant' for r in tied)
        fixed = sum(labels[r['id']] == 'relevant' for r in above)
        result['boundary_tie_size'] = len(tied)
        result['tie_precision_range'] = [(fixed+max(0, slots-(len(tied)-positive)))/k,
                                         (fixed+min(slots, positive))/k]
    return result


def evaluate(run_dirs, labels_path, output):
    require(len(run_dirs) > 0, 'no_runs')
    label_data = read_json(labels_path)
    require(label_data.get('blind_protocol_acknowledged') is True, 'blind_protocol_not_acknowledged')
    reports, common = [], None
    for directory in run_dirs:
        from pathlib import Path
        directory = Path(directory)
        validate_run_directory(directory)
        frozen = read_json(directory / 'freeze.json')
        verify(frozen)
        manifest, config = frozen['manifest'], frozen['config']
        comparison = digest([manifest, config['interests'], config['aggregation'], config['tie_seed'], config['metrics']])
        require(common is None or common == comparison, 'input_parity_mismatch')
        common = comparison
        require(label_data['manifest_sha256'] == manifest['sha256'], 'labels_for_different_manifest')
        raw_labels = label_data['rows']
        require(isinstance(raw_labels, list), 'invalid_labels')
        lookup = {r['key']: r['label'] for r in raw_labels}
        require(len(lookup) == len(raw_labels), 'duplicate_labels')
        require(set(lookup) == {label_key(manifest,r['id']) for r in manifest['candidates']}, 'incomplete_labels')
        require(all(label in LABELS for label in lookup.values()), 'invalid_label')
        labels = {r['id']: lookup[label_key(manifest,r['id'])] for r in manifest['candidates']}
        records = []
        for row in manifest['candidates']:
            path = directory / filename(row['id'])
            require(path.exists(), 'inference_incomplete_before_label_join')
            record = read_json(path)
            require(record['freeze_sha256'] == frozen['sha256'], 'checkpoint_mismatch')
            require(digest({k:v for k,v in record.items() if k!='sha256'}) == record['sha256'], 'checkpoint_hash_mismatch')
            records.append(record)
        order = rankings(records,config['tie_seed'])
        reports.append({'adapter': config['adapter']['kind'], 'freeze_sha256': frozen['sha256'],
            'ranked_ids': [r['id'] for r in order],
            'metrics': {str(k):metric(order,labels,k) for k in config['metrics']['k']},
            'statuses': {s:sum(r['status']==s for r in records) for s in ['complete','failed','insufficient_metadata']}})
    result = {'synthetic_only': True, 'label_sha256': digest(label_data), 'comparisons': reports,
              'quality_claim_ready': False, 'limitations': [
                  'Synthetic engineering evidence only; no model quality or production efficacy demonstrated.',
                  'Blind labeling acknowledgment is user attestation, not proof of historical non-exposure.',
                  'Top-k tie ranges are sensitivity bounds, not confidence intervals.',
                  'Small-panel paired differences are descriptive; no population inference or significance claim.']}
    folder = new_directory(output)
    publish(folder/'evaluation.json',result)
    return result

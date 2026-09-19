from contextlib import contextmanager
import hashlib
import math
import os
from pathlib import Path
import time
from .contracts import digest, require, read_json, publish, new_directory, external_path
from .freeze import verify
from .supervisor import Worker, ExecutionFailure, cancellation, machine_memory


def filename(identity):
    return 'paper-'+hashlib.sha256(identity.encode()).hexdigest()+'.json'


@contextmanager
def lock(folder):
    path = folder / 'run.lock'
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        path.unlink()


def rankings(records, seed):
    eligible = [r for r in records if r['status'] == 'complete']
    ordered = sorted(eligible, key=lambda r: (-r['score'], digest([seed, r['id']])))
    result, last_score, rank = [], None, 0
    for index, row in enumerate(ordered, 1):
        if row['score'] != last_score:
            rank, last_score = index, row['score']
        result.append({'id': row['id'], 'score': row['score'], 'rank': rank})
    return result


def validate_run_directory(folder):
    folder = Path(folder)
    require(not (folder / 'cleanup-failed.json').exists(), 'cleanup_failure_invalidates_run')


def run(freeze_path, output=None, resume=None, worker_factory=Worker):
    frozen = read_json(freeze_path)
    verify(frozen)
    require(bool(output) != bool(resume), 'choose_new_run_or_resume')
    folder = external_path(resume) if resume else new_directory(output)
    require(folder.is_dir(), 'run_directory_missing')
    if resume:
        stored = read_json(folder / 'freeze.json')
        require(stored == frozen, 'resume_fingerprint_mismatch')
    else:
        publish(folder / 'freeze.json', frozen)
    config, manifest = frozen['config'], frozen['manifest']
    with lock(folder), cancellation():
        validate_run_directory(folder)
        existing = {}
        for row in manifest['candidates']:
            path = folder / filename(row['id'])
            if path.exists():
                record = read_json(path)
                require(record['id'] == row['id'] and record['freeze_sha256'] == frozen['sha256'], 'checkpoint_mismatch')
                saved_hash = record.pop('sha256', None)
                require(digest(record) == saved_hash, 'checkpoint_hash_mismatch')
                record['sha256'] = saved_hash
                existing[row['id']] = record
        unexpected = set(folder.glob('paper-*.json')) - {folder / filename(r['id']) for r in manifest['candidates']}
        require(not unexpected, 'unexpected_checkpoints')
        require(not any(r['status'] == 'failed' for r in existing.values()), 'failed_run_requires_new_run')
        elapsed_before = sum(r.get('elapsed_seconds', 0) for r in existing.values())
        remaining = config['limits']['run_seconds'] - elapsed_before
        require(remaining > 0, 'run_budget_exhausted')
        deadline = time.monotonic()+remaining
        memory_before = machine_memory()
        worker = None
        records = list(existing.values())
        try:
            for row in manifest['candidates']:
                if row['id'] in existing:
                    continue
                intent = folder / ('attempt-' + filename(row['id']))
                require(not intent.exists(), 'inflight_attempt_requires_new_run')
                publish(intent, {'id': row['id'], 'freeze_sha256': frozen['sha256']})
                start = time.monotonic()
                record = {'id': row['id'], 'freeze_sha256': frozen['sha256'], 'status': 'insufficient_metadata',
                          'score': None, 'pairs': [], 'model_calls': 0}
                try:
                    s = row['selection']
                    if s['status'] == 'ready':
                        if worker is None:
                            worker = worker_factory(config['adapter'], config['limits'], deadline)
                            worker.expected_runtime = frozen.get('learned_runtime')
                            worker.start()
                        for index, interest in enumerate(config['interests']):
                            payload = {**s['payload'], 'interest': interest}
                            seq = worker.calls
                            phase = 'first_after_load' if seq == 0 else 'warm'
                            worker.calls += 1
                            record['model_calls'] += 1
                            before = time.monotonic()
                            answer = worker.request({'seq': seq, 'interest_index': index, 'payload': payload}, config['limits']['pair_seconds'])
                            score = answer.get('score')
                            if (answer.get('seq') != seq or answer.get('payload_sha256') != digest(payload)
                                    or type(score) not in (int, float) or not math.isfinite(score)):
                                raise ExecutionFailure('invalid_pair_response')
                            cpu = answer.get('cpu_seconds')
                            if type(cpu) not in (int, float) or not math.isfinite(cpu) or cpu < 0:
                                raise ExecutionFailure('invalid_cpu_measurement')
                            if config['adapter']['kind'] == 'minilm_onnx':
                                expected_tokens = next(item['tokens'] for item in frozen['token_counts']
                                    if item['id'] == row['id'] and item['interest_index'] == index)
                                if type(answer.get('tokens')) is not int or answer['tokens'] != expected_tokens:
                                    raise ExecutionFailure('token_count_changed_since_freeze')
                            record['pairs'].append({'interest_index': index, 'score': score, 'phase': phase, 'tokens': answer.get('tokens'),
                                 'payload_sha256': digest(payload), 'latency_seconds': time.monotonic()-before, 'cpu_seconds': cpu})
                        record.update(status='complete', score=max(p['score'] for p in record['pairs']))
                    else:
                        record['reason'] = s['reason']
                except (ExecutionFailure, KeyboardInterrupt) as error:
                    record.update(status='failed', score=None, error='cancelled' if isinstance(error, KeyboardInterrupt) else str(error))
                record.update(elapsed_seconds=time.monotonic()-start,
                    peak_process_group_rss_bytes=worker.peak_rss if worker else None,
                    memory_measurement_missing=worker.measurement_missing if worker else None,
                    load_seconds=worker.load_seconds if worker else None)
                record['sha256'] = digest(record)
                publish(folder / filename(row['id']), record)
                records.append(record)
                if record['status'] == 'failed':
                    break
        finally:
            if worker is not None:
                try:
                    worker.close()
                except (ExecutionFailure, OSError):
                    publish(folder / 'cleanup-failed.json', {'status': 'cleanup_incomplete'})
                    raise ExecutionFailure('cleanup_incomplete') from None
        summary = {'freeze_sha256': frozen['sha256'], 'rankings': rankings(records, config['tie_seed']),
                   'counts': {status: sum(r['status'] == status for r in records) for status in ('complete', 'failed', 'insufficient_metadata')},
                   'pending': len(manifest['candidates'])-len(records), 'synthetic_only': True,
                   'memory_scope': 'sampled cumulative process-group RSS; shared pages can be double counted; spikes can be missed',
                   'cold_definition': 'fresh process, OS cache not cleared',
                   'internal_retries': 0, 'live_model_calls': sum(r['model_calls'] for r in records) if config['adapter']['kind'] == 'minilm_onnx' else 0,
                   'machine_memory_before': memory_before, 'machine_memory_after': machine_memory()}
        pairs = [pair for record in records for pair in record['pairs']]
        def latency_stats(values):
            values = sorted(values)
            if not values:
                return {'count': 0, 'p50_seconds': None, 'p95_seconds': None}
            return {'count': len(values), 'p50_seconds': values[math.ceil(.5*len(values))-1],
                    'p95_seconds': values[math.ceil(.95*len(values))-1]}
        summary['latency'] = {phase: latency_stats([p['latency_seconds'] for p in pairs if p['phase']==phase])
                              for phase in ('first_after_load','warm')}
        summary['per_paper_latency'] = latency_stats([r['elapsed_seconds'] for r in records if r['status']=='complete'])
        summary['adapter_pair_calls'] = sum(r['model_calls'] for r in records)
        summary['peak_process_group_rss_bytes'] = max((r['peak_process_group_rss_bytes'] or 0 for r in records), default=0) or None
        # Each invocation has its own immutable summary; old reports remain untouched.
        invocation = 1
        while (folder / f'summary-{invocation}.json').exists():
            invocation += 1
        publish(folder / f'summary-{invocation}.json', summary)
        return summary

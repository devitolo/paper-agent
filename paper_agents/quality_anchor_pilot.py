"""Manual development evidence pilot; no production ranking or database access."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import time

from paper_agents.gemini_process import run_gemini

DIMENSIONS = {'novelty', 'technical_depth', 'evidence', 'baseline_quality', 'operational_realism'}
PAPERS = {'crystallization': 1, 'teller': 2}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_packet(root, paper):
    index = PAPERS[paper]
    manifest = json.loads((root / 'checksums.json').read_text())
    contents = {}
    for filename in (f'{paper}-evidence.json', f'prompt-{index}.txt'):
        data = (root / filename).read_bytes()
        require(hashlib.sha256(data).hexdigest() == manifest[filename], 'Input checksum mismatch')
        contents[filename] = data.decode('utf-8')
    packet = json.loads(contents[f'{paper}-evidence.json'])
    passages = packet['passages']
    require(0 < len(passages) <= 8, 'Invalid passage count')
    require(len({p['id'] for p in passages}) == len(passages), 'Duplicate passage IDs')
    require(sum(len(p['text']) for p in passages) <= 7000, 'Text budget exceeded')
    for p in passages:
        require(0 < len(p['text']) <= 2000, 'Window budget exceeded')
        require(type(p['start']) is int and type(p['end']) is int and 0 <= p['start'] < p['end'], 'Invalid offsets')
        require(packet['regions'][p['region']]['text'][p['start']:p['end']] == p['text'], 'Source slice mismatch')
    prompt = contents[f'prompt-{index}.txt']
    # The frozen prompt must contain exactly the validated excerpts.
    supplied = json.loads(prompt.split('\nExcerpts:\n', 1)[1])
    require(supplied == [{'id': p['id'], 'text': p['text']} for p in passages], 'Prompt/packet mismatch')
    return packet, prompt


def validate(value, packet):
    require(isinstance(value, dict) and isinstance(value.get('dimensions'), dict), 'Missing dimensions')
    require(set(value['dimensions']) == DIMENSIONS, 'Invalid dimensions')
    passages = {p['id']: p['text'] for p in packet['passages']}
    for row in value['dimensions'].values():
        require(isinstance(row, dict), 'Invalid dimension')
        require(row.get('status') in ('complete', 'unknown'), 'Invalid status')
        score = row.get('score')
        if row['status'] == 'unknown':
            require(score is None, 'Unknown must have null score')
        else:
            require(type(score) is int and 0 <= score <= 4, 'Invalid score')
            require(bool(row.get('citations')), 'Scored dimension requires citation')
        require(isinstance(row.get('rationale'), str) and 0 < len(row['rationale']) <= 1200, 'Invalid rationale')
        require(isinstance(row.get('citations'), list), 'Invalid citations')
        for citation in row['citations']:
            require(isinstance(citation, dict), 'Invalid citation')
            quote = citation.get('quote')
            require(isinstance(quote, str) and bool(quote.strip()), 'Empty citation')
            require(citation.get('passage_id') in passages, 'Unknown passage')
            require(quote in passages[citation['passage_id']], 'Quote mismatch')
    return value


def execute(jobs, model, output, timeout, provider=run_gemini):
    """Stop after any failure. CLI internal retries may occur before detection."""
    for paper, packet, prompt in jobs:
        started = time.monotonic()
        metadata = {}
        result = {'paper': paper, 'model_requested': model, 'resolved_model': 'unknown',
                  'usage': 'unknown', 'status': 'failed', 'stage': 'provider',
                  'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                  'pdf_sha256': packet.get('pdf_sha256') if packet else None,
                  'semantic_support_review': 'pending', 'wrapper_retries': 0}
        try:
            raw = provider(['gemini', '--model', model, '--output-format', 'stream-json', '-p', prompt],
                           timeout, stream_json=True, metadata=metadata, fail_fast_provider_errors=True)
            (output / f'{paper}-response.txt').write_text(raw)
            result['stage'] = 'validation'
            if packet:
                result['assessment'] = validate(json.loads(raw), packet)
                result['status'] = 'structurally_valid'
            else:
                require(raw.strip() == 'OK', 'Connectivity response was not OK')
                result['status'] = 'connectivity_ok'
        except RuntimeError as error:
            # Our transport emits sanitized operational diagnostics, not provider stderr.
            result['error'] = str(error)
        except (ValueError, KeyError, TypeError, IndexError):
            result['error'] = 'Evidence or response validation failed; inspect saved response locally.'
        finally:
            result.update(elapsed_seconds=round(time.monotonic() - started, 3), transport=metadata)
            (output / f'{paper}-result.json').write_text(json.dumps(result, indent=2))
        print(f"{paper}: {result['status']}", flush=True)
        if result['status'] == 'failed':
            print('Stopped; no further paper calls made.', flush=True)
            return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packet', type=Path, default=Path('/tmp/quality-anchor-pilot'))
    parser.add_argument('--model', required=True, help='Explicit Gemini model ID; auto is rejected')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='One 30-second connectivity call')
    mode.add_argument('--run', action='store_true', help='Assess selected paper(s), 180 seconds per call')
    parser.add_argument('--paper', choices=(*PAPERS, 'both'), default='crystallization')
    args = parser.parse_args()
    if not args.model.strip() or args.model.lower().startswith('auto'):
        parser.error('Choose an explicit model ID, not auto')
    if args.check:
        jobs = [('connectivity', None, 'Reply with OK only. Do not use tools.')]
    else:
        names = list(PAPERS) if args.paper == 'both' else [args.paper]
        jobs = [(name, *load_packet(args.packet, name)) for name in names]
        print('Verified frozen input hashes, exact source slices and passage budgets.', flush=True)
    if not (args.check or args.run):
        return
    if not shutil.which('gemini'):
        parser.error('Gemini CLI unavailable; no calls made')
    output = Path(tempfile.mkdtemp(prefix='paper-quality-v2-'))
    print(f'Results: {output}', flush=True)
    raise SystemExit(0 if execute(jobs, args.model, output, 30 if args.check else 180) else 1)


if __name__ == '__main__':
    main()

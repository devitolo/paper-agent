"""Container-only synthetic smoke entry point; bounded externally to 900 seconds."""
from pathlib import Path
from . import dataset, freeze, runner
from .contracts import read_json, require


def main():
    config = Path(__file__).parent / 'config'
    output = Path('/output')
    manifest = dataset.prepare(read_json(config/'minilm-smoke-papers.json'),
        read_json(config/'minilm-smoke-text-policy.json'), output/'data')
    frozen = freeze.create(manifest, config/'minilm-smoke.json', output/'freeze')
    result = runner.run(frozen, output=output/'run')
    require(result['counts'] == {'complete':3,'failed':0,'insufficient_metadata':0}
            and result['pending'] == 0 and result['adapter_pair_calls'] == 9
            and result['live_model_calls'] == 9, 'smoke_incomplete')
    print('Synthetic integration smoke complete: 3 papers, 9 pairs; no quality claim.', flush=True)


if __name__ == '__main__':
    main()

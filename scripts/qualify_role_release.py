"""Hosted production image checks; no nonprod startup/HTTP/inference tests."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, timeout=180)


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REF') != 'refs/heads/main':
        raise RuntimeError('Main Actions release required')
    image = os.environ['APP_IMAGE']
    if not re.fullmatch(r'ghcr.io/devitolo/paper-agent@sha256:[0-9a-f]{64}', image):
        raise RuntimeError('Immutable image required')
    docker('pull', image)
    info = json.loads(docker('image', 'inspect', image))[0]
    assert (info['Os'], info['Architecture']) == ('linux', 'amd64')
    assert info['Config']['Labels']['org.opencontainers.image.revision'] == os.environ['GITHUB_SHA']
    assert info['Config']['Labels']['org.projectpaper.source-bundle-sha256'] == os.environ['SOURCE_BUNDLE']
    run = ['run', '--rm', '--network', 'none', '--read-only', '--tmpfs', '/tmp:rw,size=64m', '--entrypoint', 'python', image]
    packages = json.loads(docker(*run, '-c', "import json,importlib.metadata as m;print(json.dumps({d.metadata['Name'].lower():d.version for d in m.distributions()}))"))
    assert not set(packages) & {'onnxruntime', 'transformers', 'numpy', 'tokenizers', 'torch'}
    docker(*run, '-m', 'pip', 'check')
    docker(*run, '-c', 'import paper_agents.pipeline_runtime,paper_agents.package_runtime,paper_agents.migration_job,paper_agents.gemini_runtime')
    hashes = json.loads(docker(*run, '-c', "import json,hashlib;from pathlib import Path;print(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('paper_agents').glob('*.py')}))"))
    assert hashes
    for path, value in hashes.items():
        assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == value
    docker('run', '--rm', '--network', 'none', '--read-only', '--tmpfs', '/tmp:rw,size=64m',
           '--mount', 'type=bind,src=' + str(Path.cwd()/'tests/runtime_roles') + ',dst=/app/tests/runtime_roles,readonly',
           '--entrypoint', 'python', image, '-m', 'unittest', 'discover', '-s', 'tests/runtime_roles', '-v')
    evidence = {'revision': os.environ['GITHUB_SHA'], 'source_bundle_sha256': os.environ['SOURCE_BUNDLE'],
                'image': image, 'config_id': info['Id'], 'platform': 'linux/amd64', 'packages': packages,
                'source_bytes_match': True, 'production_role_checks': 'passed',
                'nonprod_tests': 'held; no nonprod startup/HTTP/inference/browser checks',
                'run_url': 'https://github.com/'+os.environ['GITHUB_REPOSITORY']+'/actions/runs/'+os.environ['GITHUB_RUN_ID']}
    Path('role-release-evidence.json').write_text(json.dumps(evidence, indent=2)+'\n')


if __name__ == '__main__':
    main()

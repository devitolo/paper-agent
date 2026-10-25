#!/usr/bin/env python3
"""Freeze and test observability recovery on local Docker; never target production."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

REPO = Path(__file__).resolve().parents[1]
INDEPENDENT = ('test_observability_installer_linux_qa', 'test_observability_recovery_matrix_qa')
ESTABLISHED = ('test_host_resource_collector', 'test_resource_status',
               'test_observability_release', 'test_observability_container')
WORKER = '''import json,sys,unittest
sys.path.insert(0,"/qa-source")
suite=unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])
class Evidence(unittest.TextTestResult):
 def __init__(self,*a,**kw):super().__init__(*a,**kw);self.passed=0
 def addSuccess(self,t):self.passed+=1;super().addSuccess(t)
r=unittest.TextTestRunner(verbosity=2,resultclass=Evidence).run(suite)
value={'run':r.testsRun,'passed':r.passed,'failures':[t.id() for t,_ in r.failures],
       'errors':[t.id() for t,_ in r.errors],'skipped':[{'test':t.id(),'reason':why} for t,why in r.skipped]}
print('QA_RESULT='+json.dumps(value),flush=True)
sys.exit(0 if r.wasSuccessful() and not r.skipped else 1)
'''


def cli(*args, timeout=30):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode: raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='Candidate checkout; never a production directory')
    parser.add_argument('--image', required=True, help='Explicit existing local AMD64 Linux/Python QA image')
    parser.add_argument('--output', type=Path, help='New evidence directory; defaults to ignored qa/results')
    parser.add_argument('--timeout', type=int, default=300)
    args = parser.parse_args()
    if args.timeout < 60: parser.error('timeout must include the 45-second client-timeout probe')
    source = args.source.resolve()
    if not (source / 'deploy/observability/install_container_cron.sh').is_file():
        parser.error('source must contain the observability package')
    if os.environ.get('DOCKER_HOST') and not os.environ['DOCKER_HOST'].startswith('unix://'):
        parser.error('remote DOCKER_HOST is prohibited')
    context = json.loads(cli('context', 'inspect'))[0]
    if not context['Endpoints']['docker']['Host'].startswith('unix://'):
        parser.error('Docker context must use a local Unix socket')
    info = json.loads(cli('image', 'inspect', args.image))[0]
    if info['Architecture'] != 'amd64': parser.error('explicit local image must be AMD64')
    output = (args.output or REPO / 'qa/results' /
              ('observability-recovery-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))).resolve()
    output.mkdir(parents=True, exist_ok=False)
    frozen = output / 'source'; frozen.mkdir()
    paths = ['deploy/observability', '.github/workflows/observability-release.yml',
             'scripts/host_resource_collector.py', 'scripts/resource_collector_job.sh',
             'scripts/install_resource_collector_cron.sh', 'scripts/remove_resource_collector_cron.sh',
             'paper_agents/__init__.py', 'paper_agents/resource_status.py']
    paths += ['tests/' + name + '.py' for name in ESTABLISHED]
    for relative in paths:
        origin = source / relative; target = frozen / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if origin.is_dir(): shutil.copytree(origin, target, ignore=shutil.ignore_patterns('__pycache__'))
        else: shutil.copy2(origin, target)
    for name in INDEPENDENT:
        shutil.copy2(REPO / 'tests' / (name + '.py'), frozen / 'tests' / (name + '.py'))
    (frozen / 'tests/__init__.py').write_text('')
    hashes = {str(p.relative_to(frozen)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in frozen.rglob('*') if p.is_file()}
    revision = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'],
                              text=True, capture_output=True)
    evidence = {'source': str(source), 'git_revision': revision.stdout.strip() or None,
                'note': 'File hashes identify uncommitted candidate and QA overrides; revision alone is insufficient.',
                'context': context['Name'], 'image_id': info['Id'], 'image_architecture': info['Architecture'],
                'source_sha256': hashes, 'production_access': False,
                'scope': 'Disposable installer/recovery and package suite; no live daemon-cleanup or production E2E certification'}
    (output / 'evidence.json').write_text(json.dumps(evidence, indent=2) + '\n')
    name = 'paper-observability-recovery-qa-' + uuid.uuid4().hex[:12]
    command = ['docker', 'run', '--rm', '--pull', 'never', '--name', name,
               '--platform', 'linux/amd64', '--network', 'none', '--user', '0:0',
               '--memory', '256m', '--pids-limit', '128', '-e', 'PAPER_OBSERVABILITY_QA_ISOLATED=1',
               '--mount', 'type=bind,source=' + str(frozen) + ',target=/qa-source,readonly',
               '--entrypoint', 'python3', info['Id'], '-c', WORKER]
    command += ['tests.' + module for module in (*INDEPENDENT, *ESTABLISHED)]
    result_code = 1
    try:
        with (output / 'tests.log').open('w') as log:
            result_code = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                         timeout=args.timeout).returncode
    except subprocess.TimeoutExpired:
        evidence['timed_out'] = True
    finally:
        cleanup = subprocess.run(['docker', 'rm', '-f', name], text=True, capture_output=True, timeout=20)
        evidence['cleanup_result'] = cleanup.returncode
        if cleanup.returncode and 'No such container' not in cleanup.stderr:
            evidence['cleanup_error'] = cleanup.stderr.strip()
            result_code = 1
    lines = (output / 'tests.log').read_text().splitlines()
    result = next((json.loads(line[len('QA_RESULT='):]) for line in reversed(lines)
                   if line.startswith('QA_RESULT=')), None)
    evidence['result'] = result; evidence['exit_code'] = result_code
    (output / 'evidence.json').write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps({'evidence': str(output), 'exit_code': result_code, 'result': result}, indent=2))
    return result_code if result_code else (0 if result and not result['skipped'] else 1)


if __name__ == '__main__':
    sys.exit(main())

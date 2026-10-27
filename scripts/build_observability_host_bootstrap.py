#!/usr/bin/env python3
"""Prepare a checksum-pinned one-time bootstrap bundle; never installs anything."""
import argparse
import ast
import hashlib
import json
from pathlib import Path

GH_VERSION = '2.102.0'
GH_SHA256 = '7469124f706944133d6a169691dd1c6c3511b12e85878d255e044e2948df4c9b'
SOURCES = {
    'package-install.py': 'scripts/observability_host_package_install.py',
    'collect-publish.sh': 'deploy/observability/collect-publish.sh',
    'project-paper-observability-deploy': 'deploy/observability/project-paper-observability-deploy',
    'run-container.sh': 'deploy/observability/run-container.sh',
    'sudoers': 'deploy/observability/sudoers.host-package.example',
}


def read_regular(path, limit):
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError('single regular source file required')
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('source file too large')
    return data


def build(root, output, verifier):
    root, output, verifier = Path(root), Path(output), Path(verifier)
    if output.is_symlink() or output.exists():
        raise ValueError('new output directory required')
    contents = {name: read_regular(root / relative, 1024*1024) for name,relative in SOURCES.items()}
    contents['gh'] = read_regular(verifier, 100*1024*1024)
    if hashlib.sha256(contents['gh']).hexdigest() != GH_SHA256:
        raise ValueError('official pinned Linux AMD64 GitHub CLI required')
    hashes = {name:hashlib.sha256(data).hexdigest() for name,data in contents.items()}
    template = read_regular(root / 'scripts/observability_host_bootstrap.py', 1024*1024).decode()
    assignments = [node for node in ast.parse(template).body if isinstance(node,ast.Assign)
                   and any(isinstance(target,ast.Name) and target.id=='SOURCE_HASHES' for target in node.targets)]
    if len(assignments)!=1:
        raise ValueError('unexpected bootstrap template')
    node=assignments[0];lines=template.splitlines(keepends=True)
    lines[node.lineno-1:node.end_lineno]=['SOURCE_HASHES = '+repr(hashes)+'\n']
    bootstrap=''.join(lines).encode()
    compile(bootstrap,'bootstrap.py','exec')
    contents['bootstrap.py']=bootstrap
    output.mkdir(mode=0o700)
    for name,data in contents.items():
        path=output/name;path.write_bytes(data);path.chmod(0o600)
    manifest={'schema':'project-paper.observability-bootstrap-review.v1',
              'target':'mini-production','verifier_version':GH_VERSION,
              'files':{name:hashlib.sha256(data).hexdigest() for name,data in contents.items()}}
    (output/'bootstrap-review.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--verifier',required=True,type=Path)
    parser.add_argument('--output-directory',required=True,type=Path)
    args=parser.parse_args()
    result=build(Path(__file__).resolve().parents[1],args.output_directory,args.verifier)
    print(json.dumps({'target':result['target'],'bootstrap_sha256':result['files']['bootstrap.py']}))

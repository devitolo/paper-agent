#!/usr/bin/env python3
"""Build the bounded Mini host artifact; no installation or credential access."""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile

FILES = ('deploy/observability/collect-publish.sh',
         'deploy/observability/project-paper-observability-deploy',
         'deploy/observability/run-container.sh')


def build(root, output, source_sha, image):
    if not re.fullmatch('[0-9a-f]{40}', source_sha):
        raise ValueError('exact source commit required')
    if not re.fullmatch(r'ghcr\.io/devitolo/paper-agent/observability:observability-mini@sha256:[0-9a-f]{64}', image):
        raise ValueError('exact Mini immutable image required')
    output.mkdir(parents=True, exist_ok=True)
    manifest_files = []
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w', format=tarfile.USTAR_FORMAT) as package:
        for name in FILES:
            path = root / name
            if path.is_symlink() or not path.is_file():
                raise ValueError('regular source file required')
            data = path.read_bytes()
            if len(data) > 131072 or b'host-package-pending-guard-v1' not in data:
                raise ValueError('bounded guarded entrypoint required')
            member = tarfile.TarInfo(name)
            member.size = len(data);member.mode = 0o755
            member.uid = member.gid = member.mtime = 0
            package.addfile(member, io.BytesIO(data))
            manifest_files.append({'path': name, 'sha256': hashlib.sha256(data).hexdigest(),
                                   'mode': '0755', 'owner': 'root:root'})
    data = archive.getvalue()
    if len(data) > 1024 * 1024:
        raise ValueError('artifact too large')
    digest = hashlib.sha256(data).hexdigest()
    manifest = {'schema': 'project-paper.observability-host-package.v2',
                'target': 'mini-production', 'source_sha': source_sha,
                'artifact_sha256': digest, 'image': image, 'files': manifest_files,
                'installer_version': 2, 'created_at': datetime.now(timezone.utc).isoformat()}
    (output / 'artifact.tar').write_bytes(data)
    (output / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True, separators=(',', ':'))+'\n')
    return digest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--output-directory', type=Path, required=True)
    args = parser.parse_args()
    print(build(Path(__file__).resolve().parents[1], args.output_directory, args.source_sha, args.image))

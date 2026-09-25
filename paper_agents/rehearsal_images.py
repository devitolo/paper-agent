"""Explicit local config-ID rehearsal evidence; never a registry digest substitute."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

RECEIPT = Path('/rehearsal/image-identity.json')
INVENTORY = Path('/app/image-runtime-inputs.json')
BUNDLE_LABEL = 'org.projectpaper.source-bundle-sha256'
REVISION_LABEL = 'org.opencontainers.image.revision'


def config_id(value):
    if not isinstance(value, str) or re.fullmatch(r'sha256:[0-9a-f]{64}', value) is None:
        raise ValueError('Local rehearsal requires full Docker config image IDs, not tags or registry digests')
    return value


def require_hash(value):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None:
        raise ValueError('Local rehearsal requires source bundle SHA256 provenance')
    return value


def require_revision(value):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{40}', value) is None:
        raise ValueError('Local rehearsal requires the full source base revision')
    return value


def read_record(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('Rehearsal identity record exceeds limit')
    return json.loads(raw)


def validate_runtime():
    try:
        app = config_id(os.environ.get('PAPER_APP_IMAGE'))
        ollama = config_id(os.environ.get('PAPER_MIGRATION_OLLAMA_IMAGE'))
        expected = require_hash(os.environ.get('PAPER_REHEARSAL_SOURCE_SHA256'))
        receipt, inventory = read_record(RECEIPT), read_record(INVENTORY)
        if receipt != {'format':1, 'identity_mode':'local-rehearsal', 'app_image':app,
                       'ollama_image':ollama, 'source_bundle_sha256':expected,
                       'source_revision':require_revision(receipt.get('source_revision'))}:
            raise ValueError
        if (inventory.get('source_bundle_sha256') != expected
                or inventory.get('source_revision') != receipt['source_revision']):
            raise ValueError
    except (OSError, ValueError, TypeError, AttributeError):
        raise ValueError('Local rehearsal image identity/provenance mismatch or missing evidence') from None


def inspect_image(identity):
    config_id(identity)
    try:
        result = subprocess.run(['docker','image','inspect',identity,'--format','{{json .}}'],
                                capture_output=True,text=True,check=True,timeout=15)
        value = json.loads(result.stdout)
        if value.get('Id') != identity:
            raise ValueError
        return value
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        raise ValueError('Exact local image is unavailable or inspect returned a different identity') from None


def capture(bundle, app, ollama):
    config_id(app); config_id(ollama)  # Reject mutable references before Docker.
    digest = hashlib.sha256()
    with Path(bundle).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    source_hash = digest.hexdigest()
    inspected = inspect_image(app)
    inspect_image(ollama)
    config = inspected.get('Config')
    if not isinstance(config, dict) or not isinstance(config.get('Labels'), dict):
        raise ValueError('Loaded application image lacks source provenance labels')
    labels = config['Labels']
    if labels.get(BUNDLE_LABEL) != source_hash:
        raise ValueError('Loaded application image does not match this source bundle')
    revision = require_revision(labels.get(REVISION_LABEL))
    return {'format':1, 'identity_mode':'local-rehearsal', 'app_image':app,
            'ollama_image':ollama, 'source_bundle_sha256':source_hash, 'source_revision':revision}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-bundle',required=True,type=Path)
    parser.add_argument('--app-image',required=True)
    parser.add_argument('--ollama-image',required=True)
    parser.add_argument('--output',required=True,type=Path)
    args = parser.parse_args()
    try:
        record = capture(args.source_bundle,args.app_image,args.ollama_image)
        with args.output.open('x') as stream:
            json.dump(record,stream,indent=2)
            stream.write('\n')
        args.output.chmod(0o444)  # Nonsecret receipt must be readable by container UID10001.
    except (OSError, ValueError):
        parser.exit(1,'Rehearsal identity verification failed; no usable receipt issued.\n')
    print('Verified exact local images and source bundle; rehearsal receipt written.')


if __name__ == '__main__':
    main()

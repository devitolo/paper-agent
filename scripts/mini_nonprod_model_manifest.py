"""Bounded read-only model-tree inventory. CLI permits only the pinned ONNX hash."""
import argparse
import hashlib
import json
from pathlib import Path
import stat
import time

EXPECTED_ONNX = '5d3e70fd0c9ff14b9b5169a51e957b7a9c74897afd0a35ce4bd318150c1d4d4a'

def inspect(root, expected=EXPECTED_ONNX):
    root = Path(root).absolute()
    if not root.is_dir() or any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError('Model root absent or aliased')
    start = time.monotonic()
    files = {}
    total = 0
    for path in sorted(root.rglob('*')):
        if time.monotonic() - start > 120:
            raise RuntimeError('Model inventory time budget exceeded')
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode):
            raise ValueError('Model tree contains an alias')
        if stat.S_ISDIR(before.st_mode):
            continue
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError('Model tree contains a non-regular or linked file')
        total += before.st_size
        if total > 1024 ** 3 or len(files) >= 1000:
            raise RuntimeError('Model inventory size/count budget exceeded')
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                if time.monotonic() - start > 120:
                    raise RuntimeError('Model inventory time budget exceeded')
                digest.update(block)
        after = path.stat()
        if (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns):
            raise RuntimeError('Model file changed during inventory')
        files[str(path.relative_to(root))] = {'bytes': before.st_size, 'sha256': digest.hexdigest()}
    if files.get('onnx/model.onnx', {}).get('sha256') != expected:
        raise RuntimeError('ONNX identity differs from approved source constant')
    if 'tokenizer_config.json' not in files or not ({'tokenizer.json', 'vocab.txt'} & files.keys()):
        raise RuntimeError('Required offline tokenizer files absent')
    return {'root': str(root), 'files': files, 'total_bytes': total,
            'onnx_identity': 'matches-recorded-source',
            'tokenizer_provenance': 'current-files-observed-not-upstream-attested'}

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('model_dir', type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect(args.model_dir), sort_keys=True))

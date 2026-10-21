"""Read-only Mini production capture and explicit, mutable nonprod snapshot state.

The operator owns writer fences and destination replacement. This module never
writes production, imports credentials, migrates schemas, or runs providers.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import time
import uuid

from .import_state import digest, file_hash, schema, trusted_schemas
from .topics import load_topic_config
from .industry_config import load_zenml_config

PROVENANCE = 'data/NONPROD_SNAPSHOT.json'
SOURCE = {'kind': 'mini-production', 'container': 'paper-mini-production-app-1',
          'data_volume': 'paper-mini-rehearsal-production-data',
          'config_volume': 'paper-mini-rehearsal-production-config'}
REQUIRED = {'data/paper_agent.db', 'data/profile.json', 'config/topics.yaml', 'config/industry.json'}
MAX_BYTES = 1024**3
MAX_FILES = 20000


def require(value, message):
    if not value:
        raise RuntimeError(message)


def safe_path(root, relative, *, exists=True):
    root = Path(root).absolute()
    relative = Path(relative)
    require(not relative.is_absolute() and '..' not in relative.parts and relative.parts,
            'Unsafe snapshot relative path')
    require(not any(p.is_symlink() for p in (root, *root.parents)), 'Aliased snapshot root')
    path = root
    for part in relative.parts:
        path /= part
        require(not path.is_symlink(), 'Aliased snapshot entry')
    if exists:
        info = path.stat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'Expected unlinked regular file')
    return path


def readonly(path):
    require(os.statvfs(path).f_flag & os.ST_RDONLY, 'Production source mount must be read-only')


@lru_cache(maxsize=1)
def allowed_schemas():
    return {digest(value) for value in trusted_schemas()}


def database_info(path, *, full=True):
    with closing(sqlite3.connect(Path(path).absolute().as_uri()+'?mode=ro', uri=True, timeout=5)) as db:
        actual = schema(db)
        fingerprint = digest(actual)
        require(fingerprint in allowed_schemas(), 'Snapshot schema unsupported; no migration permitted')
        if full:
            require(db.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'Snapshot SQLite integrity failed')
            require(not db.execute('PRAGMA foreign_key_check').fetchall(), 'Snapshot foreign keys invalid')
        tables = [row[1] for row in actual if row[0] == 'table']
        counts = {name: db.execute('SELECT count(*) FROM "'+name.replace('"', '""')+'"').fetchone()[0]
                  for name in tables} if full else {}
        artifacts = db.execute('SELECT artifact_type,path FROM artifacts').fetchall() if full else []
        return {'schema_sha256': fingerprint, 'tables': tables, 'counts': counts, 'artifacts': artifacts}


def artifact_path(value):
    path = Path(value)
    require(not path.is_absolute() and '..' not in path.parts and len(path.parts) >= 3
            and path.parts[0] == 'data' and path.parts[1] in {'papers', 'extractions', 'zenml-pilot'}, 'Artifact outside approved data subtree')
    require(path.suffix.lower() in {'.pdf', '.json', '.txt', '.md'}, 'Unreviewed artifact extension')
    require(not any(part.startswith('.') or part.lower() in {'secrets', 'credentials', 'auth'}
                    for part in path.parts), 'Sensitive artifact path refused')
    return path.as_posix()


def load_provenance(root):
    path = safe_path(root, PROVENANCE)
    require(path.stat().st_size < 4*1024**2, 'Snapshot provenance too large')
    value = json.loads(path.read_text())
    require(value.get('format') == 1 and value.get('source') == SOURCE,
            'Not an explicit Mini-production nonprod snapshot')
    require(str(uuid.UUID(value['snapshot_id'])) == value['snapshot_id'], 'Invalid snapshot identity')
    require(datetime.fromisoformat(value['captured_at']).utcoffset() is not None, 'Snapshot time needs timezone')
    require(re.fullmatch(r'[0-9a-f]{40}', value.get('source_revision', '')), 'Invalid source revision')
    require(re.fullmatch(r'ghcr.io/devitolo/paper-agent@sha256:[0-9a-f]{64}', value.get('source_image', '')),
            'Invalid source image')
    require(value.get('schema_sha256') in allowed_schemas(), 'Unsupported snapshot schema identity')
    require(REQUIRED <= value.get('files', {}).keys(), 'Incomplete snapshot')
    require(len(value['files']) <= MAX_FILES + len(REQUIRED), 'Too many snapshot files')
    require(all(isinstance(h, str) and re.fullmatch(r'[0-9a-f]{64}', h)
                for h in value['files'].values()), 'Invalid snapshot hash')
    require(0 < value.get('copied_bytes', 0) <= MAX_BYTES, 'Snapshot size outside reviewed bound')
    require(value.get('sync') == 'disabled' and value.get('credentials') == 'excluded', 'Unsafe snapshot contract')
    return value


def validate_runtime(root, *, full=True):
    from .runtime_config import migration_settings
    require(os.environ.get('PAPER_AGENT_STARTUP_MODE') == 'serving-snapshot', 'Explicit snapshot mode required')
    migration_settings()  # Requires NONPROD + disabled provider/scheduler/sync flags.
    root = Path(root).absolute()
    require(not (root/'data/MINI_NONPROD_SYNTHETIC').exists(), 'Synthetic marker cannot identify a real snapshot')
    value = load_provenance(root)
    role = value.get('destination_role')
    require(role is None or role == os.environ.get('PAPER_AGENT_RUNTIME_ROLE'), 'Snapshot role mismatch')
    for name in REQUIRED:
        safe_path(root, name)
    profile = json.loads((root/'data/profile.json').read_text())
    require(isinstance(profile, dict) and all(isinstance(profile.get(k), list) for k in
            ('interests', 'positive_signals', 'negative_signals', 'feedback_history')), 'Invalid snapshot profile')
    load_topic_config(root/'config/topics.yaml')
    load_zenml_config(root/'config/industry.json')
    info = database_info(root/'data/paper_agent.db', full=full)
    require(info['schema_sha256'] == value['schema_sha256'], 'Snapshot schema drift')
    if full:
        for _kind, name in info['artifacts']:
            safe_path(root, artifact_path(name))
    return {'state': 'mini-production-snapshot', 'tables': info['tables'], 'schema': 'exact',
            'papers': info['counts'].get('papers'), 'snapshot_id': value['snapshot_id']}


def capture(source, destination, revision, image):
    source, destination = Path(source).absolute(), Path(destination).absolute()
    require(not destination.exists() and not destination.is_relative_to(source)
            and not source.is_relative_to(destination), 'Capture needs a new separate destination')
    require(not any(p.is_symlink() for p in destination.parents), 'Aliased snapshot destination')
    for name in ('data', 'config'):
        readonly(source/name)
    require(re.fullmatch(r'[0-9a-f]{40}', revision), 'Invalid production source revision')
    require(re.fullmatch(r'ghcr.io/devitolo/paper-agent@sha256:[0-9a-f]{64}', image), 'Immutable production source required')
    for name in REQUIRED:
        safe_path(source, name)
    require((source/'data/paper_agent.db').stat().st_size <= MAX_BYTES, 'Source DB exceeds size bound')
    baseline = {name: file_hash(source/name) for name in REQUIRED-{'data/paper_agent.db'}}
    destination.mkdir(mode=0o700)
    (destination/'data').mkdir()
    (destination/'config').mkdir()
    with closing(sqlite3.connect((source/'data/paper_agent.db').as_uri()+'?mode=ro', uri=True, timeout=5)) as src:
        with closing(sqlite3.connect(destination/'data/paper_agent.db')) as dst:
            deadline = time.monotonic() + 120
            def progress(*_args):
                require(time.monotonic() < deadline, 'SQLite backup exceeded 120 second bound')
            src.backup(dst, pages=256, progress=progress)
            require(dst.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",),
                    "Snapshot must be self-contained without WAL sidecars")
    info = database_info(destination/'data/paper_agent.db')
    files = {'data/paper_agent.db': file_hash(destination/'data/paper_agent.db')}
    total = (destination/'data/paper_agent.db').stat().st_size
    references = {artifact_path(name): kind for kind, name in info['artifacts']}
    require(len(references) <= MAX_FILES, 'Snapshot file count exceeds reviewed bound')
    for name in sorted((REQUIRED-{'data/paper_agent.db'}) | references.keys()):
        src = safe_path(source, name)
        before = (src.stat().st_size, src.stat().st_mtime_ns, file_hash(src))
        total += before[0]
        require(total <= MAX_BYTES, 'Snapshot copy size exceeds reviewed 1 GiB bound')
        dst = safe_path(destination, name, exists=False)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        files[name] = file_hash(dst)
        require(files[name] == before[2], 'Snapshot file changed while copying')
        after = (src.stat().st_size, src.stat().st_mtime_ns, file_hash(src))
        require(before == after, 'Production artifact changed during capture; acquire writer fences')
    require(all(file_hash(source/name) == value for name, value in baseline.items()), 'Configuration changed during capture')
    value = {'format': 1, 'snapshot_id': str(uuid.uuid4()), 'source': SOURCE,
             'captured_at': datetime.now(timezone.utc).isoformat(), 'source_revision': revision,
             'source_image': image, 'schema_sha256': info['schema_sha256'], 'counts': info['counts'],
             'files': files, 'excluded': ['credentials', 'sync identity', 'unreferenced artifacts', 'models', 'old backups'],
             'copied_bytes': total, 'sync': 'disabled', 'credentials': 'excluded'}
    (destination/PROVENANCE).write_text(json.dumps(value, indent=2)+'\n')
    verify(destination)
    return value


def verify(root):
    root = Path(root).absolute()
    value = load_provenance(root)
    for name, expected in value['files'].items():
        if name not in REQUIRED:
            artifact_path(name)
        require(file_hash(safe_path(root, name)) == expected, 'Snapshot checksum mismatch: '+name)
    entries = [p for folder in ('data', 'config') for p in (root/folder).rglob('*')]
    require(not any(p.is_symlink() for p in entries), 'Aliased snapshot entry')
    actual_files = {p.relative_to(root).as_posix() for folder in ('data', 'config') for p in (root/folder).rglob('*') if not p.is_dir()}
    require(actual_files == set(value['files']) | {PROVENANCE}, 'Unmanifested state/credentials refused')
    info = database_info(root/'data/paper_agent.db')
    require(info['schema_sha256'] == value['schema_sha256'] and info['counts'] == value['counts'], 'Snapshot database identity differs')
    require({name for _, name in info['artifacts']} | REQUIRED == value['files'].keys(), 'Unexpected or missing snapshot artifacts')
    return value


def install(source, destination, role):
    require(role in {'ui', 'pipeline'}, 'Unknown nonprod role')
    source, destination = Path(source).absolute(), Path(destination).absolute()
    require(source != destination and not source.is_relative_to(destination)
            and not destination.is_relative_to(source), 'Overlapping install roots')
    value = verify(source)
    for folder in ('data', 'config'):
        path = destination/folder
        require(not any(p.is_symlink() for p in (path, *path.parents)), 'Aliased destination')
        require(path.is_dir() and not any(path.iterdir()), 'Install requires empty destination volumes')
        require(not (os.statvfs(path).f_flag & os.ST_RDONLY), 'Destination is read-only')
    for name in sorted(value['files']):
        dst = safe_path(destination, name, exists=False)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(safe_path(source, name), dst)
    value['destination_role'] = role
    value['installation_id'] = str(uuid.uuid4())
    (destination/PROVENANCE).write_text(json.dumps(value, indent=2)+'\n')
    verify(destination)
    # The helper runs as root only against controller-created, empty volumes.
    for folder in ('data', 'config'):
        paths = [destination/folder, *(destination/folder).rglob('*')]
        # Descendants first: root helpers drop DAC_OVERRIDE and cannot traverse
        # a 0700 directory after transferring its ownership to the app user.
        for path in sorted(paths, key=lambda p: len(p.parts), reverse=True):
            os.chmod(path, 0o700 if path.is_dir() else 0o600)
            os.chown(path, 10001, 10001)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    cap = sub.add_parser('capture')
    cap.add_argument('source', type=Path)
    cap.add_argument('destination', type=Path)
    cap.add_argument('--source-revision', required=True)
    cap.add_argument('--source-image', required=True)
    check = sub.add_parser('verify')
    check.add_argument('root', type=Path)
    ins = sub.add_parser('install')
    ins.add_argument('source', type=Path)
    ins.add_argument('destination', type=Path)
    ins.add_argument('--role', choices=('ui', 'pipeline'), required=True)
    args = parser.parse_args()
    if args.command == 'capture':
        result = capture(args.source, args.destination, args.source_revision, args.source_image)
    elif args.command == 'install':
        result = install(args.source, args.destination, args.role)
    else:
        result = verify(args.root)
    print(json.dumps({k: result[k] for k in ('snapshot_id', 'captured_at', 'source', 'counts', 'copied_bytes')}, sort_keys=True))


if __name__ == '__main__':
    main()

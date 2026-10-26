"""Consistent serving-state backups; PDFs and model caches remain on Mini."""
from __future__ import annotations
from contextlib import closing
import hashlib
import gzip
import zlib
import json
import shutil
import sqlite3
import time
import os
import tarfile
import tempfile
import os
import urllib.request
import urllib.parse
from pathlib import Path

_REQUIRED = {'paper_agent.db', 'config/topics.yaml', 'config/industry.json', 'data/profile.json'}

def _safe(root: Path, name: str) -> Path:
    relative = Path(name)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Unsafe backup path')
    target = (root / relative).resolve()
    if root.resolve() not in target.parents:
        raise ValueError('Backup path escapes root')
    return target

def _check_db(path: Path) -> None:
    if not path.is_file():
        raise ValueError('Missing database')
    with closing(sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True)) as conn:
        if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('Database integrity failed')
        if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise ValueError('Database foreign keys failed')

def create_backup(db_path: Path, config_dir: Path, destination: Path, root: Path | None = None, *, require_canonical: bool = False) -> Path:
    db_path, config_dir, destination = map(Path, (db_path, config_dir, destination))
    root = Path(root) if root is not None else config_dir.parent
    _check_db(db_path)
    before={name:(config_dir/name).read_bytes() for name in ('topics.yaml','industry.json')}
    destination.mkdir(parents=True, exist_ok=True)
    staging = destination / ('.staging-' + str(time.time_ns()))
    staging.mkdir()
    try:
        with closing(sqlite3.connect(f'{db_path.resolve().as_uri()}?mode=ro', uri=True)) as src, closing(sqlite3.connect(staging / 'paper_agent.db')) as dst:
            src.backup(dst)
        copies = {'config/topics.yaml': config_dir / 'topics.yaml', 'config/industry.json': config_dir / 'industry.json'}
        with closing(sqlite3.connect(staging / 'paper_agent.db')) as conn:
            has_profile=conn.execute("SELECT 1 FROM sqlite_master WHERE name='profile_versions'").fetchone()
            row=conn.execute('SELECT profile_json FROM profile_versions WHERE active=1 ORDER BY version DESC LIMIT 1').fetchone() if has_profile else None
        if row:
            parsed=json.loads(row[0]);(staging/'data').mkdir()
            (staging/'data/profile.json').write_text(json.dumps(parsed))
        else:
            copies['data/profile.json']=root/'data/profile.json'
        with closing(sqlite3.connect(staging / 'paper_agent.db')) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'artifacts' in tables:
                for kind, name in conn.execute('SELECT artifact_type, path FROM artifacts'):
                    if 'summary' in kind:
                        if name in _REQUIRED or name == 'manifest.json':
                            raise ValueError('Summary path collides with backup state')
                        copies[name] = _safe(root, name)
        for name, source in copies.items():
            if not source.is_file():
                raise ValueError(f'Missing backup input: {name}')
            target = _safe(staging, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        if before!={name:(config_dir/name).read_bytes() for name in before}:raise ValueError('configuration changed during backup')
        # Derive the revision from the exact frozen database and files staged for
        # this backup. Never reread the live database after the SQLite backup.
        from .sync_exchange import build_input_snapshot
        if require_canonical:
            with closing(sqlite3.connect(staging / 'paper_agent.db')) as conn:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {'papers', 'paper_sources', 'artifacts', 'workflow_cycles', 'curator_runs', 'curator_evaluations', 'recommendations'}
            if not required.issubset(tables):
                raise ValueError('canonical Project Paper schema required for sync backup')
        try:
            snapshot_revision = build_input_snapshot(staging / 'paper_agent.db', staging, max_bytes=None)['revision']
        except sqlite3.OperationalError as error:
            # Standalone backup callers may use a minimal SQLite schema. Keep the
            # revision derived from the frozen archive while production schemas use
            # the canonical serving snapshot revision above.
            if 'no such table: papers' not in str(error):
                raise
            digest = hashlib.sha256()
            for path in sorted(p for p in staging.rglob('*') if p.is_file()):
                digest.update(path.relative_to(staging).as_posix().encode())
                digest.update(path.read_bytes())
            snapshot_revision = digest.hexdigest()
        files = {p.relative_to(staging).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in staging.rglob('*') if p.is_file()}
        manifest = {'schema': 1, 'files': files, 'snapshot_revision': snapshot_revision, 'app_revision': os.environ.get('PAPER_AGENT_IMAGE_REVISION','unversioned-local')}
        (staging / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True), encoding='utf-8')
        if not validate_backup(staging):
            raise ValueError('Backup validation failed')
        final = destination / ('backup-' + str(time.time_ns()))
        staging.rename(final)
        return final
    except Exception:
        shutil.rmtree(staging)
        raise

def publish_archive(backup: Path, archive: Path) -> Path:
    """Write a complete backup archive, then atomically expose its final path."""
    backup, archive = Path(backup), Path(archive)
    partial = archive.with_name(archive.name + '.partial')
    try:
        with tarfile.open(partial, 'w:gz', compresslevel=9) as output:
            for source in sorted(backup.rglob('*')):
                if source.is_file():
                    output.add(source, arcname=source.relative_to(backup).as_posix(), recursive=False)
        partial.replace(archive)
        return archive
    except Exception:
        partial.unlink(missing_ok=True)
        raise

def validate_backup(path: Path) -> bool:
    path = Path(path)
    try:
        manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
        if not isinstance(manifest, dict) or manifest.get('schema') != 1 or not isinstance(manifest.get('files'), dict):
            return False
        if 'snapshot_revision' in manifest and (not isinstance(manifest['snapshot_revision'], str) or len(manifest['snapshot_revision']) != 64):
            return False
        files = manifest['files']
        if not _REQUIRED.issubset(files):
            return False
        for name, digest in files.items():
            target = _safe(path, name)
            if not isinstance(digest, str) or not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                return False
        json.loads((path / 'config/industry.json').read_text(encoding='utf-8'))
        json.loads((path / 'data/profile.json').read_text(encoding='utf-8'))
        _check_db(path / 'paper_agent.db')
        with closing(sqlite3.connect(f'{(path / "paper_agent.db").resolve().as_uri()}?mode=ro', uri=True)) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='artifacts'").fetchone():
                for kind, name in conn.execute('SELECT artifact_type,path FROM artifacts'):
                    if 'summary' in kind and name not in files:
                        return False
        return True
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return False

def restore_backup(backup: Path, target: Path) -> Path:
    backup, target = Path(backup), Path(target)
    if not validate_backup(backup):
        raise ValueError('Invalid backup')
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError('Restore target must be empty')
    target.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((backup / 'manifest.json').read_text())
    for name in manifest['files']:
        dest = _safe(target, name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_safe(backup, name), dest)
    shutil.copy2(backup / 'manifest.json', target / 'manifest.json')
    if not validate_backup(target):
        raise ValueError('Restored backup failed verification')
    return target


MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_EXPANDED_BACKUP_BYTES = 256 * 1024 * 1024

def expanded_backup_size(backup: Path, *, limit: int | None = None) -> int:
    if limit is None:
        limit = MAX_EXPANDED_BACKUP_BYTES
    total = 0
    for path in Path(backup).rglob('*'):
        if path.is_file():
            total += path.stat().st_size
            if total > limit:
                raise ValueError('Expanded recovery archive exceeds size limit')
    return total


def download_backup(base_url: str, destination: Path, *, allow_http_loopback: bool = False) -> Path:
    """Pull a recovery archive to staging; publish only after full validation."""
    from .sync_exchange import signature
    parsed = urllib.parse.urlparse(base_url)
    loopback_test = allow_http_loopback and parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost'}
    if (parsed.scheme != 'https' and not loopback_test) or not parsed.hostname or parsed.username or parsed.path not in {'', '/'} or parsed.query or parsed.fragment:
        raise ValueError('Backup URL must be an HTTPS origin')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    timestamp = str(time.time())
    path = '/sync/backup'
    request = urllib.request.Request(base_url.rstrip('/') + path, headers={
        'X-Paper-Sync-Timestamp': timestamp,
        'X-Paper-Sync-Signature': signature('GET', path, timestamp, b''),
    })
    with tempfile.TemporaryDirectory(prefix='.download-', dir=destination) as directory:
        staging = Path(directory)
        archive = staging / 'state.tar.gz'
        total = 0
        with urllib.request.build_opener(NoRedirect).open(request, timeout=120) as response, archive.open('wb') as output:
            while chunk := response.read(65536):
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise ValueError('Recovery archive exceeds size limit')
                output.write(chunk)
        extracted = staging / 'state'
        extracted.mkdir()
        total = 0
        with tarfile.open(archive, 'r:gz') as incoming:
            for count, member in enumerate(incoming, start=1):
                if count > 10000 or not member.isfile():
                    raise ValueError('Unsupported archive member')
                total += member.size
                if total > MAX_EXPANDED_BACKUP_BYTES:
                    raise ValueError('Expanded recovery archive exceeds size limit')
                target = _safe(extracted, member.name)
                if target.exists():
                    raise ValueError('Duplicate recovery archive member')
                target.parent.mkdir(parents=True, exist_ok=True)
                with incoming.extractfile(member) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output, length=65536)
        if not validate_backup(extracted):
            raise ValueError('Downloaded recovery archive failed validation')
        final = destination / ('backup-' + str(time.time_ns()))
        extracted.rename(final)
        return final


def download_routine_snapshot(base_url: str, *, allow_http_loopback: bool = False) -> dict:
    """Fetch and validate the bounded canonical JSON routine snapshot."""
    from .sync_exchange import encoded, signature, ROUTINE_SCHEMA, ROUTINE_MAX_EXPANDED_BYTES, _digest
    parsed = urllib.parse.urlparse(base_url)
    loopback_test = allow_http_loopback and parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost'}
    if (parsed.scheme != 'https' and not loopback_test) or not parsed.hostname or parsed.username or parsed.path not in {'', '/'} or parsed.query or parsed.fragment:
        raise ValueError('Snapshot URL must be an HTTPS origin')
    path = '/sync/routine'; timestamp = str(time.time())
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs): return None
    request = urllib.request.Request(base_url.rstrip('/') + path, headers={
        'Accept': 'application/json+gzip', 'X-Paper-Sync-Timestamp': timestamp,
        'X-Paper-Sync-Signature': signature('GET', path, timestamp, b''),
    })
    with urllib.request.build_opener(NoRedirect).open(request, timeout=120) as response:
        compressed = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(compressed) > MAX_ARCHIVE_BYTES:
        raise ValueError('Routine snapshot exceeds compressed size limit')
    try:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS); expanded = bytearray()
        for offset in range(0, len(compressed), 65536):
            expanded.extend(decoder.decompress(compressed[offset:offset + 65536], ROUTINE_MAX_EXPANDED_BYTES + 1 - len(expanded)))
            if len(expanded) > ROUTINE_MAX_EXPANDED_BYTES: raise ValueError('Routine snapshot exceeds expanded size limit')
        expanded.extend(decoder.flush(ROUTINE_MAX_EXPANDED_BYTES + 1 - len(expanded)))
        if len(expanded) > ROUTINE_MAX_EXPANDED_BYTES or not decoder.eof or decoder.unused_data:
            raise ValueError('Invalid compressed routine snapshot')
        body = bytes(expanded)
    except (zlib.error, EOFError) as error:
        raise ValueError('Invalid compressed routine snapshot') from error
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError('duplicate routine JSON key')
            result[key] = value
        return result
    try: payload = json.loads(body.decode('utf-8'), object_pairs_hook=unique)
    except (UnicodeDecodeError, ValueError) as error: raise ValueError('Invalid routine snapshot') from error
    if not isinstance(payload, dict) or type(payload.get('schema')) is not int or payload.get('schema') != ROUTINE_SCHEMA or payload.get('kind') != 'routine_snapshot':
        raise ValueError('Unsupported routine snapshot')
    revision = payload.get('revision'); unsigned = dict(payload); unsigned.pop('revision', None)
    if not isinstance(revision, str) or _digest(encoded(unsigned)) != revision:
        raise ValueError('Routine snapshot revision mismatch')
    return payload

"""Bounded, authenticated AWS-to-Mini snapshot packages; no network or activation.

The caller fences source writers for the entire export. SQLite's online backup
handles committed WAL pages, but cannot make files and multiple databases atomic.
Only the main database is approved: all current source traversal state lives there.
"""
from contextlib import closing
from datetime import datetime, timezone, timedelta
from functools import lru_cache
import gzip
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import tarfile
import tempfile
import time

from .import_state import digest, file_hash, schema, sync_directory, trusted_schemas
from .industry_config import load_zenml_config
from .topics import load_topic_config, render_topics_yaml

FORMAT = 1
POLICY = 'aws-mini-full-v1'
DATABASE = 'data/paper_agent.db'
CONFIG = frozenset({'data/profile.json', 'config/topics.yaml', 'config/industry.json'})
REQUIRED = CONFIG | {DATABASE}
MAX_BYTES = 1024**3
MAX_FILE_BYTES = 512 * 1024**2
MAX_FILES = 20000
MAX_METADATA = 8 * 1024**2
MAX_CONFIG = 4 * 1024**2
MAX_ARCHIVE = MAX_BYTES + 32 * 1024**2
CHUNK = 1024**2
EXCLUDED = ['secrets', 'environment', 'logs', 'locks', 'runtime', 'models',
            'pdf-bytes', 'host-state', 'nonprod-databases', 'sqlite-sidecars', 'unreferenced-files']
# Deliberately independent of future additions to sql/schema.sql.
TABLES = frozenset('''papers paper_sources workflow_cycles scouting_guidance scout_runs
scout_diagnostic_reports scout_candidates curator_runs curator_evaluations recommendations
artifacts raw_feedback feedback_parse_attempts structured_feedback profile_versions
feedback_profile_applications feedback_profile_apply_attempts feedback openalex_search_state
openalex_page_dispositions summary_field_feedback minilm_eval_runs minilm_eval_queue
minilm_eval_decisions semantic_scholar_search_state semantic_scholar_page_dispositions
summary_field_quality_signals arxiv_search_state arxiv_page_dispositions core_search_state
core_page_dispositions minilm_shadow_runs minilm_shadow_path_results minilm_shadow_outputs
minilm_shadow_decisions saved_papers excluded_papers sync_input_row_map sync_producers
sync_profile_proposals sync_profile_receipts sync_result_batches sync_snapshot_revisions
sync_summary_versions topic_agent_allowance topic_agent_requests'''.split())
PROFILE_KEYS = {'interests', 'positive_signals', 'negative_signals', 'notes', 'feedback_history'}
INDUSTRY_KEYS = {'enabled', 'include_signals', 'exclude_signals', 'max_items_per_run', 'schedule'}
SENSITIVE = re.compile(r'password|passwd|secret|credential|privatekey|apikey|accesstoken|refreshtoken|authorization|cookie', re.I)


class SnapshotError(RuntimeError):
    """Only fixed, non-sensitive diagnostic messages cross the public boundary."""


def require(condition, message):
    if not condition:
        raise SnapshotError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def decode(payload):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'Duplicate JSON key')
            result[key] = value
        return result
    def nonfinite(_value):
        raise SnapshotError('Invalid JSON number')
    return json.loads(payload, object_pairs_hook=unique, parse_constant=nonfinite)


def private_root(path, *, create=False):
    path = Path(os.path.abspath(path))
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'Aliased root refused')
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(path.is_dir(), 'Required directory missing')
    return path


def relative_path(name):
    require(isinstance(name, str) and 0 < len(name.encode()) <= 240, 'Unsafe relative path')
    path = PurePosixPath(name)
    require(not path.is_absolute() and path.as_posix() == name
            and all(re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', p) and p not in {'.', '..'}
                    for p in path.parts), 'Unsafe relative path')
    require(not any(SENSITIVE.search(re.sub(r'[^a-z0-9]', '', p.lower()))
                    or p.lower() in {'auth', 'logs', 'models', 'ollama', 'minilm', 'test', 'nonprod'}
                    for p in path.parts), 'Sensitive path refused')
    return path


def safe_file(root, name):
    path = root
    for part in relative_path(name).parts:
        path /= part
        require(not path.is_symlink(), 'Aliased file refused')
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'Isolated regular file required')
    return path


def separate(a, b):
    require(a != b and not a.is_relative_to(b) and not b.is_relative_to(a), 'Overlapping roots refused')


def credential_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            require(not SENSITIVE.search(re.sub(r'[^a-z0-9]', '', key.lower())), 'Credential field refused')
            credential_keys(child)
    elif isinstance(value, list):
        for child in value:
            credential_keys(child)


def artifact_name(name, *, pdf=False):
    path = relative_path(name)
    require(len(path.parts) >= 3 and path.parts[:2] in {
        ('data', 'papers'), ('data', 'extractions'), ('data', 'zenml-pilot')}, 'Artifact outside allowlist')
    require(path.suffix.lower() in ({'.pdf'} if pdf else {'.json', '.txt', '.md'}), 'Artifact extension refused')
    return name


@lru_cache(maxsize=1)
def schema_hashes():
    return {digest(value) for value in trusted_schemas()}


def database_info(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        actual = schema(db)
        tables = {row[1] for row in actual if row[0] == 'table'}
        require(tables == TABLES and digest(actual) in schema_hashes(), 'Unapproved database schema')
        require(db.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'SQLite integrity failed')
        require(not db.execute('PRAGMA foreign_key_check').fetchall(), 'SQLite foreign keys failed')
        counts = {}
        for table in sorted(tables):
            columns = [r[1] for r in db.execute('PRAGMA table_info("' + table + '")')]
            require(not any(SENSITIVE.search(re.sub(r'[^a-z0-9]', '', c.lower())) for c in columns),
                    'Credential column refused')
            counts[table] = db.execute('SELECT count(*) FROM "' + table + '"').fetchone()[0]
            for col in columns:
                if col.endswith('_json'):
                    for (value,) in db.execute('SELECT "' + col + '" FROM "' + table + '"'):
                        if value:
                            require(isinstance(value, str) and len(value.encode()) <= MAX_CONFIG, 'Database JSON field too large')
                            credential_keys(decode(value))
        references, excluded = set(), []
        for row_id, kind, name in db.execute('SELECT id,artifact_type,path FROM artifacts ORDER BY id'):
            if kind == 'pdf':
                artifact_name(name, pdf=True)
                excluded.append({'id': row_id, 'path': name})
            else:
                require(kind in {'triage_summary', 'review'}, 'Unapproved artifact type')
                references.add(artifact_name(name))
        require(len(references) + len(REQUIRED) <= MAX_FILES, 'Too many artifacts')
        return {'schema_sha256': digest(actual), 'user_version': db.execute('PRAGMA user_version').fetchone()[0],
                'counts': counts}, references, excluded


def config_check(root):
    for name in CONFIG:
        require(safe_file(root, name).stat().st_size <= MAX_CONFIG, 'Configuration too large')
    profile = decode((root / 'data/profile.json').read_bytes())
    require(isinstance(profile, dict) and set(profile) <= PROFILE_KEYS
            and all(isinstance(profile.get(k), list) for k in PROFILE_KEYS - {'notes'}), 'Invalid profile')
    industry = decode((root / 'config/industry.json').read_bytes())
    require(isinstance(industry, dict) and set(industry) <= INDUSTRY_KEYS, 'Invalid industry config')
    credential_keys(profile)
    credential_keys(industry)
    load_zenml_config(root / 'config/industry.json')
    # Canonicalization strips ignored YAML preambles/comments rather than exporting them.
    topics = render_topics_yaml(load_topic_config(root / 'config/topics.yaml')).encode()
    return topics


def write_file(path, payload):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open('xb') as handle:
        os.chmod(path, 0o600)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def sync_tree(root):
    for path in root.rglob('*'):
        if path.is_file():
            with path.open('rb') as handle:
                os.fsync(handle.fileno())
    for directory in sorted([root, *(p for p in root.rglob('*') if p.is_dir())],
                            key=lambda p: len(p.parts), reverse=True):
        sync_directory(directory)


def validate_manifest(value, stream, now=None, max_age=timedelta(days=7)):
    require(isinstance(value, dict) and set(value) == {'format', 'policy', 'stream', 'revision',
            'source_revision', 'created_at', 'files', 'database', 'excluded', 'excluded_pdf_references'},
            'Invalid manifest fields')
    require(value['format'] == FORMAT and value['policy'] == POLICY and value['stream'] == stream,
            'Snapshot identity mismatch')
    require(isinstance(stream, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', stream), 'Invalid stream')
    require(type(value['revision']) is int and 0 < value['revision'] < 10**20, 'Invalid revision')
    require(isinstance(value['source_revision'], str) and re.fullmatch(r'[0-9a-f]{40}', value['source_revision']),
            'Invalid source revision')
    timestamp = datetime.fromisoformat(value['created_at'])
    require(timestamp.tzinfo is not None and timestamp.utcoffset() == timedelta(0), 'UTC timestamp required')
    now = now or datetime.now(timezone.utc)
    require(now - timestamp <= max_age and timestamp <= now + timedelta(minutes=5), 'Snapshot expired or future dated')
    require(value['excluded'] == EXCLUDED, 'Snapshot exclusion policy mismatch')
    files = value['files']
    require(isinstance(files, dict) and REQUIRED <= files.keys() and len(files) <= MAX_FILES, 'Incomplete manifest')
    require(len({name.casefold() for name in files}) == len(files), 'Case-aliased files refused')
    total = 0
    for name, entry in files.items():
        relative_path(name)
        if name not in REQUIRED:
            artifact_name(name)
        require(isinstance(entry, dict) and set(entry) == {'size', 'sha256'}
                and type(entry['size']) is int and 0 <= entry['size'] <= MAX_FILE_BYTES
                and isinstance(entry['sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', entry['sha256']),
                'Invalid manifest file record')
        total += entry['size']
    require(total <= MAX_BYTES, 'Snapshot exceeds size limit')
    return timestamp


def validate_payload(root, manifest):
    files = manifest['files']
    actual = set()
    for path in root.rglob('*'):
        require(not path.is_symlink(), 'Aliased payload refused')
        if not path.is_dir():
            actual.add(path.relative_to(root).as_posix())
    require(actual == files.keys(), 'Unmanifested or missing payload')
    for name, expected in files.items():
        path = safe_file(root, name)
        require(path.stat().st_size == expected['size'] and file_hash(path) == expected['sha256'],
                'Snapshot file checksum mismatch')
        if name not in REQUIRED and path.suffix.lower() == '.json':
            require(path.stat().st_size <= MAX_CONFIG, 'JSON artifact too large')
            credential_keys(decode(path.read_bytes()))
    topics = config_check(root)
    require((root / 'config/topics.yaml').read_bytes() == topics, 'Noncanonical topic configuration')
    info, references, excluded = database_info(root / DATABASE)
    require(info == manifest['database'], 'Database manifest mismatch')
    require(references | REQUIRED == files.keys(), 'Missing or unreferenced artifact')
    require(excluded == manifest['excluded_pdf_references'], 'PDF exclusions mismatch')


def valid_key(key):
    require(isinstance(key, bytes) and len(key) >= 32, 'At least 32 secret bytes required')


def envelope(manifest_bytes, archive, key):
    valid_key(key)
    body = {'format': FORMAT, 'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
            'archive_sha256': file_hash(archive), 'archive_bytes': archive.stat().st_size}
    return {**body, 'hmac_sha256': hmac.new(key, b'paper-full-snapshot-v1\0' + canonical(body), hashlib.sha256).hexdigest()}


def authenticate(manifest_bytes, auth_bytes, archive, key):
    valid_key(key)
    auth = decode(auth_bytes)
    require(isinstance(auth, dict) and set(auth) == {'format', 'manifest_sha256', 'archive_sha256',
            'archive_bytes', 'hmac_sha256'}, 'Invalid authentication envelope')
    require(isinstance(auth['hmac_sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', auth['hmac_sha256']),
            'Invalid authentication tag')
    expected = envelope(manifest_bytes, archive, key)
    require(hmac.compare_digest(expected['hmac_sha256'], auth['hmac_sha256']) and auth == expected,
            'Snapshot authentication failed')


def export_snapshot(source, destination, *, stream, revision, source_revision, key, source_quiesced=False):
    """Publish snapshot-N/{manifest.json,auth.json,snapshot.tar.gz,payload/} atomically.

    The destination must be separate from the source. No secrets enter the package;
    arbitrary business prose is preserved, not heuristically scanned or redacted.
    """
    try:
        require(source_quiesced, 'Source writer fence acknowledgement required')
        valid_key(key)
        source, destination = private_root(source), Path(os.path.abspath(destination))
        separate(source, destination)
        require(not any((source / name).exists() for name in
                        ('data/NONPROD_SNAPSHOT.json', 'data/MINI_NONPROD_SYNTHETIC')),
                'Nonproduction source refused')
        destination = private_root(destination, create=True)
        require(type(revision) is int and 0 < revision < 10**20, 'Invalid revision')
        published = destination / f'snapshot-{revision:020d}'
        require(not published.exists(), 'Snapshot revision already exported')
        with tempfile.TemporaryDirectory(prefix='.export-', dir=destination) as folder:
            stage = Path(folder)
            payload = stage / 'payload'
            payload.mkdir(mode=0o700)
            (payload / 'data').mkdir(mode=0o700)
            for name in CONFIG:
                require(safe_file(source, name).stat().st_size <= MAX_CONFIG, 'Configuration too large')
            baseline = {name: file_hash(safe_file(source, name)) for name in CONFIG}
            source_db = safe_file(source, DATABASE)
            require(source_db.stat().st_size <= MAX_FILE_BYTES, 'Database too large')
            with closing(sqlite3.connect(source_db.as_uri() + '?mode=ro', uri=True, timeout=5)) as src:
                before = src.execute('PRAGMA data_version').fetchone()
                page_size = src.execute('PRAGMA page_size').fetchone()[0]
                with closing(sqlite3.connect(payload / DATABASE)) as dst:
                    deadline = time.monotonic() + 120
                    def progress(_status, _remaining, pages):
                        require(time.monotonic() < deadline, 'SQLite backup timeout')
                        require(pages * page_size <= MAX_FILE_BYTES,
                                'Database too large')
                    src.backup(dst, pages=256, progress=progress)
                    require(dst.execute('PRAGMA journal_mode=DELETE').fetchone() == ('delete',), 'Standalone database required')
                    # Do not carry deleted records/free pages from the source into an export.
                    dst.execute('VACUUM')
                info, references, excluded = database_info(payload / DATABASE)
                files = {DATABASE: {'size': (payload / DATABASE).stat().st_size,
                                    'sha256': file_hash(payload / DATABASE)}}
                total = files[DATABASE]['size']
                originals = {}
                topics = config_check(source)
                for name in sorted(CONFIG | references):
                    srcfile = safe_file(source, name)
                    size = srcfile.stat().st_size
                    total += size
                    require(size <= MAX_FILE_BYTES and total <= MAX_BYTES, 'Snapshot exceeds size limit')
                    originals[name] = file_hash(srcfile)
                    target = payload / name
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    if name == 'config/topics.yaml':
                        write_file(target, topics)
                    else:
                        with srcfile.open('rb') as reader, target.open('xb') as writer:
                            copied = 0
                            while block := reader.read(CHUNK):
                                copied += len(block)
                                require(copied <= size, 'Source file changed during export')
                                writer.write(block)
                        require(copied == size and file_hash(target) == originals[name], 'Source file changed during export')
                    os.chmod(target, 0o600)
                    files[name] = {'size': target.stat().st_size, 'sha256': file_hash(target)}
                require(before == src.execute('PRAGMA data_version').fetchone(), 'Database changed during export')
                require(all(file_hash(safe_file(source, n)) == h for n, h in {**originals, **baseline}.items()),
                        'Source file changed during export')
            manifest = {'format': FORMAT, 'policy': POLICY, 'stream': stream, 'revision': revision,
                        'source_revision': source_revision, 'created_at': datetime.now(timezone.utc).isoformat(),
                        'files': files, 'database': info, 'excluded': EXCLUDED, 'excluded_pdf_references': excluded}
            validate_manifest(manifest, stream)
            validate_payload(payload, manifest)
            manifest_bytes = canonical(manifest)
            require(len(manifest_bytes) <= MAX_METADATA, 'Manifest too large')
            write_file(stage / 'manifest.json', manifest_bytes)
            archive = stage / 'snapshot.tar.gz'
            with archive.open('xb') as raw:
                with gzip.GzipFile(fileobj=raw, mode='wb', compresslevel=9, mtime=0, filename='') as zipped:
                    with tarfile.open(fileobj=zipped, mode='w|', format=tarfile.USTAR_FORMAT) as tar:
                        for name in sorted(files):
                            member = tarfile.TarInfo(name)
                            member.size = files[name]['size']
                            member.mode = 0o600
                            with (payload / name).open('rb') as data:
                                tar.addfile(member, data)
            require(archive.stat().st_size <= MAX_ARCHIVE, 'Compressed snapshot too large')
            write_file(stage / 'auth.json', canonical(envelope(manifest_bytes, archive, key)))
            os.chmod(payload / DATABASE, 0o600)
            os.chmod(archive, 0o600)
            sync_tree(stage)
            # Single exporter per source revision is an operator prerequisite.
            require(not published.exists(), 'Snapshot revision already exported')
            os.rename(stage, published)
            sync_directory(destination)
            return published
    except SnapshotError:
        raise
    except Exception:
        raise SnapshotError('Snapshot export failed') from None


def unpack(archive, target, manifest):
    """Read only our regular-file USTAR subset; never extract links or extension headers."""
    expected = manifest['files']
    seen = set()
    with gzip.open(archive, 'rb') as stream:
        while True:
            header = stream.read(512)
            require(len(header) == 512, 'Truncated archive')
            if header == b'\0' * 512:
                # Check the complete gzip trailer and bound tar padding / concatenated data.
                padding = stream.read(10241)
                require(512 <= len(padding) <= 10240 and not padding.strip(b'\0'), 'Invalid archive trailer')
                break
            member = tarfile.TarInfo.frombuf(header, 'utf-8', 'strict')
            require(member.type == tarfile.REGTYPE and not member.linkname and header[257:263] == b'ustar\0',
                    'Unsupported archive entry')
            name = member.name
            require(name in expected and name not in seen and member.size == expected[name]['size'],
                    'Unexpected archive entry')
            relative_path(name)
            seen.add(name)
            destination = target / name
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with destination.open('xb') as output:
                os.chmod(destination, 0o600)
                remaining = member.size
                while remaining:
                    block = stream.read(min(CHUNK, remaining))
                    require(block, 'Truncated archive file')
                    output.write(block)
                    remaining -= len(block)
            padding = stream.read(-member.size % 512)
            require(len(padding) == -member.size % 512 and not padding.strip(b'\0'), 'Invalid file padding')
    require(seen == expected.keys(), 'Incomplete archive')


def read_key(path):
    path = Path(os.path.abspath(path))
    private_root(path.parent)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'Isolated regular secret file required')
    require(info.st_mode & 0o077 == 0 and info.st_size <= 4096, 'Secret file must be private and bounded')
    key = path.read_bytes()
    valid_key(key)
    return key

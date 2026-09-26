"""Validate a quiesced imported copy. This slice permits NO schema migration."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

from .topics import load_topic_config

REQUIRED = {'data/paper_agent.db', 'data/profile.json', 'config/topics.yaml'}
RECEIPT = 'data/import-accepted.json'
LOCK = 'data/.initialize.lock'
IGNORED = {RECEIPT, LOCK}
TRUSTED_SCHEMA = Path(__file__).resolve().parents[1]/'sql/schema.sql'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def file_hash(path):
    sha = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            sha.update(block)
    return sha.hexdigest()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def schema(connection):
    # Exact schema contract: unknown/older layouts need a separately reviewed migration.
    return connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()


OPENALEX_SCHEMA_MARKER = '-- Versioned OpenAlex traversal state; enabled only by the cursor retrieval path.'
OPENALEX_SCHEMA_SQL = TRUSTED_SCHEMA.read_text().split(OPENALEX_SCHEMA_MARKER, 1)[1]
SUMMARY_FEEDBACK_SCHEMA_MARKER = '-- Versioned summary-field quality feedback; additive after the OpenAlex traversal state.'
SUMMARY_FEEDBACK_SCHEMA_SQL = TRUSTED_SCHEMA.read_text().split(SUMMARY_FEEDBACK_SCHEMA_MARKER, 1)[1]


def _schema_variants(sql):
    from .db import migrate_structured_feedback_score_to_real
    require(sql.count('score REAL,') == 1, 'Trusted score schema changed; review compatibility')
    variants = []
    for migrated in (False, True):
        connection = sqlite3.connect(':memory:')
        try:
            connection.executescript(sql.replace('score REAL,', 'score INTEGER,') if migrated else sql)
            if migrated:
                migrate_structured_feedback_score_to_real(connection)
            variants.append(schema(connection))
        finally:
            connection.close()
    return variants


def trusted_schemas():
    """Fresh schema plus exact historical score-migration renderings."""
    return _schema_variants(TRUSTED_SCHEMA.read_text())


def pre_openalex_schemas():
    """Accepted production schema before approved OpenAlex cursor tables."""
    sql = TRUSTED_SCHEMA.read_text().split(OPENALEX_SCHEMA_MARKER, 1)[0]
    return _schema_variants(sql)


def pre_summary_feedback_schemas():
    """Accepted production schema before summary-field quality feedback."""
    sql = TRUSTED_SCHEMA.read_text().split(SUMMARY_FEEDBACK_SCHEMA_MARKER, 1)[0]
    return _schema_variants(sql)


def apply_approved_post_import_schema_deltas(root):
    """Apply reviewed additive schema growth to already accepted imports only."""
    root = Path(root).absolute()
    database = root/'data/paper_agent.db'
    connection = sqlite3.connect(database)
    try:
        require(connection.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'Imported SQLite integrity failed')
        require(not connection.execute('PRAGMA foreign_key_check').fetchall(), 'Imported foreign keys invalid')
        actual_schema = schema(connection)
        if actual_schema in trusted_schemas():
            return False
        if actual_schema in pre_summary_feedback_schemas():
            delta_sql = SUMMARY_FEEDBACK_SCHEMA_SQL
        else:
            require(actual_schema in pre_openalex_schemas(), 'Imported schema incompatible; no schema deltas approved')
            delta_sql = OPENALEX_SCHEMA_SQL
        with connection:
            connection.executescript(delta_sql)
        require(schema(connection) in trusted_schemas(), 'Approved schema delta did not produce trusted schema')
        return True
    finally:
        connection.close()


def logical_rows(connection):
    result = {}
    for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        quoted = '"' + name.replace('"', '""') + '"'
        rows = connection.execute('SELECT * FROM ' + quoted).fetchall()
        # Hash each row and sort: includes every column, ID, relation and state flag.
        normalized = [[{'blob_hex':v.hex()} if isinstance(v, bytes) else v for v in row] for row in rows]
        result[name] = {'count':len(rows), 'sha256':digest(sorted(digest(row) for row in normalized))}
    return result


def inspect(root, *, staging=None):
    root = Path(root).absolute()
    for directory in ('data', 'config'):
        require((root/directory).is_dir() and not (root/directory).is_symlink(), 'Required real state directory missing')
    files = {}
    for directory in ('data', 'config'):
        for path in sorted((root/directory).rglob('*')):
            require(not path.is_symlink(), 'Imported state must not contain symlinks')
            relative = path.relative_to(root).as_posix()
            if path.is_dir() or relative in IGNORED or relative == staging:
                continue
            require(path.is_file(), 'Unsupported imported file type')
            require(not relative.endswith(('-wal', '-shm', '-journal')), 'Import requires a quiesced standalone SQLite snapshot')
            files[relative] = file_hash(path)
    require(REQUIRED <= files.keys(), 'Required imported DB/profile/topics missing; refusing to seed')
    profile = json.loads((root/'data/profile.json').read_text())
    require(isinstance(profile, dict) and all(isinstance(profile.get(k),list) for k in
        ('interests','positive_signals','negative_signals','feedback_history')), 'Invalid imported profile')
    topics = root/'config/topics.yaml'
    require('topics:' in [line.strip() for line in topics.read_text().splitlines()], 'Invalid imported topics')
    load_topic_config(topics)
    marker = root/'data/package-version.json'
    if marker.exists():
        require(json.loads(marker.read_text()).get('schema_version') == 1, 'Unsupported package schema')
    expected_schemas = trusted_schemas()
    # immutable requires the quiescent no-sidecar contract above; never creates files.
    connection = sqlite3.connect((root/'data/paper_agent.db').as_uri()+'?mode=ro&immutable=1', uri=True)
    try:
        require(connection.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'Imported SQLite integrity failed')
        require(not connection.execute('PRAGMA foreign_key_check').fetchall(), 'Imported foreign keys invalid')
        actual_schema = schema(connection)
        require(actual_schema in expected_schemas, 'Imported schema incompatible; no schema deltas approved')
        for (stored,) in connection.execute('SELECT path FROM artifacts'):
            path = Path(stored)
            require(not path.is_absolute() and '..' not in path.parts and path.parts and path.parts[0] == 'data',
                    'Artifact path requires a separately reviewed mapping')
            require(path.as_posix() in files, 'Referenced artifact missing from imported state')
        tables = logical_rows(connection)
    finally:
        connection.close()
    return {'files':files, 'schema_sha256':digest(actual_schema), 'tables':tables}


def manifest(root):
    """Read-only manifest builder for an already isolated, quiesced snapshot."""
    return {'format':1, 'compatibility':'exact-schema-no-deltas', **inspect(root)}


def load_manifest(path):
    value = json.loads(Path(path).read_text())
    require(set(value) == {'format','compatibility','files','schema_sha256','tables'} and
            value['format'] == 1 and value['compatibility'] == 'exact-schema-no-deltas', 'Unsupported import manifest')
    require(isinstance(value['files'], dict) and REQUIRED <= value['files'].keys(), 'Incomplete import manifest')
    return value


def validate_lock_descriptor(path, descriptor):
    opened, current = os.fstat(descriptor), path.lstat()
    require(stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1
            and stat.S_ISREG(current.st_mode) and current.st_nlink == 1
            and (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino),
            'Invalid initialization lock: expected isolated regular file')


@contextmanager
def initialization_lock(root):
    path = Path(root)/LOCK
    try:
        before = path.lstat()
    except FileNotFoundError:
        before = None
    if before is not None:
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
                'Invalid initialization lock: expected isolated regular file')
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    # Exclusive creation prevents silently accepting a replacement on the absent path.
    descriptor = os.open(path, flags | (os.O_CREAT | os.O_EXCL if before is None else 0), 0o600)
    try:
        validate_lock_descriptor(path, descriptor)
        if before is not None:
            opened = os.fstat(descriptor)
            require((opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino),
                    'Initialization lock changed while opening')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Imported state initialization already active') from None
        validate_lock_descriptor(path, descriptor)
        with os.fdopen(descriptor, 'r+') as handle:
            descriptor = None  # The stream now owns and closes the descriptor.
            yield handle
    finally:
        if descriptor is not None:
            os.close(descriptor)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def staging_name(approved):
    return 'data/.import-receipt-' + digest(approved) + '.pending'


def initialize(root, manifest_path):
    root = Path(root)
    approved = load_manifest(manifest_path)
    receipt_value = {'format':1, 'manifest_sha256':digest(approved)}
    encoded = json.dumps(receipt_value, sort_keys=True).encode()
    staging = staging_name(approved)
    temporary, destination = root/staging, root/RECEIPT
    require(staging not in approved['files'], 'Reserved receipt staging path collides with snapshot')
    if destination.exists():
        apply_approved_post_import_schema_deltas(root)

    def validate(ignore_staging=False):
        current = inspect(root, staging=staging if ignore_staging else None)
        if destination.exists():
            require(not destination.is_symlink() and json.loads(destination.read_text()) == receipt_value,
                    'Import receipt mismatch')
            # Legitimate application writes and approved post-import schema deltas need not match
            # the original snapshot once the manifest-bound receipt exists.
        else:
            require(current['schema_sha256'] == approved['schema_sha256'], 'Import schema differs from manifest')
            require(current == {k:approved[k] for k in ('files','schema_sha256','tables')},
                    'Imported state differs from manifest')

    exists = os.path.lexists(temporary)
    if exists:
        # Recovery may not create a lock to legitimize a preexisting colliding file.
        require((root/LOCK).is_file() and not (root/LOCK).is_symlink(), 'Unowned receipt staging collision')
    else:
        validate()  # Reject invalid inputs before creating metadata.
    with initialization_lock(root) as handle:
        if os.path.lexists(temporary):
            handle.seek(0)
            require(handle.read().encode() == encoded, 'Unowned receipt staging collision')
            info = temporary.lstat()
            require(stat.S_ISREG(info.st_mode) and info.st_size <= len(encoded), 'Invalid receipt staging file')
            content = temporary.read_bytes()
            require(encoded.startswith(content), 'Invalid receipt staging content')
            require(info.st_nlink == 1 or (info.st_nlink == 2 and destination.exists()
                    and not destination.is_symlink() and os.path.samefile(temporary, destination)),
                    'Unexpected receipt staging hardlink')
            validate(ignore_staging=True)
            # A linked receipt must be complete and independently validated above.
            if destination.exists():
                with destination.open('rb') as receipt:
                    os.fsync(receipt.fileno())
                sync_directory(root/'data')
            temporary.unlink()
            sync_directory(root/'data')
        else:
            validate()
        if not destination.exists():
            # Durable manifest-bound ownership precedes creating the exact reserved path.
            validate_lock_descriptor(root/LOCK, handle.fileno())
            handle.seek(0); handle.truncate(); handle.write(encoded.decode())
            handle.flush(); os.fsync(handle.fileno())
            sync_directory(root/'data')
            # Retain interrupted partial staging for validation/recovery on the next attempt.
            with temporary.open('xb') as stream:
                stream.write(encoded)
                stream.flush(); os.fsync(stream.fileno())
            os.link(temporary, destination)  # Atomic no-clobber publication.
            sync_directory(root/'data')
            temporary.unlink()
        # Also finish durability when an earlier process died after publication/unlink.
        with destination.open('rb') as receipt:
            os.fsync(receipt.fileno())
        sync_directory(root/'data')

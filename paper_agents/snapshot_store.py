"""Mini-initiated package ingestion and atomic pipeline-input selection.

No transport is configured here. A private transfer adapter supplies bounded file
streams; LocalPackage is the explicit offline adapter used by tests and the CLI.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import BinaryIO, Protocol
import uuid

from . import full_snapshot as snapshot
from .migration_lifecycle import lease


class PackageSource(Protocol):
    """Open one immutable publication's file; never accept arbitrary remote paths.

    Production implementations MUST enforce the private peer, authenticated
    restricted identity/directory, byte/time limits and no redirect/public fallback.
    The caller owns connection timeout/cancellation. Ingestion owns content auth.
    """
    def open(self, name: str) -> BinaryIO: ...


class LocalPackage:
    """Offline fixture / already privately transferred package; performs no network IO."""
    def __init__(self, root):
        self.root = snapshot.private_root(root)

    def open(self, name):
        snapshot.require(name in {'manifest.json', 'auth.json', 'snapshot.tar.gz'}, 'Unapproved transfer file')
        return snapshot.safe_file(self.root, name).open('rb')


def receive(source, name, destination, maximum):
    count = 0
    with source.open(name) as incoming, destination.open('xb') as outgoing:
        os.chmod(destination, 0o600)
        while True:
            block = incoming.read(min(snapshot.CHUNK, maximum - count + 1))
            if not block:
                break
            snapshot.require(isinstance(block, bytes), 'Invalid transfer stream')
            count += len(block)
            snapshot.require(count <= maximum, 'Transfer size limit exceeded')
            outgoing.write(block)
        outgoing.flush()
        os.fsync(outgoing.fileno())


def store_root(root):
    root = snapshot.private_root(root, create=True)
    snapshot.require(root.stat().st_mode & 0o077 == 0, 'Store directory must be private')
    # Never initialize over existing application state.
    snapshot.require(not (root / 'data').exists() and not (root / 'config').exists(), 'Dedicated input store required')
    return root


def recover_transfers(root):
    """Under the exclusive lease, remove only our identifiable abandoned transfers.

    Unknown files/directories are preserved. A process death before the ownership
    marker is durable may leave an empty orphan for explicit operator cleanup.
    """
    for stage in root.glob('.incoming-*'):
        snapshot.require(not stage.is_symlink(), 'Aliased transfer staging refused')
        if not stage.is_dir():
            continue
        marker = stage / 'transfer-owner.json'
        if not marker.exists():
            continue
        snapshot.safe_file(stage, 'transfer-owner.json')
        snapshot.require(marker.stat().st_size <= 1024, 'Invalid staging ownership marker')
        expected = {'format': 1, 'policy': snapshot.POLICY, 'directory': stage.name}
        snapshot.require(marker.read_bytes() == snapshot.canonical(expected), 'Invalid staging ownership marker')
        shutil.rmtree(stage)
    snapshot.sync_directory(root)


def version_record(value):
    snapshot.require(isinstance(value, dict) and set(value) == {'directory', 'revision', 'created_at', 'manifest_sha256'},
                     'Invalid input version record')
    snapshot.require(type(value['revision']) is int and 0 < value['revision'] < 10**20,
                     'Invalid input revision')
    snapshot.require(isinstance(value['directory'], str) and re.fullmatch(
        r'snapshot-' + f'{value["revision"]:020d}' + r'-[0-9a-f]{32}', value['directory']), 'Invalid version directory')
    snapshot.require(isinstance(value['manifest_sha256'], str)
                     and re.fullmatch(r'[0-9a-f]{64}', value['manifest_sha256']), 'Invalid manifest identity')


def state_read(root, stream):
    path = root / 'state.json'
    if not path.exists() and not path.is_symlink():
        # Missing state with old versions must not silently reset the replay barrier.
        versions = root / 'versions'
        snapshot.require(not versions.is_symlink(), 'Aliased versions directory refused')
        if versions.exists():
            snapshot.private_root(versions)
            snapshot.require(not any(versions.iterdir()), 'Missing state requires operator recovery')
        return {'format': 1, 'stream': stream, 'current': None, 'previous': None,
                'high_water_revision': 0, 'high_water_created_at': None}
    snapshot.safe_file(root, 'state.json')
    snapshot.require(path.stat().st_size <= 16384, 'Input state too large')
    state = snapshot.decode(path.read_bytes())
    snapshot.require(isinstance(state, dict) and set(state) == {'format', 'stream', 'current', 'previous',
                     'high_water_revision', 'high_water_created_at'} and state['format'] == 1
                     and state['stream'] == stream and type(state['high_water_revision']) is int
                     and state['high_water_revision'] > 0, 'Invalid input state')
    snapshot.require(state['current'] is not None, 'Missing current input')
    for record in (state['current'], state['previous']):
        if record is not None:
            version_record(record)
            snapshot.require(record['revision'] <= state['high_water_revision'], 'Invalid replay barrier')
    return state


def state_commit(root, value):
    # Current, previous and the replay barrier move in ONE atomic durable record.
    descriptor, name = tempfile.mkstemp(prefix='.state-', dir=root)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(snapshot.canonical(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, root / 'state.json')
        snapshot.sync_directory(root)
    finally:
        temporary.unlink(missing_ok=True)


def version_path(root, record):
    version_record(record)
    versions = snapshot.private_root(root / 'versions')
    return snapshot.private_root(versions / record['directory'])


def verify_version(root, record, stream, key):
    version = version_path(root, record)
    manifest_file = snapshot.safe_file(version, 'manifest.json')
    snapshot.require(manifest_file.stat().st_size <= snapshot.MAX_METADATA, 'Manifest too large')
    manifest_bytes = manifest_file.read_bytes()
    manifest = snapshot.decode(manifest_bytes)
    # An explicit rollback may select an old snapshot; normal pulls enforce freshness.
    snapshot.validate_manifest(manifest, stream, max_age=timedelta.max)
    snapshot.require(snapshot.digest(manifest) == record['manifest_sha256']
                     and manifest['revision'] == record['revision']
                     and manifest['created_at'] == record['created_at'], 'Input version identity mismatch')
    auth = snapshot.safe_file(version, 'auth.json')
    archive = snapshot.safe_file(version, 'snapshot.tar.gz')
    snapshot.require(auth.stat().st_size <= 4096 and archive.stat().st_size <= snapshot.MAX_ARCHIVE,
                     'Stored package exceeds bounds')
    snapshot.authenticate(manifest_bytes, auth.read_bytes(), archive, key)
    payload = snapshot.private_root(version / 'payload')
    snapshot.validate_payload(payload, manifest)
    return payload


def pull_snapshot(source: PackageSource, root, *, stream, key):
    """Receive, authenticate, validate and select an input; returns revision only.

    Consumers must use selected_input for their entire read/copy operation. They
    must treat these inputs as immutable and write pipeline outputs elsewhere.
    """
    try:
        snapshot.valid_key(key)
        root = store_root(root)
        if isinstance(source, LocalPackage):
            snapshot.separate(source.root, root)
        with lease(root, exclusive=True, name='.snapshot.lock'):
            state = state_read(root, stream)
            recover_transfers(root)
            versions = snapshot.private_root(root / 'versions', create=True)
            with tempfile.TemporaryDirectory(prefix='.incoming-', dir=root) as folder:
                stage = Path(folder)
                snapshot.write_file(stage / 'transfer-owner.json', snapshot.canonical(
                    {'format': 1, 'policy': snapshot.POLICY, 'directory': stage.name}))
                snapshot.sync_directory(stage)
                receive(source, 'manifest.json', stage / 'manifest.json', snapshot.MAX_METADATA)
                receive(source, 'auth.json', stage / 'auth.json', 4096)
                receive(source, 'snapshot.tar.gz', stage / 'snapshot.tar.gz', snapshot.MAX_ARCHIVE)
                manifest_bytes = (stage / 'manifest.json').read_bytes()
                snapshot.authenticate(manifest_bytes, (stage / 'auth.json').read_bytes(), stage / 'snapshot.tar.gz', key)
                manifest = snapshot.decode(manifest_bytes)
                timestamp = snapshot.validate_manifest(manifest, stream)
                snapshot.require(manifest['revision'] > state['high_water_revision'], 'Stale or duplicate snapshot revision')
                if state['high_water_created_at'] is not None:
                    snapshot.require(timestamp >= datetime.fromisoformat(state['high_water_created_at']),
                                     'Snapshot capture time regressed')
                payload = stage / 'payload'
                payload.mkdir(mode=0o700)
                snapshot.unpack(stage / 'snapshot.tar.gz', payload, manifest)
                snapshot.validate_payload(payload, manifest)
                snapshot.sync_tree(stage)
                record = {'directory': f'snapshot-{manifest["revision"]:020d}-{uuid.uuid4().hex}',
                          'revision': manifest['revision'], 'created_at': manifest['created_at'],
                          'manifest_sha256': snapshot.digest(manifest)}
                destination = versions / record['directory']
                os.rename(stage, destination)
                snapshot.sync_directory(versions)
                new_state = {**state, 'current': record, 'previous': state['current'],
                             'high_water_revision': manifest['revision'],
                             'high_water_created_at': manifest['created_at']}
                try:
                    state_commit(root, new_state)
                except Exception:
                    # A post-replace fsync error leaves a complete selected version.
                    # Never delete it or erase the replay barrier on an ambiguous commit.
                    if not (root / 'state.json').exists() or (root / 'state.json').read_bytes() != snapshot.canonical(new_state):
                        shutil.rmtree(destination)
                        snapshot.sync_directory(versions)
                    raise
                return manifest['revision']
    except snapshot.SnapshotError:
        raise
    except Exception:
        raise snapshot.SnapshotError('Snapshot pull failed; inspect input state before retry') from None


def rollback(root, *, stream, key):
    try:
        root = store_root(root)
        with lease(root, exclusive=True, name='.snapshot.lock'):
            state = state_read(root, stream)
            snapshot.require(state['previous'] is not None, 'No previous input available')
            verify_version(root, state['previous'], stream, key)
            state_commit(root, {**state, 'current': state['previous'], 'previous': state['current']})
            return state['previous']['revision']
    except snapshot.SnapshotError:
        raise
    except Exception:
        raise snapshot.SnapshotError('Snapshot rollback failed; inspect input state before retry') from None


@contextmanager
def selected_input(root, *, stream, key):
    """Pin and validate the immutable input until the caller finishes reading it."""
    root = snapshot.private_root(root)
    with lease(root, name='.snapshot.lock'):
        state = state_read(root, stream)
        snapshot.require(state['current'] is not None, 'No pipeline input selected')
        yield verify_version(root, state['current'], stream, key)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    export = sub.add_parser('export')
    export.add_argument('source', type=Path)
    export.add_argument('destination', type=Path)
    export.add_argument('--revision', required=True, type=int)
    export.add_argument('--source-revision', required=True)
    export.add_argument('--source-quiesced', action='store_true', required=True)
    pull = sub.add_parser('pull-local', help='Offline adapter only; no network transfer')
    pull.add_argument('package', type=Path)
    pull.add_argument('store', type=Path)
    undo = sub.add_parser('rollback')
    undo.add_argument('store', type=Path)
    for command in (export, pull, undo):
        command.add_argument('--stream', required=True)
        command.add_argument('--key-file', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        key = snapshot.read_key(args.key_file)
        if args.command == 'export':
            snapshot.separate(args.key_file.absolute(), args.destination.absolute())
            snapshot.separate(args.key_file.absolute(), args.source.absolute())
            snapshot.export_snapshot(args.source, args.destination, stream=args.stream, revision=args.revision,
                                     source_revision=args.source_revision, key=key, source_quiesced=args.source_quiesced)
            revision = args.revision
        elif args.command == 'pull-local':
            snapshot.separate(args.key_file.absolute(), args.store.absolute())
            revision = pull_snapshot(LocalPackage(args.package), args.store, stream=args.stream, key=key)
        else:
            snapshot.separate(args.key_file.absolute(), args.store.absolute())
            revision = rollback(args.store, stream=args.stream, key=key)
        print(json.dumps({'status': 'ok', 'operation': args.command, 'revision': revision}))
        return 0
    except Exception:
        # Never print exception values, paths, secrets or private business records.
        print(json.dumps({'status': 'failed', 'operation': args.command}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/python3
"""Fixed Mini host-package transaction. Provision as root-owned code, never via a runner script.

GitHub OIDC artifact attestations authorize the exact hosted workflow, tag,
source commit and artifact digest. No separate signing key is used.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile
import tempfile
import time

SCHEMA = 'project-paper.observability-host-package.v1'
TARGET = 'mini-production'
FILES = {
    'deploy/observability/collect-publish.sh': '/usr/local/sbin/project-paper-observability-collect-publish',
    'deploy/observability/project-paper-observability-deploy': '/usr/local/sbin/project-paper-observability-deploy',
    'deploy/observability/run-container.sh': '/usr/local/sbin/project-paper-observability-container',
}
CONTROL = '/var/lib/project-paper-observability/mini-production-control'
CRON = '/etc/cron.d/project-paper-observability-mini-production'
VERIFIER = '/usr/local/libexec/project-paper-observability/gh'
REPOSITORY = 'devitolo/paper-agent'
WORKFLOW = 'devitolo/paper-agent/.github/workflows/observability-release.yml'
SOURCE_REF = 'refs/tags/observability-mini'
INCOMING = '/var/lib/project-paper-observability/mini-production-incoming'
LIMIT = 1024 * 1024


def exact(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError('invalid schema fields')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def decode(data):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('duplicate field')
            value[key] = item
        return value
    return json.loads(data, object_pairs_hook=unique)


def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('invalid timestamp')
    value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise ValueError('UTC required')
    return value.timestamp()


def read_fd(fd, limit):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError('invalid file size/type')
    with os.fdopen(os.dup(fd), 'rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('file too large')
    return data


def trusted(path, *, directory=False):
    """All ancestors and leaf must be root-owned, non-writable and not symlinks."""
    for item in (*reversed(path.parents), path):
        info = item.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
            raise ValueError('untrusted host path')
        expected_dir = item != path or directory
        if expected_dir and not stat.S_ISDIR(info.st_mode):
            raise ValueError('directory required')
        if not expected_dir and not stat.S_ISREG(info.st_mode):
            raise ValueError('regular file required')


def sync_directory(path):
    fd = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic(path, data, mode):
    fd, temporary = tempfile.mkstemp(prefix='.package-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locks(root):
    descriptors = []
    try:
        for name in ('project-paper-observability-deploy.lock', 'project-paper-observability.lock'):
            path = root / 'run/lock' / name
            info = path.parent.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                    or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
                raise ValueError('untrusted lock directory')
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            descriptors.append(fd)
            info = os.fstat(fd)
            if info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
                raise ValueError('untrusted lock')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def snapshot_inputs(root, bundle, source_sha):
    # Open every component with dir_fd + NOFOLLOW: a runner may replace inbox
    # files but cannot redirect root reads outside the fixed inbox.
    inbox = root / INCOMING.lstrip('/')
    trusted(inbox.parent, directory=True)
    info = inbox.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
            or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
        raise ValueError('untrusted incoming directory')
    parent = os.open(inbox, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        directory = os.open(bundle + '-' + source_sha, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            result = []
            for name, size in (('manifest.json', 16384), ('attestation.jsonl', 524288), ('artifact.tar', LIMIT)):
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                try:
                    result.append(read_fd(fd, size))
                finally:
                    os.close(fd)
            return result
        finally:
            os.close(directory)
    finally:
        os.close(parent)


def verify(root, bundle, source_sha, inputs, staging, now):
    raw, attestation, archive = inputs
    if sha(archive) != bundle:
        raise ValueError('artifact digest mismatch')
    verifier = root / VERIFIER.lstrip('/')
    trusted(verifier)
    for name, data in (('manifest.json', raw), ('attestation.jsonl', attestation), ('artifact.tar', archive)):
        atomic(staging / name, data, 0o600)
    # No inherited token, proxy, config, plugin or runner-writable verifier.
    environment = {'PATH': '/usr/bin:/bin', 'HOME': str(staging), 'GH_HOST': 'github.com',
                   'GH_CONFIG_DIR': str(staging), 'XDG_CACHE_HOME': str(staging)}
    for name in ('artifact.tar', 'manifest.json'):
        subprocess.run([str(verifier), 'attestation', 'verify', str(staging / name),
                        '--bundle', str(staging / 'attestation.jsonl'),
                        '--repo', REPOSITORY,
                        '--source-digest', source_sha, '--signer-digest', source_sha,
                        '--source-ref', SOURCE_REF, '--deny-self-hosted-runners',
                        '--cert-oidc-issuer', 'https://token.actions.githubusercontent.com',
                        '--cert-identity', 'https://github.com/' + WORKFLOW + '@' + SOURCE_REF,
                        '--predicate-type', 'https://slsa.dev/provenance/v1'],
                       env=environment, check=True, timeout=45,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    manifest = decode(raw)
    exact(manifest, ('schema', 'target', 'source_sha', 'artifact_sha256', 'files', 'installer_version', 'created_at'))
    if (manifest['schema'], manifest['target'], manifest['artifact_sha256']) != (SCHEMA, TARGET, bundle):
        raise ValueError('manifest identity mismatch')
    if manifest['source_sha'] != source_sha:
        raise ValueError('invalid source revision')
    if type(manifest['installer_version']) is not int or manifest['installer_version'] != 1:
        raise ValueError('unsupported installer version')
    if not now - 7 * 86400 <= timestamp(manifest['created_at']) <= now + 300:
        raise ValueError('expired/future manifest')
    if not isinstance(manifest['files'], list) or len(manifest['files']) != len(FILES):
        raise ValueError('invalid file count')
    expected = {}
    for entry in manifest['files']:
        exact(entry, ('path', 'sha256', 'mode', 'owner'))
        name = entry['path']
        if not isinstance(name, str) or name not in FILES or name in expected:
            raise ValueError('unexpected/duplicate path')
        if entry['mode'] != '0755' or entry['owner'] != 'root:root':
            raise ValueError('unexpected ownership/mode')
        if not isinstance(entry['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', entry['sha256']):
            raise ValueError('invalid file digest')
        expected[name] = entry['sha256']
    content = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as package:
        for member in package:
            if (not member.isfile() or member.name not in expected or member.name in content
                    or member.size > 131072 or member.mode != 0o755 or member.uid != 0 or member.gid != 0):
                raise ValueError('invalid archive member')
            data = package.extractfile(member).read(131073)
            if (len(data) > 131072 or sha(data) != expected[member.name]
                    or b'host-package-pending-guard-v1' not in data):
                raise ValueError('file digest mismatch')
            content[member.name] = data
    if set(content) != set(expected):
        raise ValueError('incomplete archive')
    return manifest, content


def health(root, started):
    env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
           'PROJECT_PAPER_HOST_PACKAGE_QUALIFY': '1',
           'PROJECT_PAPER_RESOURCE_ENV_FILE': '/etc/project-paper-observability/mini-production/mini-production.env'}
    # Collector-only qualification. Never force publication during host updates.
    subprocess.run([str(root / 'usr/local/sbin/project-paper-observability-container')],
                   env=env, check=True, timeout=55, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    path = root / 'var/lib/project-paper-observability/mini-production-export/status.json'
    with path.open('rb') as stream:
        status = decode(stream.read(32769))
    if (status.get('schema'), status.get('environment'), status.get('scope')) != ('project-paper.resource-status.v1', TARGET, 'host'):
        raise ValueError('invalid collector qualification')
    observed = timestamp(status.get('observed_at'))
    if observed < started or abs(time.time() - observed) > 300:
        raise ValueError('collector qualification not fresh')


def install(root, bundle, source_sha, *, check_health=health, now=None):
    """Root is internal for isolated tests; CLI never accepts a filesystem root."""
    now = time.time() if now is None else now
    if not isinstance(bundle, str) or not re.fullmatch('[0-9a-f]{64}', bundle):
        raise ValueError('invalid artifact identity')
    if not isinstance(source_sha, str) or not re.fullmatch('[0-9a-f]{40}', source_sha):
        raise ValueError('invalid source commit')
    root = Path(root)
    control = root / CONTROL.lstrip('/')
    trusted(control, directory=True)
    with locks(root):
        pending = control / 'HOST-PACKAGE-PENDING.json'
        receipt = control / 'HOST-PACKAGE-RECEIPT.json'
        if pending.exists() or pending.is_symlink():
            raise ValueError('pending transaction requires trusted recovery')
        with tempfile.TemporaryDirectory(prefix='package-verify-', dir=control) as directory:
            stage = Path(directory)
            manifest, files = verify(root, bundle, source_sha, snapshot_inputs(root, bundle, source_sha), stage, now)
            if receipt.exists() or receipt.is_symlink():
                trusted(receipt)
                if stat.S_IMODE(receipt.stat().st_mode) != 0o600:
                    raise ValueError('unexpected receipt mode')
                if timestamp(decode(receipt.read_bytes())['created_at']) > timestamp(manifest['created_at']):
                    raise ValueError('older package requires explicit recovery')
            destinations = [root / p.lstrip('/') for p in FILES.values()] + [root / CRON.lstrip('/')]
            for index, destination in enumerate(destinations):
                trusted(destination)
                if stat.S_IMODE(destination.stat().st_mode) != (0o755 if index < 3 else 0o644):
                    raise ValueError('unexpected installed mode')
            cron = destinations[-1].read_bytes()
            # Preserve exact existing cadence/config/lock/log; this path upgrades
            # an installed collect-publish flow, never silently changes scheduling.
            if (cron.count(b'*/15 * * * * root ') != 1
                    or cron.count(b'/usr/local/sbin/project-paper-observability-collect-publish') != 1):
                raise ValueError('combined cron prerequisite missing')
            # A trusted bootstrap must install guards in all entrypoints BEFORE
            # this installer is enabled; never create a first mixed-version window.
            for destination in destinations[:-1]:
                if b'host-package-pending-guard-v1' not in destination.read_bytes():
                    raise ValueError('pending guard bootstrap missing')
            if receipt.exists() and all((root / FILES[name].lstrip('/')).read_bytes() == data for name, data in files.items()):
                previous = decode(receipt.read_bytes())
                if previous.get('artifact_sha256') == bundle and previous.get('result') == 'ready':
                    return {**previous, 'result': 'already_ready'}
            transaction = Path(tempfile.mkdtemp(prefix='host-package-', dir=control))
            sync_directory(control)
            before = []
            for index, destination in enumerate(destinations + [receipt]):
                if destination.exists():
                    data, mode = destination.read_bytes(), stat.S_IMODE(destination.stat().st_mode)
                    atomic(transaction / f'{index}.before', data, 0o600)
                    before.append({'path': str(destination), 'mode': mode, 'sha256': sha(data)})
                else:
                    before.append({'path': str(destination), 'mode': None, 'sha256': None})
            audit = {'schema': SCHEMA, 'target': TARGET, 'artifact_sha256': bundle,
                     'source_sha': manifest['source_sha'], 'created_at': manifest['created_at'],
                     'transaction': transaction.name, 'before': before}
            atomic(transaction / 'transaction.json', json.dumps(audit, sort_keys=True).encode(), 0o600)
            atomic(pending, json.dumps(audit, sort_keys=True).encode(), 0o600)
            try:
                for name, data in files.items():
                    atomic(root / FILES[name].lstrip('/'), data, 0o755)
                check_health(root, time.time())
                audit['result'] = 'ready'
                audit['installed_at'] = datetime.now(timezone.utc).isoformat()
                atomic(receipt, json.dumps(audit, sort_keys=True).encode(), 0o600)
                atomic(transaction / 'result.json', json.dumps(audit, sort_keys=True).encode(), 0o600)
                pending.unlink()
                sync_directory(control)
                prune_successes(control, transaction.name)
                return audit
            except Exception:
                # If restoration fails the durable pending marker remains and all
                # later updates refuse to proceed; retain backups for recovery.
                for index, item in enumerate(before):
                    destination = Path(item['path'])
                    if item['mode'] is None:
                        destination.unlink(missing_ok=True)
                    else:
                        data = (transaction / f'{index}.before').read_bytes()
                        if sha(data) != item['sha256']:
                            raise ValueError('rollback backup invalid')
                        atomic(destination, data, item['mode'])
                atomic(transaction / 'result.json', b'{"result":"rolled_back"}', 0o600)
                pending.unlink()
                sync_directory(control)
                raise


def prune_successes(control, current):
    """Keep 20 successful transactions; never prune failures or pending evidence."""
    candidates = []
    try:
        for path in control.glob('host-package-*'):
            trusted(path, directory=True)
            result = path / 'result.json'
            if not result.exists():
                continue
            trusted(result)
            if decode(result.read_bytes()).get('result') == 'ready':
                candidates.append(path)
        older = sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)[20:]
        for path in older:
            if path.name == current:
                continue
            children = list(path.iterdir())
            if any(not re.fullmatch(r'[0-4]\.before|transaction\.json|result\.json', child.name) for child in children):
                continue
            for child in children:
                trusted(child)
            for child in children:
                child.unlink()
            path.rmdir()
        sync_directory(control)
    except (OSError, ValueError):
        # Pruning cannot invalidate a committed healthy transaction. Failed or
        # unfamiliar evidence stays available for administrative inspection.
        return


def recover(root, *, check_health=health):
    """Restore a durable pending transaction after interruption; fixed paths only."""
    root = Path(root)
    control = root / CONTROL.lstrip('/')
    trusted(control, directory=True)
    with locks(root):
        pending = control / 'HOST-PACKAGE-PENDING.json'
        trusted(pending)
        audit = decode(pending.read_bytes())
        exact(audit, ('schema', 'target', 'artifact_sha256', 'source_sha', 'created_at', 'transaction', 'before'))
        if audit['schema'] != SCHEMA or audit['target'] != TARGET:
            raise ValueError('invalid recovery identity')
        name = audit['transaction']
        if not isinstance(name, str) or not re.fullmatch(r'host-package-[A-Za-z0-9_-]+', name):
            raise ValueError('invalid transaction path')
        transaction = control / name
        trusted(transaction, directory=True)
        expected = [str(root / path.lstrip('/')) for path in FILES.values()]
        expected += [str(root / CRON.lstrip('/')), str(control / 'HOST-PACKAGE-RECEIPT.json')]
        if not isinstance(audit['before'], list) or len(audit['before']) != len(expected):
            raise ValueError('invalid recovery file count')
        # Validate ALL backups before replacing any current file.
        restore = []
        for index, (entry, destination) in enumerate(zip(audit['before'], expected)):
            exact(entry, ('path', 'mode', 'sha256'))
            if entry['path'] != destination:
                raise ValueError('invalid recovery destination')
            trusted(Path(destination).parent, directory=True)
            if Path(destination).exists() or Path(destination).is_symlink():
                trusted(Path(destination))
            if entry['mode'] is None and index == 4 and entry['sha256'] is None:
                restore.append((Path(destination), None, None))
                continue
            if entry['mode'] != (0o755 if index < 3 else 0o644 if index == 3 else 0o600):
                raise ValueError('invalid recovery mode')
            backup = transaction / f'{index}.before'
            trusted(backup)
            data = backup.read_bytes()
            if sha(data) != entry['sha256']:
                raise ValueError('invalid recovery backup')
            restore.append((Path(destination), data, entry['mode']))
        for destination, data, mode in restore:
            if data is None:
                destination.unlink(missing_ok=True)
            else:
                atomic(destination, data, mode)
        check_health(root, time.time())
        atomic(transaction / 'result.json', b'{"result":"recovered"}', 0o600)
        pending.unlink()
        sync_directory(control)
        return {'result': 'recovered', 'target': TARGET, 'transaction': name}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', required=True, choices=[TARGET])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--bundle')
    action.add_argument('--recover', action='store_true')
    parser.add_argument('--source-sha')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('fixed root-owned installer required')
    try:
        result = recover(Path('/')) if args.recover else install(Path('/'), args.bundle, args.source_sha)
        print(json.dumps({key: result[key] for key in ('result', 'target', 'artifact_sha256', 'source_sha', 'transaction') if key in result}))
    except Exception:
        print('{"result":"failed","detail":"inspect root-owned transaction evidence"}')
        raise SystemExit(1)


if __name__ == '__main__':
    main()

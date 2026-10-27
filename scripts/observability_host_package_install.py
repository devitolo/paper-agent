#!/usr/bin/python3 -I
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
ENV_FILE = '/etc/project-paper-observability/mini-production/mini-production.env'
IMAGE_RECEIPT = CONTROL + '/INSTALL-RECEIPT'
IMAGE_PATTERN = r'ghcr\.io/devitolo/paper-agent/observability:observability-mini@sha256:[0-9a-f]{64}'


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
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
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
    exact(manifest, ('schema', 'target', 'source_sha', 'artifact_sha256', 'files', 'installer_version', 'created_at', 'image'))
    if (manifest['schema'], manifest['target'], manifest['artifact_sha256']) != (SCHEMA, TARGET, bundle):
        raise ValueError('manifest identity mismatch')
    if manifest['source_sha'] != source_sha:
        raise ValueError('invalid source revision')
    if type(manifest['installer_version']) is not int or manifest['installer_version'] != 2:
        raise ValueError('unsupported installer version')
    if not isinstance(manifest['image'], str) or not re.fullmatch(IMAGE_PATTERN, manifest['image']):
        raise ValueError('target-specific immutable image required')
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


def docker_command(arguments, *, timeout):
    # The registry image is public and exact-digest pinned. Do not inherit a
    # runner Docker context, credentials, proxy or daemon selector.
    environment = {'PATH':'/usr/bin:/bin', 'HOME':'/nonexistent', 'DOCKER_CONFIG':'/nonexistent'}
    result = subprocess.run(['/usr/bin/docker','--host','unix:///var/run/docker.sock', *arguments],
                            env=environment, check=True, timeout=timeout,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if len(result.stdout)>16384:
        raise ValueError('unexpected Docker metadata size')
    return result.stdout


def resources(root, docker_root):
    for path in (root / CONTROL.lstrip('/'), Path(docker_root)):
        filesystem=os.statvfs(path)
        if filesystem.f_bavail*filesystem.f_frsize < 2*1024**3:
            raise ValueError('insufficient disk headroom')
    meminfo=(root / 'proc/meminfo').read_text()
    available=[line.split()[1:] for line in meminfo.splitlines() if line.startswith('MemAvailable:')]
    if len(available)!=1 or len(available[0])!=2 or available[0][1]!='kB' or not available[0][0].isdigit():
        raise ValueError('memory headroom unavailable')
    if int(available[0][0])*1024 < 256*1024**2:
        raise ValueError('insufficient memory headroom')


def prepare_image(root, image, source_sha, previous_image):
    docker_root=docker_command(['info','--format','{{.DockerRootDir}}'],timeout=10).decode().strip()
    if not docker_root.startswith('/') or '\n' in docker_root:
        raise ValueError('invalid Docker storage path')
    resources(root, docker_root)
    old_metadata=decode(docker_command(['image','inspect','--format',
        '{"architecture":{{json .Architecture}},"os":{{json .Os}}}',previous_image],timeout=10))
    exact(old_metadata, ('architecture','os'))
    if (old_metadata['architecture'],old_metadata['os']) != ('amd64','linux'):
        raise ValueError('local rollback image unavailable or incompatible')
    # The updater never removes/prunes images. The locally present old immutable
    # reference stays in the backed-up environment throughout qualification.
    docker_command(['pull','--quiet',image],timeout=120)
    raw=docker_command(['image','inspect','--format',
        '{"architecture":{{json .Architecture}},"os":{{json .Os}},"revision":{{json (index .Config.Labels "org.opencontainers.image.revision")}},"size":{{json .Size}}}',image],timeout=10)
    metadata=decode(raw)
    exact(metadata, ('architecture','os','revision','size'))
    if (metadata['architecture'],metadata['os'],metadata['revision']) != ('amd64','linux',source_sha):
        raise ValueError('image architecture/source mismatch')
    if type(metadata['size']) is not int or not 0 < metadata['size'] <= 512*1024**2:
        raise ValueError('image exceeds qualified size budget')
    resources(root, docker_root)


def image_config(data, image):
    try:
        text = data.decode('ascii')
    except UnicodeError:
        raise ValueError('invalid image config') from None
    lines = text.splitlines(keepends=True)
    values = {}
    for line in lines:
        key, separator, value = line.rstrip('\n').partition('=')
        if not separator or key in values:
            raise ValueError('invalid image config fields')
        values[key] = value
    expected = {
        'PROJECT_PAPER_RESOURCE_ROOT':'/var/lib/project-paper-observability/mini-production',
        'PROJECT_PAPER_RESOURCE_EXPORT_DIR':'/var/lib/project-paper-observability/mini-production-export',
        'PROJECT_PAPER_RESOURCE_ENVIRONMENT':TARGET,
        'PROJECT_PAPER_RESOURCE_HOST':TARGET,
        'PROJECT_PAPER_RESOURCE_ENV_FILE':ENV_FILE,
    }
    if set(values) != set(expected) | {'PROJECT_PAPER_OBSERVABILITY_IMAGE'}:
        raise ValueError('unexpected config fields')
    if any(values[key] != value for key,value in expected.items()):
        raise ValueError('unexpected config target/path')
    if not re.fullmatch(IMAGE_PATTERN, values['PROJECT_PAPER_OBSERVABILITY_IMAGE']):
        raise ValueError('invalid previous image')
    if not re.fullmatch(IMAGE_PATTERN, image):
        raise ValueError('invalid new image')
    return ''.join('PROJECT_PAPER_OBSERVABILITY_IMAGE='+image+'\n' if line.startswith('PROJECT_PAPER_OBSERVABILITY_IMAGE=') else line for line in lines).encode('ascii')


def managed_paths(root):
    return [(root / path.lstrip('/'), 0o755, False) for path in FILES.values()] + [
        (root / ENV_FILE.lstrip('/'), 0o640, False),
        (root / CRON.lstrip('/'), 0o644, False),
        (root / IMAGE_RECEIPT.lstrip('/'), 0o644, True),
        (root / (CONTROL + '/HOST-PACKAGE-RECEIPT.json').lstrip('/'), 0o600, True),
    ]


def restore_plan(root, transaction, before):
    expected = managed_paths(root)
    if not isinstance(before, list) or len(before) != len(expected):
        raise ValueError('invalid recovery file count')
    restore = []
    # Check every backup before replacing ANY current file.
    for index, (entry, (destination, mode, optional)) in enumerate(zip(before, expected)):
        exact(entry, ('path', 'mode', 'sha256'))
        if entry['path'] != str(destination):
            raise ValueError('invalid recovery destination')
        trusted(destination.parent, directory=True)
        if destination.exists() or destination.is_symlink():
            trusted(destination)
        if optional and entry['mode'] is None and entry['sha256'] is None:
            restore.append((destination, None, None)); continue
        if entry['mode'] != mode:
            raise ValueError('invalid recovery mode')
        backup = transaction / f'{index}.before'
        trusted(backup)
        data = backup.read_bytes()
        if sha(data) != entry['sha256']:
            raise ValueError('invalid recovery backup')
        restore.append((destination, data, mode))
    return restore


def restore_files(restore):
    for destination, data, mode in restore:
        if data is None:
            destination.unlink(missing_ok=True)
            sync_directory(destination.parent)
        else:
            atomic(destination, data, mode)


def health(root, started):
    env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin',
           'PROJECT_PAPER_HOST_PACKAGE_QUALIFY': '1',
           'PROJECT_PAPER_RESOURCE_ENV_FILE': '/etc/project-paper-observability/mini-production/mini-production.env'}
    # Collector-only qualification. Never force publication during host updates.
    subprocess.run([str(root / 'usr/local/sbin/project-paper-observability-container')],
                   env=env, check=True, timeout=75, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    path = root / 'var/lib/project-paper-observability/mini-production-export/status.json'
    with path.open('rb') as stream:
        status = decode(stream.read(32769))
    if (status.get('schema'), status.get('environment'), status.get('scope')) != ('project-paper.resource-status.v1', TARGET, 'host'):
        raise ValueError('invalid collector qualification')
    observed = timestamp(status.get('observed_at'))
    if observed < started or abs(time.time() - observed) > 300:
        raise ValueError('collector qualification not fresh')


def install(root, bundle, source_sha, *, check_health=health, prepare=None, now=None):
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
        bootstrap_pending = control / 'HOST-BOOTSTRAP-PENDING.json'
        if bootstrap_pending.exists() or bootstrap_pending.is_symlink():
            raise ValueError('host bootstrap recovery pending')
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
                previous_receipt=decode(receipt.read_bytes())
                prior_time=timestamp(previous_receipt['created_at'])
                package_time=timestamp(manifest['created_at'])
                same_identity=all(previous_receipt.get(key)==value for key,value in (
                    ('artifact_sha256',bundle),('source_sha',source_sha),('image',manifest['image'])))
                if prior_time > package_time or (prior_time == package_time and not same_identity):
                    raise ValueError('older or ambiguous package requires explicit recovery')
            specifications = managed_paths(root)
            for destination, mode, optional in specifications:
                if optional and not destination.exists() and not destination.is_symlink():
                    continue
                trusted(destination)
                if stat.S_IMODE(destination.stat().st_mode) != mode:
                    raise ValueError('unexpected installed mode')
            env_path = root / ENV_FILE.lstrip('/')
            config_before = env_path.read_bytes()
            config_after = image_config(config_before, manifest['image'])
            cron_path = root / CRON.lstrip('/')
            cron = cron_path.read_bytes()
            if (cron.count(b'*/15 * * * * root ') != 1
                    or cron.count(b'/usr/local/sbin/project-paper-observability-collect-publish') != 1):
                raise ValueError('combined cron prerequisite missing')
            for path in FILES.values():
                if b'host-package-pending-guard-v1' not in (root / path.lstrip('/')).read_bytes():
                    raise ValueError('pending guard bootstrap missing')
            if receipt.exists() and config_before == config_after and all((root / FILES[name].lstrip('/')).read_bytes() == data for name, data in files.items()):
                previous = decode(receipt.read_bytes())
                if previous.get('artifact_sha256') == bundle and previous.get('image') == manifest['image'] and previous.get('result') == 'ready':
                    return {**previous, 'result': 'already_ready'}
            previous_image = next(line.split('=',1)[1] for line in config_before.decode('ascii').splitlines() if line.startswith('PROJECT_PAPER_OBSERVABILITY_IMAGE='))
            (prepare or prepare_image)(root, manifest['image'], source_sha, previous_image)
            transaction = Path(tempfile.mkdtemp(prefix='host-package-', dir=control))
            sync_directory(control)
            before = []
            for index, (destination, _, _) in enumerate(specifications):
                if destination.exists():
                    data, mode = destination.read_bytes(), stat.S_IMODE(destination.stat().st_mode)
                    atomic(transaction / f'{index}.before', data, 0o600)
                    before.append({'path': str(destination), 'mode': mode, 'sha256': sha(data)})
                else:
                    before.append({'path': str(destination), 'mode': None, 'sha256': None})
            audit = {'schema': SCHEMA, 'target': TARGET, 'artifact_sha256': bundle,
                     'source_sha': manifest['source_sha'], 'created_at': manifest['created_at'], 'image': manifest['image'],
                     'transaction': transaction.name, 'before': before}
            atomic(transaction / 'transaction.json', json.dumps(audit, sort_keys=True).encode(), 0o600)
            atomic(pending, json.dumps(audit, sort_keys=True).encode(), 0o600)
            try:
                for name, data in files.items():
                    atomic(root / FILES[name].lstrip('/'), data, 0o755)
                atomic(env_path, config_after, 0o640)
                check_health(root, time.time())
                if cron_path.read_bytes() != cron:
                    raise ValueError('cron changed during qualification')
                image_receipt = f"target={TARGET}\nimage={manifest['image']}\nconfig={ENV_FILE}\n".encode()
                atomic(root / IMAGE_RECEIPT.lstrip('/'), image_receipt, 0o644)
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
                restore_files(restore_plan(root, transaction, before))
                check_health(root, time.time())
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
            if any(not re.fullmatch(r'[0-6]\.before|transaction\.json|result\.json', child.name) for child in children):
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
        exact(audit, ('schema', 'target', 'artifact_sha256', 'source_sha', 'created_at', 'image', 'transaction', 'before'))
        if audit['schema'] != SCHEMA or audit['target'] != TARGET:
            raise ValueError('invalid recovery identity')
        name = audit['transaction']
        if not isinstance(name, str) or not re.fullmatch(r'host-package-[A-Za-z0-9_-]+', name):
            raise ValueError('invalid transaction path')
        transaction = control / name
        trusted(transaction, directory=True)
        restore_files(restore_plan(root, transaction, audit['before']))
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
        print(json.dumps({key: result[key] for key in ('result', 'target', 'artifact_sha256', 'source_sha', 'image', 'transaction') if key in result}))
    except Exception:
        print('{"result":"failed","detail":"inspect root-owned transaction evidence"}')
        raise SystemExit(1)


if __name__ == '__main__':
    main()

"""Archive engine executed inside the selected application image, with services stopped."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
import shutil


def validate(root):
    database = root / 'data/paper_agent.db'
    if not database.is_file():
        raise ValueError('Missing database')
    with closing(sqlite3.connect(f'file:{database}?mode=ro', uri=True)) as connection:
        if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('Database integrity check failed')
    profile = json.loads((root / 'data/profile.json').read_text())
    if not isinstance(profile, dict):
        raise ValueError('Invalid profile')
    if not (root / 'config/topics.yaml').is_file():
        raise ValueError('Missing topics')


def backup(root, archive, image):
    validate(root)
    if archive.exists():
        raise ValueError('Backup already exists; choose a new filename')
    descriptor, filename = tempfile.mkstemp(prefix='.paper-backup-', suffix='.partial', dir=archive.parent)
    os.close(descriptor)
    temporary = Path(filename)
    try:
        with temporary.open('wb') as stream:
            with tarfile.open(fileobj=stream, mode='w:gz') as output:
                for name in ('data', 'config'):
                    for path in (root / name).rglob('*'):
                        if path.is_symlink() or not (path.is_file() or path.is_dir()):
                            raise ValueError('Unsupported link or special file in user state')
                    output.add(root / name, arcname=name)
                import io
                payload = json.dumps({'format': 1, 'app_image': image}).encode()
                info = tarfile.TarInfo('manifest.json')
                info.size = len(payload)
                output.addfile(info, io.BytesIO(payload))
        # Verify the actual compressed archive before publishing it.
        with tempfile.TemporaryDirectory() as folder:
            unpack(temporary, Path(folder), image)
        os.chmod(temporary, 0o600)
        os.link(temporary, archive)  # Atomically refuse an existing destination.
        if os.geteuid() == 0:
            owner = archive.parent.stat()
            os.chown(archive, owner.st_uid, owner.st_gid)
    finally:
        temporary.unlink(missing_ok=True)


def unpack(archive, target, image):
    with tarfile.open(archive, 'r:gz') as source:
        members = source.getmembers()
        names = set()
        for member in members:
            path = Path(member.name)
            if (path.is_absolute() or '..' in path.parts or member.name in names
                    or not (member.isdir() or member.isfile())
                    or not path.parts or path.parts[0] not in ('data', 'config', 'manifest.json')):
                raise ValueError('Unsafe archive member')
            names.add(member.name)
        source.extractall(target, members=members, filter='data')
    manifest = json.loads((target / 'manifest.json').read_text())
    if manifest != {'format': 1, 'app_image': image}:
        raise ValueError('Restore requires the same application image as the backup')
    validate(target)


def restore(root, archive, image):
    # Restore never overwrites an existing installation.
    if any((root / name).exists() and any((root / name).iterdir()) for name in ('data', 'config')):
        raise ValueError('Restore requires empty data/config volumes; existing data is never overwritten')
    with tempfile.TemporaryDirectory() as folder:
        staging = Path(folder)
        unpack(archive, staging, image)
        for name in ('data', 'config'):
            shutil.copytree(staging / name, root / name, dirs_exist_ok=True)
        if os.geteuid() == 0:
            for name in ('data', 'config'):
                for path in [root / name, *(root / name).rglob('*')]:
                    os.chown(path, 10001, 10001)
        validate(root)


if __name__ == '__main__':
    action, filename, image = sys.argv[1:]
    if action not in ('backup', 'restore'):
        raise SystemExit('Expected backup or restore')
    globals()[action](Path('/app'), Path('/backup') / filename, image)
    print(action + ' verified')

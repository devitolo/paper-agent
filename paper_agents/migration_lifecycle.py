"""Shared running-work leases exclude imported application initialization."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat


def check_descriptor(path, descriptor):
    opened, current = os.fstat(descriptor), path.lstat()
    if not (stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1
            and stat.S_ISREG(current.st_mode) and current.st_nlink == 1
            and (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)):
        raise RuntimeError('Invalid isolated runtime lock')


@contextmanager
def lease(data, *, exclusive=False, name='.runtime.lock'):
    data = Path(data)
    if not data.is_dir() or data.is_symlink():
        raise RuntimeError('Runtime data directory missing or aliased')
    path = data/name
    try:
        before = path.lstat()
    except FileNotFoundError:
        before = None
    if before is not None and (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1):
        raise RuntimeError('Invalid isolated runtime lock')
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(path, flags | (os.O_CREAT | os.O_EXCL if before is None else 0), 0o600)
    try:
        check_descriptor(path,descriptor)
        opened = os.fstat(descriptor)
        if before is not None and (opened.st_dev,opened.st_ino) != (before.st_dev,before.st_ino):
            raise RuntimeError('Runtime lock changed during open')
        try:
            fcntl.flock(descriptor,(fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Runtime lifecycle busy') from None
        check_descriptor(path,descriptor)
        yield descriptor
    finally:
        os.close(descriptor)


def downgrade(descriptor):
    # flock conversion may release/reacquire on some systems. Any intervening
    # initializer/job still respects this same lock; the app is not serving yet.
    fcntl.flock(descriptor,fcntl.LOCK_SH)


def validate_container_contract():
    import re
    mode = os.environ.get('PAPER_MIGRATION_IDENTITY_MODE', 'registry')
    if mode == 'local-rehearsal':
        from .rehearsal_images import validate_runtime
        validate_runtime()
        return
    if mode != 'registry':
        raise ValueError('Unknown migration image identity mode')
    for name in ('PAPER_APP_IMAGE','PAPER_MIGRATION_OLLAMA_IMAGE'):
        if re.fullmatch(r'[^\s@]+@sha256:[0-9a-f]{64}',os.environ.get(name,'')) is None:
            raise ValueError('Migration requires immutable application and Ollama image digests')


def signal_group(process, sig):
    try:
        os.killpg(process.pid,sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS can report EPERM for an unreaped exited leader. Reap, then retry;
        # never suppress a permission failure while a live group remains.
        if process.poll() is None:
            raise
        try:os.killpg(process.pid,sig)
        except ProcessLookupError:pass

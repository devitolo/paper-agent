"""Migration-only Gemini launch contract. Never inspect native credentials."""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys

NODE = Path('/usr/local/bin/node')
CLI = Path('/opt/paper-gemini/cli')
INVENTORY = Path('/opt/paper-gemini/runtime.json')
SECRET = Path('/run/secrets/paper_gemini_api_key')
PARENT_CLEANUP_SECONDS = 3.0


def enabled():
    return (os.environ.get('PAPER_AGENT_STARTUP_MODE') == 'imported'
            and os.environ.get('PAPER_AGENT_GEMINI_ENABLED') == '1')


def read_key(path=None):
    """Private single-link regular file, bounded read, no path/content in errors."""
    path = SECRET if path is None else path
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_mode & 0o077
                or not 1 <= info.st_size <= 4096):
            raise ValueError
        value = os.read(descriptor, 4097)
        if len(value) > 4096:
            raise ValueError
        value = value.decode('ascii').removesuffix('\n')
        if not re.fullmatch(r'[!-~]{1,4096}', value):
            raise ValueError
        return value
    except (OSError, ValueError, UnicodeError):
        raise RuntimeError('Gemini credential unavailable: provision the private UID10001 key file with mode 0400 or 0600.') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def model_policy():
    # Deliberately unset in deployment examples. Operator decision, not a
    # qualification boolean; never turn this into an application --model flag.
    value = os.environ.get('PAPER_AGENT_GEMINI_DEFAULT_MODEL', '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', value):
        raise RuntimeError('Gemini default model policy unresolved: configure the approved CLI default model.')
    return value


def check_runtime():
    try:
        inventory = json.loads(INVENTORY.read_text())
        if inventory['node_version'] != 'v22.23.2' or inventory['cli_version'] != '0.52.0':
            raise ValueError
        for name, path in (('node_sha256', NODE), ('cli_sha256', CLI)):
            if not path.is_file() or inventory[name] != hashlib.sha256(path.read_bytes()).hexdigest():
                raise ValueError
        if not os.access(NODE, os.X_OK):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError('Gemini runtime unavailable: use the pinned migration-gemini image and its build inventory.') from None


def check_ready():
    check_runtime()
    model_policy()
    read_key()


def check_timeout(timeout):
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 7200:
        raise RuntimeError('Gemini requires a finite timeout between zero and 7200 seconds.')


def readiness_error():
    if enabled():
        try:
            check_ready()
        except RuntimeError as error:
            return str(error)
    return None


@contextmanager
def launch(command, timeout):
    """Guardian owns an independent lease even if the app dies in a web thread."""
    if not enabled():
        yield command, {}, 1.0
        return
    check_timeout(timeout)
    check_ready()
    if not command or command[0] != 'gemini':
        raise RuntimeError('Gemini migration requires the controlled CLI launcher.')
    from .migration_lifecycle import lease, check_descriptor
    locks = Path(os.environ['PAPER_AGENT_LIFECYCLE_DIR'])
    with lease(locks) as descriptor:
        reader, writer = os.pipe()
        try:
            descriptors = [reader, descriptor]
            singleton = os.environ.get('PAPER_AGENT_SCHEDULER_LEASE_FD')
            if singleton is not None:
                try:
                    singleton = int(singleton)
                    check_descriptor(locks/'.scheduler.lock', singleton)
                except (ValueError, OSError, RuntimeError):
                    raise RuntimeError('Gemini scheduler lifecycle lease unavailable.') from None
                descriptors.append(singleton)
            arguments = [sys.executable, '-m', 'paper_agents.gemini_guardian',
                         str(reader), str(descriptor), str(singleton if singleton is not None else -1),
                         str(timeout), *command[1:]]
            yield arguments, {'pass_fds': tuple(descriptors)}, PARENT_CLEANUP_SECONDS
        finally:
            os.close(reader)
            os.close(writer)

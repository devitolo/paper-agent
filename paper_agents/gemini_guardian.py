"""Bounded migration CLI guardian; parent death also triggers group cleanup."""
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time

from . import gemini_runtime as runtime
from .migration_lifecycle import check_descriptor, signal_group


def enable_subreaper():
    # A dedicated Linux guardian can reap orphaned CLI grandchildren itself;
    # otherwise a Python application at container PID1 would accumulate zombies.
    if not sys.platform.startswith('linux'):
        raise RuntimeError('Migration process supervision requires Linux subreaper support.')
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise RuntimeError('Gemini child reaping unavailable.')
    return True


def reap_descendants():
    # All unreaped direct children belong to this dedicated subreaper. Keeping
    # them unreaped while signalling prevents PID reuse between listing/kill.
    # Killing an adopted session leader adopts its descendants in turn.
    warned = False
    deadline = time.monotonic()+1
    children = Path(f'/proc/self/task/{os.getpid()}/children')
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid:
            continue
        try:
            for value in children.read_text().split():
                try:
                    os.kill(int(value), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        except OSError:
            # Do not relinquish leases/private HOME on an inspection failure.
            pass
        if time.monotonic() >= deadline and not warned:
            try:
                print('Migration descendant cleanup pending; runtime lease retained.', file=sys.stderr, flush=True)
            except OSError:
                pass  # Parent/log reader may already be gone; retain ownership.
            warned = True
        time.sleep(.01)


def child_environment(home, model, key):
    # No inherited auth overrides, NODE_OPTIONS, proxy, telemetry, dotenv,
    # extension, workspace, or native-home configuration enters this child.
    return {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(home),
            'GEMINI_CLI_HOME': str(home), 'TMPDIR': str(home/'tmp'),
            'GEMINI_CLI_SYSTEM_SETTINGS_PATH': str(home/'system.json'),
            'GEMINI_CLI_SYSTEM_DEFAULTS_PATH': str(home/'system.json'),
            'GEMINI_API_KEY': key, 'GEMINI_MODEL': model,
            'GEMINI_CLI_TRUST_WORKSPACE': 'true',
            'LANG': 'C.UTF-8', 'NO_COLOR': '1'}


def supervise(command, parent_reader, budget, *, env, cwd):
    subreaper = enable_subreaper()
    process = None
    handlers = {}
    def stop(signum, frame):
        for sig in handlers:
            signal.signal(sig, signal.SIG_IGN)
        raise KeyboardInterrupt
    for sig in (signal.SIGTERM, signal.SIGINT):
        handlers[sig] = signal.signal(sig, stop)
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   start_new_session=True, env=env, cwd=cwd)
        deadline = time.monotonic() + budget
        with selectors.DefaultSelector() as selector:
            if parent_reader is not None:
                selector.register(parent_reader, selectors.EVENT_READ)
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    return 124
                if selector.select(min(.05, max(0, deadline-time.monotonic()))):
                    if not os.read(parent_reader, 1):
                        return 125
        return process.returncode
    except KeyboardInterrupt:
        return 130
    finally:
        for sig in handlers:
            signal.signal(sig, signal.SIG_IGN)
        try:
            if process is not None:
                try:
                    signal_group(process, signal.SIGTERM)
                except OSError:
                    pass
                time.sleep(.25)
                try:
                    signal_group(process, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
                if subreaper:
                    reap_descendants()
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)


def run(reader, lease_descriptor, singleton, timeout, arguments):
    runtime.check_timeout(timeout)
    locks = Path(os.environ['PAPER_AGENT_LIFECYCLE_DIR'])
    check_descriptor(locks/'.runtime.lock', lease_descriptor)
    if singleton >= 0:
        check_descriptor(locks/'.scheduler.lock', singleton)
    runtime.check_ready()
    # Per-call private writable HOME and CWD; removed after descendant cleanup.
    with tempfile.TemporaryDirectory(prefix='paper-gemini-') as temporary:
        home = Path(temporary)
        (home/'.gemini').mkdir(mode=0o700)
        (home/'tmp').mkdir(mode=0o700)
        (home/'work').mkdir(mode=0o700)
        settings = {'security': {'auth': {'selectedType': 'gemini-api-key'}}}
        (home/'.gemini/settings.json').write_text(json.dumps(settings))
        (home/'system.json').write_text('{}')
        env = child_environment(home, runtime.model_policy(), runtime.read_key())
        return supervise([str(runtime.NODE), str(runtime.CLI), *arguments], reader,
                         timeout, env=env, cwd=home/'work')


if __name__ == '__main__':
    try:
        code = run(int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]),
                   float(sys.argv[4]), sys.argv[5:])
    except Exception:
        # Never show exceptions containing a prompt, key, or provider output.
        print('Gemini controlled launcher failed; details withheld.', file=sys.stderr)
        code = 126
    raise SystemExit(code)

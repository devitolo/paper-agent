"""Persistent cron execution container: no listener and no internal scheduler."""
import json
import os
from pathlib import Path
import signal
import time

READY_FILE = Path('/tmp/paper-pipeline-ready.json')


def process_identity(pid):
    # Linux proc start time distinguishes a live owner from a reused/stale PID.
    return (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[19]


def run():
    """Caller owns the existing shared lifecycle lease for this entire lifetime."""
    if os.environ.get('PAPER_AGENT_RUNTIME_ROLE') != 'pipeline':
        raise RuntimeError('Explicit pipeline role required')
    stopping = False
    previous = {}
    def terminate(signum, frame):
        nonlocal stopping
        stopping = True
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, terminate)
        payload = {'role': 'pipeline', 'pid': os.getpid(),
                   'process_start': process_identity(os.getpid())}
        temporary = READY_FILE.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload))
        temporary.replace(READY_FILE)
        print('Pipeline runtime ready; host cron owns scheduling; no web listener.', flush=True)
        while not stopping:
            time.sleep(1)
    finally:
        READY_FILE.unlink(missing_ok=True)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def readiness():
    """Local process and read-only DB check; no network/provider/inference request."""
    if os.environ.get('PAPER_AGENT_RUNTIME_ROLE') != 'pipeline':
        raise RuntimeError('Pipeline readiness requires pipeline role')
    payload = json.loads(READY_FILE.read_text())
    pid = payload.get('pid')
    if payload.get('role') != 'pipeline' or type(pid) is not int or pid <= 0:
        raise RuntimeError('Invalid pipeline readiness identity')
    os.kill(pid, 0)
    if process_identity(pid) != payload.get('process_start'):
        raise RuntimeError('Stale pipeline readiness identity')
    from .package_runtime import app_status
    state = app_status()
    if not state['ready']:
        raise RuntimeError('Pipeline database unavailable')
    return {'ready': True, 'role': 'pipeline', 'database': state['database'],
            'scheduler': 'external', 'web_listener': 'not_started_by_pipeline_runtime'}

"""Serial process-group supervision with wall/output/memory limits."""
from contextlib import contextmanager
import json
import os
import selectors
import signal
import subprocess
import sys
from pathlib import Path
import time
from .contracts import canonical


class ExecutionFailure(RuntimeError):
    """Public message must be a fixed error code, never child output."""


def linux_process_group_rss(pgid, proc_root='/proc', page_size=None):
    """Read Linux process-group RSS without requiring procps in the slim image."""
    total, found = 0, False
    try:
        page_size = page_size or os.sysconf('SC_PAGE_SIZE')
        for path in Path(proc_root).iterdir():
            if not path.name.isdigit():
                continue
            try:
                # comm may contain spaces and parentheses; split after its final ')'.
                fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
                if int(fields[2]) != pgid:
                    continue
                total += int((path / 'statm').read_text().split()[1]) * page_size
                found = True
            except FileNotFoundError:
                continue  # Process exited between directory listing and read.
        return total if found else None
    except (OSError, ValueError, IndexError):
        return None


def process_group_rss(pgid):
    if sys.platform.startswith('linux'):
        return linux_process_group_rss(pgid)
    # ps is bounded, local, and includes descendants in the worker process group.
    # Sum of RSS can double-count shared pages; this is not unique physical memory.
    try:
        result = subprocess.run(['ps', '-axo', 'pgid=,rss='], capture_output=True, timeout=0.5)
        if result.returncode:
            return None
        values = [int(parts[1])*1024 for line in result.stdout.splitlines()
                  if len(parts := line.split()) == 2 and int(parts[0]) == pgid]
        return sum(values) if values else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def machine_memory():
    """Best-effort Linux availability/swap counters; unknown on unsupported hosts."""
    from pathlib import Path
    try:
        values = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, rest = line.split(':', 1)
            if key in {'MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'}:
                values[key] = int(rest.split()[0]) * 1024
        return {key: values.get(key) for key in ('MemTotal','MemAvailable','SwapTotal','SwapFree')}
    except (OSError, ValueError):
        return {key: None for key in ('MemTotal','MemAvailable','SwapTotal','SwapFree')}


@contextmanager
def cancellation():
    old = {}
    def stop(signum, frame):
        raise KeyboardInterrupt
    for sig in (signal.SIGINT, signal.SIGTERM):
        old[sig] = signal.signal(sig, stop)
    try:
        yield
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


class Worker:
    def __init__(self, spec, limits, run_deadline, sampler=process_group_rss):
        self.spec, self.limits, self.run_deadline, self.sampler = spec, limits, run_deadline, sampler
        self.process = None
        self.peak_rss = None
        self.measurement_missing = False
        self.stdout_bytes = self.stderr_bytes = 0
        self.calls = 0
        self.load_seconds = None

    def start(self):
        if os.name != 'posix':
            raise ExecutionFailure('unsupported_supervisor_platform')
        start = time.monotonic()
        self.process = subprocess.Popen([sys.executable, '-m', 'experiments.topical_relevance.worker'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        response = self.request(self.spec, self.limits['load_seconds'])
        from .contracts import digest
        if response.get('status') != 'ready' or response.get('adapter_sha256') != digest(self.spec):
            raise ExecutionFailure('adapter_handshake_mismatch')
        if self.spec['kind'] == 'minilm_onnx' and response.get('learned_runtime') != getattr(self, 'expected_runtime', None):
            raise ExecutionFailure('learned_worker_fingerprint_mismatch')
        self.load_seconds = time.monotonic()-start

    def request(self, payload, seconds):
        deadline = min(time.monotonic()+seconds, self.run_deadline)
        data = (canonical(payload)+'\n').encode()
        try:
            # Input has a frozen small cap. Nonblocking write/read share one deadline.
            os.set_blocking(self.process.stdin.fileno(), False)
            pending = memoryview(data)
            out, stderr_count = bytearray(), 0
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdin, selectors.EVENT_WRITE, 'input')
                selector.register(self.process.stdout, selectors.EVENT_READ, 'stdout')
                selector.register(self.process.stderr, selectors.EVENT_READ, 'stderr')
                next_sample = 0
                while True:
                    now = time.monotonic()
                    if now >= deadline:
                        raise ExecutionFailure('wall_deadline')
                    if now >= next_sample:
                        rss = self.sampler(self.process.pid)
                        if rss is None:
                            self.measurement_missing = True
                            if self.limits['require_memory_measurement']:
                                raise ExecutionFailure('memory_measurement_unavailable')
                        else:
                            self.peak_rss = max(self.peak_rss or 0, rss)
                            if rss > self.limits['max_rss_bytes']:
                                raise ExecutionFailure('memory_limit')
                        next_sample = now + .1
                    for key, _ in selector.select(min(.05, max(0, deadline-time.monotonic()))):
                        if key.data == 'input':
                            n = os.write(key.fd, pending)
                            pending = pending[n:]
                            if not pending:
                                selector.unregister(key.fileobj)
                            continue
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            if key.data == 'stdout':
                                raise ExecutionFailure('worker_ended_without_response')
                            continue
                        if key.data == 'stderr':
                            self.stderr_bytes += len(chunk)
                            stderr_count += len(chunk)
                        else:
                            self.stdout_bytes += len(chunk)
                            out.extend(chunk)
                        if len(out) + stderr_count > self.limits['response_bytes']:
                            raise ExecutionFailure('output_limit')
                        if b'\n' in out:
                            line, trailing = bytes(out).split(b'\n', 1)
                            if trailing.strip():
                                raise ExecutionFailure('unexpected_extra_output')
                            try:
                                reply = json.loads(line)
                            except (ValueError, UnicodeError):
                                raise ExecutionFailure('malformed_response') from None
                            if not isinstance(reply, dict):
                                raise ExecutionFailure('malformed_response')
                            return reply
        except (OSError, BrokenPipeError):
            raise ExecutionFailure('worker_io_failure') from None

    def close(self):
        if self.process is None:
            return
        process = self.process
        # Ignore repeated signals during bounded process-group cleanup.
        old = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            for sig, pause in ((signal.SIGTERM, .1), (signal.SIGKILL, 0)):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    # macOS may report EPERM for an unreaped exited group leader.
                    if process.poll() is None:
                        raise
                    try:
                        os.killpg(process.pid, sig)
                    except ProcessLookupError:
                        pass
                time.sleep(pause)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                raise ExecutionFailure('cleanup_incomplete') from None
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
            for sig, handler in old.items():
                signal.signal(sig, handler)

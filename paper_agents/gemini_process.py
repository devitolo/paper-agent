"""Bounded Gemini CLI lifecycle. Diagnostics deliberately exclude output text."""
from __future__ import annotations

from contextlib import contextmanager
import os
import logging
import sys
import signal
import subprocess
import threading
import time

CLEANUP_GRACE_SECONDS = 1.0


def _signal_process(process: subprocess.Popen, force: bool) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        elif process.poll() is None:
            process.kill() if force else process.terminate()
    except ProcessLookupError:
        pass
    except PermissionError:
        # On macOS signalling a group containing only an unreaped zombie can
        # report EPERM. Reap the leader, then retry the group (ESRCH if gone).
        if process.poll() is None:
            raise
        try:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            pass


def _cleanup(process: subprocess.Popen) -> None:
    try:
        # Do not probe with killpg(..., 0): some hosts deny probes during group
        # teardown. Always complete TERM -> bounded grace -> KILL instead.
        try:
            _signal_process(process, False)
        except OSError:
            # Still attempt KILL even if graceful termination was denied.
            pass
        time.sleep(CLEANUP_GRACE_SECONDS)
        _signal_process(process, True)
        try:
            process.wait(timeout=CLEANUP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _signal_process(process, True)
            raise RuntimeError("Gemini CLI process could not be reaped within cleanup deadline.") from None
    finally:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


@contextmanager
def _cleanup_signals():
    # A second cancellation must not interrupt the bounded KILL/reap phase.
    handlers = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            handlers[sig] = signal.signal(sig, signal.SIG_IGN)
    try:
        yield
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


@contextmanager
def _termination_as_exception():
    # Ignore repeats as soon as cancellation starts, including communicate's
    # internal KeyboardInterrupt wait before our finally block is reached.
    handlers = {}
    def terminate(signum, frame):
        for sig in handlers:
            signal.signal(sig, signal.SIG_IGN)
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)
    if os.name == "posix" and threading.current_thread() is threading.main_thread():
        for sig, default in ((signal.SIGTERM, signal.SIG_DFL),
                             (signal.SIGINT, signal.default_int_handler)):
            if signal.getsignal(sig) == default:
                handlers[sig] = signal.signal(sig, terminate)
    try:
        yield
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def _diagnostics(stdout: bytes | None, stderr: bytes | None, *, classify: bool = False) -> str:
    stdout, stderr = stdout or b"", stderr or b""
    detail = f"stdout_bytes={len(stdout)}, stderr_bytes={len(stderr)}; output withheld"
    if classify:
        # Preserve existing quota fallback triggers without echoing provider text,
        # which can contain prompts, reviews, credentials, or user profile data.
        text = (stderr or stdout).lower()
        if any(marker in text for marker in (b"429", b"quota", b"rate limit", b"ratelimit",
                                             b"too many requests", b"toomanyrequests",
                                             b"free_tier_requests", b"exhausted")):
            detail += "; provider_category=quota"
    return detail


def run_gemini(command: list[str], timeout: int) -> str:
    process = None
    with _termination_as_exception():
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=os.name == "posix")
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                details = _diagnostics(error.output, error.stderr)
                raise RuntimeError(f"Gemini CLI timed out after {timeout} seconds while updating the feedback profile. {details}") from None
            if process.returncode:
                raise RuntimeError(f"Gemini CLI failed (exit {process.returncode}): "
                                   + _diagnostics(stdout, stderr, classify=True))
            return stdout.decode("utf-8", errors="replace")
        except FileNotFoundError:
            raise RuntimeError("Gemini CLI was not found. Install and authenticate `gemini`, then retry.") from None
        except OSError:
            raise RuntimeError("Gemini CLI process I/O failed; output withheld.") from None
        finally:
            if process is not None:
                original_error = sys.exc_info()[1]
                with _cleanup_signals():
                    try:
                        _cleanup(process)
                    except (OSError, RuntimeError):
                        detail = "Gemini CLI process cleanup incomplete; output withheld."
                        if original_error is None:
                            raise RuntimeError(detail) from None
                        if isinstance(original_error, RuntimeError):
                            original_error.args = (str(original_error) + " " + detail,)
                        else:
                            logging.getLogger(__name__).warning(detail)

"""Bounded Gemini CLI lifecycle. Diagnostics deliberately exclude output text."""
from __future__ import annotations

from contextlib import contextmanager
import os
import logging
import json
import selectors
import re
import sys
import signal
import subprocess
import threading
import time

CLEANUP_GRACE_SECONDS = 1.0
COMPLETION_EXIT_GRACE_SECONDS = 1.0
MAX_STREAM_BYTES = 8 * 1024 * 1024


def _quota_hint(output: bytes) -> str:
    text = output.lower()
    if any(marker in text for marker in (b"429", b"quota", b"rate limit", b"ratelimit",
                                         b"too many requests", b"toomanyrequests",
                                         b"free_tier_requests", b"exhausted")):
        return "; provider_category=quota"
    return ""


def _provider_failure(stderr: bytes) -> dict:
    """Allowlisted operational hints only; never persist arbitrary stderr."""
    text = stderr.decode("utf-8", errors="replace").lower()
    codes = re.findall(r'(?:status|code)[\s\\"\':=]+(429|500|502|503|504)\b', text)
    category = ("unavailable" if "503" in codes or "high demand" in text
                else "quota" if "429" in codes or "quota" in text
                else "server_error" if codes else None)
    return {"provider_category": category, "http_status": int(codes[-1]) if codes else None,
            "internal_retry_observed": "retrying" in text or "retry with backoff" in text}


class _CompletionStream:
    """Gemini CLI's JSONL protocol, not JSON guessed from model prose.

    See geminicli.com/docs/cli/headless/ and upstream core/src/output/types.ts.
    A model message is never a completion signal; only a successful result is.
    Profile updates are a single text response, so tool/error events fail closed.
    """

    def __init__(self):
        self.initialized = False
        self.complete = False
        self.chunks = []
        self.events = 0

    def feed(self, line: bytes) -> None:
        try:
            event = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            raise RuntimeError("Gemini CLI returned malformed stream framing; output withheld.") from None
        if not isinstance(event, dict) or self.complete:
            raise RuntimeError("Gemini CLI returned unexpected stream framing; output withheld.")
        self.events += 1
        kind = event.get("type")
        if kind == "error" or (kind == "result" and (event.get("status") != "success" or "error" in event)):
            error_payload = event.get("error", event.get("message", ""))
            raise RuntimeError("Gemini CLI reported a stream error; output withheld"
                               + _quota_hint(json.dumps(error_payload).encode()))
        if kind == "init" and not self.initialized and self.events == 1:
            self.initialized = True
        elif kind == "message" and self.initialized and isinstance(event.get("content"), str):
            if event.get("role") == "assistant" and event.get("delta") is True:
                self.chunks.append(event["content"])
            elif event.get("role") != "user" or self.chunks:
                raise RuntimeError("Gemini CLI returned unexpected message framing; output withheld.")
        elif kind == "result" and self.initialized and "".join(self.chunks).strip():
            self.complete = True
        else:
            raise RuntimeError("Gemini CLI returned unsupported or incomplete stream framing; output withheld.")


def _read_completion(process, timeout: int, metadata: dict | None, fail_fast_provider_errors: bool = False) -> str:
    """Drain both pipes with a deadline, then allow a bounded terminal-exit grace.

    The documented terminal result can precede CLI teardown. Once it is verified,
    cleanup in run_gemini still kills/reaps the entire group before returning.
    """
    if os.name != "posix":
        raise RuntimeError("Gemini CLI streaming completion requires a POSIX host.")
    stream = _CompletionStream()
    stdout, stderr, pending = bytearray(), bytearray(), bytearray()
    deadline = time.monotonic() + timeout
    completion_deadline = None
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        try:
            while True:
                now = time.monotonic()
                remaining = (completion_deadline if stream.complete else deadline) - now
                if remaining <= 0:
                    if stream.complete:
                        break
                    raise RuntimeError(f"Gemini CLI timed out after {timeout} seconds while updating the feedback profile. "
                                       + _diagnostics(stdout, stderr) + f"; completion_seen=no; events={stream.events}")
                if not selector.get_map():
                    if process.poll() is not None or not stream.complete:
                        break
                    time.sleep(min(.02, remaining))
                    continue
                for key, _ in selector.select(min(remaining, .1)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    target = stdout if key.data == "stdout" else stderr
                    target.extend(chunk)
                    if len(stdout) + len(stderr) > MAX_STREAM_BYTES:
                        raise RuntimeError("Gemini CLI exceeded the output limit; output withheld.")
                    if key.data == "stderr" and fail_fast_provider_errors:
                        hint = _provider_failure(stderr)
                        if hint["provider_category"]:
                            raise RuntimeError("Gemini provider failure: " + str(hint["provider_category"]) + "; output withheld.")
                    if key.data == "stdout":
                        pending.extend(chunk)
                        while b"\n" in pending:
                            line, _, rest = pending.partition(b"\n")
                            pending = bytearray(rest)
                            if not line.strip():
                                continue
                            stream.feed(line)
                            if stream.complete and completion_deadline is None:
                                completion_deadline = min(deadline, time.monotonic() + COMPLETION_EXIT_GRACE_SECONDS)
            code = process.poll()
            if code not in (None, 0):
                raise RuntimeError(f"Gemini CLI failed (exit {code}): " + _diagnostics(stdout, stderr, classify=bool(stderr)))
            if pending.strip() or not stream.complete:
                raise RuntimeError("Gemini CLI ended without a complete successful result; " + _diagnostics(stdout, stderr, classify=bool(stderr)))
            return "".join(stream.chunks)
        finally:
            if metadata is not None:
                metadata.update(_provider_failure(stderr))
                metadata.update(stdout_bytes=len(stdout), stderr_bytes=len(stderr),
                                events=stream.events, completion_seen=stream.complete,
                                exit_code_before_cleanup=process.poll())


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
        detail += _quota_hint(stderr or stdout)
    return detail


def run_gemini(command: list[str], timeout: int, *, stream_json: bool = False,
               metadata: dict | None = None, fail_fast_provider_errors: bool = False) -> str:
    process = None
    with _termination_as_exception():
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=os.name == "posix")
            if stream_json:
                return _read_completion(process, timeout, metadata, fail_fast_provider_errors)
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

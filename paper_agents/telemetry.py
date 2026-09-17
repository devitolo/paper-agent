"""Opt-in, metadata-only tracing of automated pipelines.

No auto-instrumentation, global OTel provider, content capture, or SDK atexit
shutdown. Export runs on one bounded daemon worker per process. Pipeline flush
waits at most FLUSH_SECONDS; lost spans are preferable to blocking research work.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import http.client
import math
import os
import queue
import re
import threading
import time
from urllib.parse import urlsplit

QUEUE_SIZE = 256
BATCH_SIZE = 32
EXPORT_TIMEOUT = 1.0
FLUSH_SECONDS = .25
DEFAULT_ENDPOINT = "http://127.0.0.1:6006/v1/traces"
_active = ContextVar("paper_telemetry_backend", default=None)
_current = ContextVar("paper_telemetry_span", default=None)
_backend = None
_init_lock = threading.Lock()

# Application names mapped centrally to portable OTel/OpenInference attributes.
_COUNTS = {"workflow_cycle_id", "profile_version_id", "scout_run_id", "curator_run_id",
           "paper_id", "recommendation_id", "scout_candidate_id", "guidance_id",
           "attempt", "fetched_count", "stored_count", "eligible_count", "reviewed_count",
           "candidate_count", "recommendation_count", "chunk_count", "chunk_index",
           "error_count", "warning_count", "http_status"}
_TOKENS = {"prompt_tokens": "llm.token_count.prompt", "completion_tokens": "llm.token_count.completion"}
_IDENTIFIERS = {"model": "llm.model_name", "prompt_version": "paper.prompt_version",
                "scoring_version": "paper.scoring_version"}
_ENUMS = {"source": {"arxiv", "openalex", "semantic_scholar"}, "provider": {"ollama"},
          "outcome": {"ok", "failed", "unavailable", "skipped"},
          "fallback": {"source_abstract", "deterministic_merge"},
          "error_type": {"http", "timeout", "network", "invalid_response", "application"}}
_NAMES = {"pipeline.daily", "scout", "source.http", "curator", "curator.evidence",
          "curator.evaluate", "reviewer", "reviewer.paper", "reviewer.extract",
          "reviewer.prepare_text", "reviewer.http", "reviewer.chunk", "reviewer.synthesis",
          "ollama.generate", "telemetry.smoke"}


def _attributes(values):
    result = {}
    for key, value in values.items():
        if key in _COUNTS and type(value) is int and 0 <= value < 2**63:
            result["paper." + key] = value
        elif key in _TOKENS and type(value) is int and 0 <= value < 2**63:
            result[_TOKENS[key]] = value
        elif key in _IDENTIFIERS and isinstance(value, str) and len(value) <= 128:
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*", value) and "://" not in value:
                result[_IDENTIFIERS[key]] = value
        elif key in _ENUMS and isinstance(value, str) and value in _ENUMS[key]:
            result["llm.provider" if key == "provider" else "paper." + key] = value
        elif key in {"requested_rescout", "synthetic"} and type(value) is bool:
            result["paper." + key] = value
        elif key == "delay_seconds" and type(value) in (int, float) and math.isfinite(value) and value >= 0:
            result["paper." + key] = min(value, 86400)
    return result


def attributes(**values):
    try:
        current = _current.get()
        if current is not None:
            current.set_attributes(_attributes(values))
    except Exception:
        pass


def event(name, **values):
    try:
        current = _current.get()
        if current is not None and name in {"retry", "fallback", "rescout", "recommendation"}:
            current.add_event(name, _attributes(values))
    except Exception:
        pass


def failure(error_type="application", *, http_status=None):
    attributes(error_type=error_type, http_status=http_status, outcome="failed")
    try:
        current = _current.get()
        if current is not None:
            from opentelemetry.trace import StatusCode
            current.set_status(StatusCode.ERROR)  # No exception description/stack.
    except Exception:
        pass


class BoundedProcessor:
    """SDK SpanProcessor protocol; enqueue never waits, overflow drops newest."""

    def __init__(self, exporter):
        self.exporter = exporter
        self.queue = queue.Queue(maxsize=QUEUE_SIZE)
        self.stopping = threading.Event()
        self.dropped = 0
        self.export_failures = 0
        self.worker = threading.Thread(target=self._work, name="paper-telemetry", daemon=True)
        self.worker.start()

    def on_start(self, span, parent_context=None):
        pass

    def on_end(self, span):
        try:
            if self.stopping.is_set():
                self.dropped += 1
            else:
                self.queue.put_nowait(span)
        except queue.Full:
            self.dropped += 1
        except Exception:
            pass

    def _work(self):
        while not self.stopping.is_set() or not self.queue.empty():
            try:
                batch = [self.queue.get(timeout=.05)]
            except queue.Empty:
                continue
            while len(batch) < BATCH_SIZE:
                try:
                    batch.append(self.queue.get_nowait())
                except queue.Empty:
                    break
            try:
                result = self.exporter.export(tuple(batch))
                if result is False or getattr(result, "name", None) == "FAILURE":
                    self.export_failures += 1
            except Exception:
                self.export_failures += 1  # Never log provider text or URLs.
            finally:
                for _ in batch:
                    self.queue.task_done()

    def force_flush(self, timeout_millis=250):
        deadline = time.monotonic() + min(FLUSH_SECONDS, max(0, timeout_millis / 1000))
        while self.queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(min(.005, max(0, deadline - time.monotonic())))
        return not self.queue.unfinished_tasks

    def shutdown(self):
        self.stopping.set()
        self.worker.join(timeout=FLUSH_SECONDS)


class HTTPExporter:
    """Standard OTLP protobuf over HTTP, no retry/proxy/env headers/body logging."""

    def __init__(self, endpoint):
        parsed = urlsplit(endpoint)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
                parsed.username or parsed.password or parsed.query or parsed.fragment or
                parsed.path != "/v1/traces"):
            raise ValueError("Invalid telemetry endpoint")
        self.host, self.port = parsed.hostname, parsed.port
        self.secure = parsed.scheme == "https"

    def export(self, spans):
        from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
        connection_type = http.client.HTTPSConnection if self.secure else http.client.HTTPConnection
        connection = connection_type(self.host, self.port, timeout=EXPORT_TIMEOUT)
        try:
            connection.request("POST", "/v1/traces", body=encode_spans(spans).SerializeToString(),
                               headers={"Content-Type": "application/x-protobuf"})
            response = connection.getresponse()
            return 200 <= response.status < 300
        finally:
            connection.close()


class Backend:
    def __init__(self, exporter):
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider, SpanLimits
        from opentelemetry.sdk.trace.sampling import ALWAYS_ON
        self.provider = TracerProvider(
            resource=Resource({"service.name": "project-paper", "openinference.project.name": "project-paper"}),
            sampler=ALWAYS_ON, shutdown_on_exit=False,
            span_limits=SpanLimits(max_attributes=32, max_events=16, max_event_attributes=8),
        )
        self.processor = BoundedProcessor(exporter)
        self.provider.add_span_processor(self.processor)
        self.tracer = self.provider.get_tracer("paper_agents.telemetry", "1")


def _get_backend():
    global _backend
    if os.environ.get("PAPER_AGENT_TELEMETRY") != "1":
        return None
    # Never wait for another invocation's initialization.
    if not _init_lock.acquire(blocking=False):
        return None
    try:
        if _backend is None:
            _backend = Backend(HTTPExporter(os.environ.get("PAPER_AGENT_OTLP_ENDPOINT", DEFAULT_ENDPOINT)))
        return _backend
    except Exception:
        return None
    finally:
        _init_lock.release()


@contextmanager
def span(name, kind="CHAIN", **values):
    backend = _active.get()
    manager = None
    token = None
    try:
        if backend is not None and name in _NAMES and kind in {"CHAIN", "TOOL", "LLM"}:
            attrs = _attributes(values)
            attrs["openinference.span.kind"] = kind
            manager = backend.tracer.start_as_current_span(
                name, attributes=attrs, record_exception=False, set_status_on_exception=False)
            token = _current.set(manager.__enter__())
    except Exception:
        manager = None
    try:
        yield
    except BaseException as error:
        import urllib.error
        category = ("http" if isinstance(error, urllib.error.HTTPError) else
                    "timeout" if isinstance(error, TimeoutError) else
                    "network" if isinstance(error, OSError) else "application")
        failure(category, http_status=error.code if isinstance(error, urllib.error.HTTPError) else None)
        raise
    finally:
        if manager is not None:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                pass
        if token is not None:
            try:
                _current.reset(token)
            except Exception:
                pass


def traced(name, kind="CHAIN", **values):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with span(name, kind, **values):
                result = function(*args, **kwargs)
                if isinstance(result, dict):
                    # Only fixed scalar keys; never traverse content/error values.
                    attributes(**{k: result[k] for k in _COUNTS | {"requested_rescout"} if k in result})
                return result
        return wrapped
    return decorate


def pipeline_trace(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            backend = _get_backend()
        except Exception:
            backend = None
        if backend is None:
            return function(*args, **kwargs)
        try:
            from opentelemetry.context import Context, attach, detach
            context_token = attach(Context())  # Automated invocations are independent roots.
        except Exception:
            return function(*args, **kwargs)
        active_token = _active.set(backend)
        try:
            with span("pipeline.daily", source=kwargs.get("source_name", "arxiv")):
                return function(*args, **kwargs)
        finally:
            try:
                detach(context_token)
                _active.reset(active_token)
            except Exception:
                pass
            try:
                backend.processor.force_flush()
            except Exception:
                pass
    return wrapped

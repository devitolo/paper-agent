"""Bounded, resumable Semantic Scholar normal-search retrieval (opt-in)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from collections import deque
from email.utils import parsedate_to_datetime
import errno
import hashlib
import json
import random
import re
import socket
import ssl
import time
import urllib.error

from paper_agents import db
from paper_agents.scout import dedupe_candidates, semantic_scholar_paper_to_candidate


ATTEMPT_BUDGET = 3
PAGE_SIZE = 10
REFRESH_SECONDS = 7 * 86400
TRAVERSAL_SECONDS = 30 * 86400
TRANSIENT_HTTP_STATUSES = {500, 502, 503, 504}
PROVIDER_ERROR_BODY_READ_LIMIT = 4096
PROVIDER_ERROR_BODY_REPORT_LIMIT = 1000
PROVIDER_ERROR_HEADERS = {
    "retry-after",
    "ratelimit-limit",
    "ratelimit-remaining",
    "ratelimit-reset",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "request-id",
    "x-request-id",
    "cf-ray",
}
SAFE_PROVIDER_ERROR_FIELDS = {"code", "detail", "error", "message", "status", "type"}


class SemanticScholarProgress:
    def __init__(self, connection, source, *, now=None, experiment=False):
        if experiment and len(source.api_keys) != 2:
            raise ValueError("Pagination experiment requires two distinct keys")
        self.experiment = experiment
        self.attempt_budget = 4 if experiment else ATTEMPT_BUDGET
        # An explicit experiment-only wall limit also bounds Retry-After sleeps.
        self.time_budget = min(240, 4 * source.timeout) if experiment else None
        self.deadline = None
        self.connection = connection
        self.source = source
        self.source.begin_run()
        self.now = now or datetime.now(timezone.utc)
        self.original = dict(connection.execute(
            "SELECT key, value_json FROM semantic_scholar_search_state"
        ))
        self.states = {key: json.loads(value) for key, value in self.original.items()}
        self.pages = []
        self.diagnostics = {
            "mode": "offset_v1",
            "endpoint": source.api_url,
            "attempt_budget": self.attempt_budget,
            "budget_scope": "metadata_search_http_attempts",
            "attempts": 0,
            "pages": [],
            "stop_reason": "request_budget_reached",
        }
        if experiment:
            self.diagnostics.update(experiment="four_page_key_pair_v1", page_budget=4,
                                    time_budget_seconds=self.time_budget, pagination_key_switches=0)
        self.diagnostics.update(source.diagnostics())

    def _new_traversal(self, months):
        return {
            "offset": 0,
            "cutoff": (self.now.date() - timedelta(days=months * 31)).isoformat(),
            "started": self.now.timestamp(),
            "exhausted": False,
        }

    def fetch(self, topics, *, freshness_months, max_candidates, errors):
        if self.connection.in_transaction:
            raise RuntimeError("Semantic Scholar HTTP must start outside a write transaction")
        if self.experiment:
            self.deadline = time.monotonic() + self.time_budget
        clock = self.now.timestamp()
        control = self.states.setdefault("@source", {"not_before": 0, "sequence": 0})
        if control["not_before"] > clock:
            self.diagnostics.update(
                stop_reason="source_cooldown",
                not_before=control["not_before"],
                deferred_queries=list(topics),
            )
            return []

        queries = []
        query_keys = set()
        for topic in dict.fromkeys(topic for topic in topics if topic.strip()):
            contract = [1, self.source.api_url, "normal_search", topic.strip(), freshness_months, PAGE_SIZE]
            key = hashlib.sha256(json.dumps(contract).encode()).hexdigest()
            if key in query_keys:
                continue
            query_keys.add(key)
            if key not in self.states:
                self.states[key] = {
                    "last_served": -1,
                    "last_refresh": clock,
                    "deep": self._new_traversal(freshness_months),
                }
            queries.append((key, topic))
        queries.sort(key=lambda pair: self.states[pair[0]]["last_served"])

        known_keys = {row[0] for row in self.connection.execute("SELECT canonical_key FROM papers")}
        seen_keys = set(known_keys)
        candidates = []
        attempted = []
        queue = deque(queries)
        page_fingerprints = {}
        while queue:
            key, topic = queue.popleft()
            if self.experiment and time.monotonic() >= self.deadline:
                self.diagnostics["stop_reason"] = "time_budget_reached"
                break
            if self.diagnostics["attempts"] >= self.attempt_budget:
                break
            remaining = max_candidates - len(candidates)
            if remaining <= 0:
                self.diagnostics["stop_reason"] = "candidate_budget_reached"
                break
            state = self.states[key]
            deep = state["deep"]
            if clock - deep["started"] >= TRAVERSAL_SECONDS:
                state["deep"] = deep = self._new_traversal(freshness_months)
                state["last_refresh"] = clock
            refresh = clock - state["last_refresh"] >= REFRESH_SECONDS
            if deep["exhausted"] and not refresh:
                continue
            traversal = self._new_traversal(freshness_months) if refresh else deep
            attempted.append(topic)
            control["sequence"] += 1
            state["last_served"] = control["sequence"]
            try:
                if self.experiment and len(self.pages) >= 2 and self.source.key_slot == 1:
                    self.source.key_slot = 2
                    self.source.api_key = self.source.api_keys[1]
                    self.diagnostics["pagination_key_switches"] += 1
                    self.source._record("pagination_key_switch", to_key_slot="key2")
                payload = self._fetch_page(
                    topic,
                    offset=traversal["offset"],
                    cutoff=traversal["cutoff"],
                    page_size=min(PAGE_SIZE, remaining),
                )
                if self.experiment and payload["next"] is not None and (
                        not isinstance(payload["next"], int) or payload["next"] <= traversal["offset"]):
                    raise ValueError("Semantic Scholar returned an invalid next offset")
                repeated = False
                if self.experiment:
                    fingerprint = tuple(sorted(str(paper.get("paperId") or json.dumps(paper, sort_keys=True))
                                               for paper in payload["data"]))
                    repeated = fingerprint in page_fingerprints.setdefault(key, set())
                    page_fingerprints[key].add(fingerprint)
                dispositions = []
                accepted = []
                for paper in payload["data"]:
                    reason, candidate = semantic_scholar_disposition(
                        paper, cutoff=traversal["cutoff"], seen_keys=seen_keys
                    )
                    dispositions.append({
                        "id": paper.get("paperId"),
                        "reason": reason,
                        "paper": paper,
                    })
                    if reason == "accepted":
                        candidate.metadata["query_topic"] = topic
                        canonical_key = db.canonical_key_for_candidate(candidate.as_dict())
                        seen_keys.add(canonical_key)
                        accepted.append(candidate)
                page = {
                    "offset": traversal["offset"],
                    "next_offset": payload["next"],
                    "cutoff": traversal["cutoff"],
                    "kind": "freshness" if refresh else "continuation",
                    "dispositions": dispositions,
                }
                if self.experiment:
                    page["key_slot"] = f"key{self.source.key_slot}"
                self.pages.append((key, topic, page))
                self.diagnostics.update(self.source.diagnostics())
                candidates = dedupe_candidates([*candidates, *accepted])
                self.diagnostics["pages"].append({
                    "topic": topic,
                    "offset": page["offset"],
                    "kind": page["kind"],
                    "raw_count": len(dispositions),
                    "accepted_count": len(accepted),
                    "rejected_count": len(dispositions) - len(accepted),
                    "provider_end": payload["next"] is None,
                })
                if repeated:
                    self.diagnostics["stop_reason"] = "repeated_provider_page"
                    break
                if refresh:
                    # A freshness sample must not erase or starve the deeper checkpoint.
                    state["last_refresh"] = clock
                else:
                    deep["offset"] = payload["next"]
                    deep["exhausted"] = payload["next"] is None
                if self.experiment:
                    if accepted:
                        self.diagnostics["stop_reason"] = "new_paper_found"
                        break
                    if payload["next"] is None:
                        self.diagnostics["stop_reason"] = "provider_exhausted"
                        break
                    if len(self.pages) >= 4:
                        self.diagnostics["stop_reason"] = "page_budget_reached"
                        break
                    # Revisit only after the remaining queries get their fair turn.
                    queue.append((key, topic))
            except urllib.error.HTTPError as error:
                provider_error = sanitized_provider_error(error, api_keys=self.source.api_keys)
                if error.code == 429:
                    delay = cooldown_seconds(
                        error.headers.get("Retry-After") if error.headers else None, self.now
                    )
                    control["not_before"] = clock + delay
                    self._persist_cooldown(control["not_before"])
                    self.diagnostics.update(
                        stop_reason=self.source.last_diagnostics.get("stop_reason", "source_cooldown"),
                        not_before=control["not_before"],
                        provider_error=provider_error,
                    )
                    self.diagnostics.update(self.source.diagnostics())
                    break
                errors.append(f"Semantic Scholar HTTP {error.code}: {topic}")
                self.diagnostics.update(
                    stop_reason="source_error", provider_error=provider_error
                )
                self.diagnostics.update(self.source.diagnostics())
                break
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
                errors.append(f"Semantic Scholar page failed ({type(error).__name__}): {topic}")
                stop_reason = "source_error"
                if self.experiment and isinstance(error, TimeoutError) and (
                        time.monotonic() >= self.deadline or "time budget exhausted" in str(error)):
                    stop_reason = "time_budget_reached"
                elif self.experiment and "invalid next offset" in str(error):
                    stop_reason = "non_advancing_cursor"
                self.diagnostics.update(
                    stop_reason=stop_reason,
                    transport_error={
                        "error_type": type(error).__name__,
                        "reason_type": type(error.reason).__name__
                        if isinstance(error, urllib.error.URLError) else None,
                    },
                )
                self.diagnostics.update(self.source.diagnostics())
                break

        if (self.experiment and not queue and attempted
                and self.diagnostics["stop_reason"] == "request_budget_reached"
                and self.diagnostics["attempts"] < self.attempt_budget):
            self.diagnostics["stop_reason"] = "traversals_exhausted_until_refresh"
        if not attempted:
            self.diagnostics["stop_reason"] = "traversals_exhausted_until_refresh"
        elif (not self.experiment and len(attempted) == len(queries)
              and self.diagnostics["stop_reason"] == "request_budget_reached"):
            self.diagnostics["stop_reason"] = "query_round_complete"
        self.diagnostics.update(
            deferred_queries=[topic for _, topic in queries if topic not in attempted],
            unique_count=len(candidates),
        )
        return candidates

    def _fetch_page(self, topic, **kwargs):
        """Use at most one budgeted retry for a transient provider failure."""
        retried = False
        delay = self.source.request_delay
        while self.diagnostics["attempts"] < self.attempt_budget:
            if self.diagnostics["attempts"]:
                pause = delay if retried else self.source.request_delay
                if self.experiment and time.monotonic() + pause >= self.deadline:
                    raise TimeoutError("Semantic Scholar experiment time budget exhausted")
                time.sleep(pause)
            try:
                remaining = self.attempt_budget - self.diagnostics["attempts"]
                if self.experiment:
                    self.source.last_request_attempts = 0
                extra = {"deadline": self.deadline} if self.experiment else {}
                result = self.source.fetch_page(topic, max_attempts=remaining, **extra, **kwargs)
                self.diagnostics["attempts"] += max(1, self.source.last_request_attempts)
                return result
            except urllib.error.HTTPError as error:
                self.diagnostics["attempts"] += max(1, self.source.last_request_attempts)
                if (
                    error.code not in TRANSIENT_HTTP_STATUSES
                    or retried
                    or self.diagnostics["attempts"] >= self.attempt_budget
                ):
                    raise
                delay = transient_retry_seconds(
                    error.headers.get("Retry-After") if error.headers else None,
                    self.now,
                    fallback=self.source.request_delay,
                )
                retried = True
                self.diagnostics.setdefault("transient_retries", []).append({
                    "topic": topic,
                    "http_status": error.code,
                    "delay_seconds": delay,
                })
            except urllib.error.URLError as error:
                self.diagnostics["attempts"] += max(1, self.source.last_request_attempts)
                if (
                    not transient_url_error(error)
                    or retried
                    or self.diagnostics["attempts"] >= self.attempt_budget
                ):
                    raise
                retried = True
                delay = self.source.request_delay
                self.diagnostics.setdefault("transient_retries", []).append({
                    "topic": topic,
                    "transport_error": type(error.reason).__name__,
                    "delay_seconds": delay,
                })
            except RuntimeError:
                self.diagnostics["attempts"] += max(1, self.source.last_request_attempts)
                raise
            except (OSError, ValueError, KeyError, TypeError):
                if self.experiment:
                    self.diagnostics["attempts"] += self.source.last_request_attempts
                raise
        raise RuntimeError("Semantic Scholar request budget exhausted")

    def _persist_cooldown(self, not_before):
        old = self.original.get("@source")
        current = json.loads(old) if old else {"sequence": 0, "not_before": 0}
        current["not_before"] = max(current["not_before"], not_before)
        encoded = json.dumps(current, sort_keys=True)
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            actual = self.connection.execute(
                "SELECT value_json FROM semantic_scholar_search_state WHERE key='@source'"
            ).fetchone()
            if (actual[0] if actual else None) != old:
                raise RuntimeError("Concurrent Semantic Scholar retrieval changed state; retry later")
            self.connection.execute(
                "INSERT OR REPLACE INTO semantic_scholar_search_state VALUES (?, ?)",
                ("@source", encoded),
            )
        self.original["@source"] = encoded

    def checkpoint(self, scout_run_id):
        actual = dict(self.connection.execute(
            "SELECT key, value_json FROM semantic_scholar_search_state"
        ))
        if actual != self.original:
            raise RuntimeError("Concurrent Semantic Scholar retrieval changed state; retry later")
        for key, value in self.states.items():
            self.connection.execute(
                "INSERT OR REPLACE INTO semantic_scholar_search_state VALUES (?, ?)",
                (key, json.dumps(value, sort_keys=True)),
            )
        for key, topic, page in self.pages:
            self.connection.execute(
                """INSERT INTO semantic_scholar_page_dispositions
                   (scout_run_id,query_key,topic,page_json) VALUES (?,?,?,?)""",
                (scout_run_id, key, topic, json.dumps(page)),
            )


def semantic_scholar_disposition(paper, *, cutoff, seen_keys):
    candidate = semantic_scholar_paper_to_candidate(paper)
    if not candidate.title or not candidate.source_id:
        return "missing_title_or_source_id", None
    if candidate.published:
        try:
            if date.fromisoformat(candidate.published[:10]) < date.fromisoformat(cutoff):
                return "too_old", None
        except ValueError:
            try:
                if int(candidate.published[:4]) < int(cutoff[:4]):
                    return "too_old", None
            except ValueError:
                return "invalid_payload", None
    canonical_key = db.canonical_key_for_candidate(candidate.as_dict())
    if canonical_key in seen_keys:
        return "duplicate", None
    return "accepted", candidate


def cooldown_seconds(value, now):
    try:
        delay = float(value)
        if not 0 <= delay < float("inf"):
            raise ValueError
        return delay
    except (ValueError, TypeError):
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0, (parsed - now).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 60 + random.uniform(0, 30)


def transient_retry_seconds(value, now, *, fallback):
    """Honor a valid Retry-After while never retrying faster than configured."""
    try:
        delay = float(value)
        if not 0 <= delay < float("inf"):
            raise ValueError
    except (ValueError, TypeError):
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            delay = max(0, (parsed - now).total_seconds())
        except (ValueError, TypeError, OverflowError):
            delay = fallback
    return max(fallback, delay)


def transient_url_error(error):
    """Return true only for transport failures that are useful to retry once."""
    reason = error.reason
    if isinstance(reason, ssl.SSLCertVerificationError):
        return False
    if isinstance(reason, (TimeoutError, ConnectionResetError, ConnectionAbortedError,
                           BrokenPipeError)):
        return True
    if isinstance(reason, socket.gaierror):
        return reason.errno == socket.EAI_AGAIN
    if isinstance(reason, OSError):
        return reason.errno in {
            errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE,
            errno.EHOSTUNREACH, errno.ENETUNREACH,
        }
    if isinstance(reason, str):
        normalized = reason.casefold()
        return any(marker in normalized for marker in (
            "timed out",
            "temporary failure in name resolution",
            "connection reset by peer",
        ))
    return False


def sanitized_provider_error(error, *, api_key=None, api_keys=()):
    """Return bounded HTTP diagnostics without request headers or credentials."""
    secrets = [value for value in api_keys if isinstance(value, str) and value]
    if api_key and api_key not in secrets:
        secrets.append(api_key)

    def redact(value):
        result = str(value)
        for secret in secrets:
            result = result.replace(secret, "[REDACTED]")
        return result

    headers = {
        name.lower(): redact(value)[:256]
        for name, value in (error.headers.items() if error.headers else [])
        if name.lower() in PROVIDER_ERROR_HEADERS
    }
    raw = error.read(PROVIDER_ERROR_BODY_READ_LIMIT + 1)
    truncated = len(raw) > PROVIDER_ERROR_BODY_READ_LIMIT
    text = raw[:PROVIDER_ERROR_BODY_READ_LIMIT].decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        body = " ".join(text.split())
    else:
        parsed = safe_provider_payload(parsed)
        body = json.dumps(parsed, ensure_ascii=True, separators=(",", ":"))
    body = redact(body)
    body = re.sub(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+", "Bearer [REDACTED]", body)
    body = re.sub(
        r'''(?i)\b(x-api-key|api[_ -]?key|authorization)\b\s*[:=]\s*["']?[^\s,}"']+''',
        r"\1=[REDACTED]",
        body,
    )
    if len(body) > PROVIDER_ERROR_BODY_REPORT_LIMIT:
        body = body[:PROVIDER_ERROR_BODY_REPORT_LIMIT]
        truncated = True
    return {
        "http_status": error.code,
        "reason": redact(error.reason)[:256],
        "headers": headers,
        "body": body,
        "body_truncated": truncated,
    }


def safe_provider_payload(value):
    if isinstance(value, dict):
        return {
            str(key): safe_provider_payload(item)
            for key, item in value.items()
            if str(key).lower() in SAFE_PROVIDER_ERROR_FIELDS
        }
    if isinstance(value, list):
        return [safe_provider_payload(item) for item in value[:10]]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)

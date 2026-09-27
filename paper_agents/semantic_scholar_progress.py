"""Bounded, resumable Semantic Scholar normal-search retrieval (opt-in)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import random
import re
import time
import urllib.error

from paper_agents import db
from paper_agents.scout import dedupe_candidates, semantic_scholar_paper_to_candidate


ATTEMPT_BUDGET = 3
PAGE_SIZE = 10
REFRESH_SECONDS = 7 * 86400
TRAVERSAL_SECONDS = 30 * 86400
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
    def __init__(self, connection, source, *, now=None):
        self.connection = connection
        self.source = source
        self.now = now or datetime.now(timezone.utc)
        self.original = dict(connection.execute(
            "SELECT key, value_json FROM semantic_scholar_search_state"
        ))
        self.states = {key: json.loads(value) for key, value in self.original.items()}
        self.pages = []
        self.diagnostics = {
            "mode": "offset_v1",
            "endpoint": source.api_url,
            "attempt_budget": ATTEMPT_BUDGET,
            "budget_scope": "metadata_search_http_attempts",
            "attempts": 0,
            "pages": [],
            "stop_reason": "request_budget_reached",
        }

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
        for key, topic in queries:
            if self.diagnostics["attempts"] >= ATTEMPT_BUDGET:
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
            if self.diagnostics["attempts"]:
                time.sleep(self.source.request_delay)
            self.diagnostics["attempts"] += 1
            attempted.append(topic)
            control["sequence"] += 1
            state["last_served"] = control["sequence"]
            try:
                payload = self.source.fetch_page(
                    topic,
                    offset=traversal["offset"],
                    cutoff=traversal["cutoff"],
                    page_size=min(PAGE_SIZE, remaining),
                )
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
                self.pages.append((key, topic, page))
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
                if refresh:
                    # A freshness sample must not erase or starve the deeper checkpoint.
                    state["last_refresh"] = clock
                else:
                    deep["offset"] = payload["next"]
                    deep["exhausted"] = payload["next"] is None
            except urllib.error.HTTPError as error:
                provider_error = sanitized_provider_error(error, api_key=self.source.api_key)
                if error.code == 429:
                    delay = cooldown_seconds(
                        error.headers.get("Retry-After") if error.headers else None, self.now
                    )
                    control["not_before"] = clock + delay
                    self._persist_cooldown(control["not_before"])
                    self.diagnostics.update(
                        stop_reason="source_cooldown",
                        not_before=control["not_before"],
                        provider_error=provider_error,
                    )
                    break
                errors.append(f"Semantic Scholar HTTP {error.code}: {topic}")
                self.diagnostics.update(
                    stop_reason="source_error", provider_error=provider_error
                )
                break
            except (OSError, ValueError, KeyError, TypeError) as error:
                errors.append(f"Semantic Scholar page failed ({type(error).__name__}): {topic}")
                self.diagnostics["stop_reason"] = "source_error"
                break

        if not attempted:
            self.diagnostics["stop_reason"] = "traversals_exhausted_until_refresh"
        elif len(attempted) == len(queries) and self.diagnostics["stop_reason"] == "request_budget_reached":
            self.diagnostics["stop_reason"] = "query_round_complete"
        self.diagnostics.update(
            deferred_queries=[topic for _, topic in queries if topic not in attempted],
            unique_count=len(candidates),
        )
        return candidates

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


def sanitized_provider_error(error, *, api_key=None):
    """Return bounded HTTP diagnostics without request headers or credentials."""
    headers = {
        name.lower(): str(value)[:256]
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
    if api_key:
        body = body.replace(api_key, "[REDACTED]")
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
        "reason": str(error.reason)[:256],
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

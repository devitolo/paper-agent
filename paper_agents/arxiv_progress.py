"""Bounded, resumable arXiv offset retrieval."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import random
import time
import urllib.error

from paper_agents import db
from paper_agents.scout import arxiv_entry_to_candidate, dedupe_candidates


ATTEMPT_BUDGET = 3
PAGE_SIZE = 10
REFRESH_SECONDS = 7 * 86400
TRAVERSAL_SECONDS = 30 * 86400


class ArxivProgress:
    def __init__(self, connection, source, *, now=None):
        self.connection = connection
        self.source = source
        self.now = now or datetime.now(timezone.utc)
        self.original = dict(connection.execute("SELECT key, value_json FROM arxiv_search_state"))
        self.states = {key: json.loads(value) for key, value in self.original.items()}
        self.pages = []
        self.diagnostics = {
            "mode": "offset_v1",
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
            raise RuntimeError("arXiv HTTP must start outside a write transaction")
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
            contract = [1, "arxiv", topic.strip(), freshness_months, PAGE_SIZE]
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
                    page_size=min(PAGE_SIZE, remaining),
                )
                if payload["next"] == traversal["offset"]:
                    raise ValueError("arXiv continuation did not advance")
                dispositions = []
                accepted = []
                for entry in payload["entries"]:
                    reason, candidate = arxiv_disposition(
                        entry, cutoff=traversal["cutoff"], seen_keys=seen_keys
                    )
                    dispositions.append({
                        "id": candidate.source_id if candidate else None,
                        "reason": reason,
                    })
                    if reason == "accepted":
                        candidate.metadata["query_topic"] = topic
                        seen_keys.add(db.canonical_key_for_candidate(candidate.as_dict()))
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
                    "duplicate_count": sum(item["reason"] == "duplicate" for item in dispositions),
                    "rejected_count": len(dispositions) - len(accepted),
                    "provider_end": payload["next"] is None,
                })
                if refresh:
                    state["last_refresh"] = clock
                else:
                    deep["offset"] = payload["next"]
                    deep["exhausted"] = payload["next"] is None
            except urllib.error.HTTPError as error:
                if error.code == 429:
                    delay = cooldown_seconds(
                        error.headers.get("Retry-After") if error.headers else None, self.now
                    )
                    control["not_before"] = clock + delay
                    self._persist_cooldown(control["not_before"])
                    self.diagnostics.update(
                        stop_reason="source_cooldown", not_before=control["not_before"]
                    )
                    break
                errors.append(f"arXiv HTTP {error.code}: {topic}")
                self.diagnostics["stop_reason"] = "source_error"
                break
            except (OSError, ValueError, KeyError, TypeError) as error:
                errors.append(f"arXiv page failed ({type(error).__name__}): {topic}")
                self.diagnostics["stop_reason"] = "source_error"
                break

        if not attempted:
            self.diagnostics["stop_reason"] = "traversals_exhausted_until_refresh"
        elif len(attempted) == len(queries) and self.diagnostics["stop_reason"] == "request_budget_reached":
            self.diagnostics["stop_reason"] = "query_round_complete"
        self.diagnostics.update(
            deferred_queries=[topic for _, topic in queries if topic not in attempted],
            unique_count=len(candidates),
            coverage_mode="single_result_406_fallback" if self.source.single_result_mode else "normal",
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
                "SELECT value_json FROM arxiv_search_state WHERE key='@source'"
            ).fetchone()
            if (actual[0] if actual else None) != old:
                raise RuntimeError("Concurrent arXiv retrieval changed state; retry later")
            self.connection.execute(
                "INSERT OR REPLACE INTO arxiv_search_state VALUES (?, ?)",
                ("@source", encoded),
            )
        self.original["@source"] = encoded

    def checkpoint(self, scout_run_id):
        actual = dict(self.connection.execute("SELECT key, value_json FROM arxiv_search_state"))
        if actual != self.original:
            raise RuntimeError("Concurrent arXiv retrieval changed state; retry later")
        for key, value in self.states.items():
            self.connection.execute(
                "INSERT OR REPLACE INTO arxiv_search_state VALUES (?, ?)",
                (key, json.dumps(value, sort_keys=True)),
            )
        for key, topic, page in self.pages:
            self.connection.execute(
                """INSERT INTO arxiv_page_dispositions
                   (scout_run_id,query_key,topic,page_json) VALUES (?,?,?,?)""",
                (scout_run_id, key, topic, json.dumps(page)),
            )


def arxiv_disposition(entry, *, cutoff, seen_keys):
    candidate = arxiv_entry_to_candidate(entry)
    if not candidate.title or not candidate.source_id:
        return "missing_title_or_source_id", candidate
    if candidate.published:
        try:
            if date.fromisoformat(candidate.published[:10]) < date.fromisoformat(cutoff):
                return "too_old", candidate
        except ValueError:
            return "invalid_payload", candidate
    canonical_key = db.canonical_key_for_candidate(candidate.as_dict())
    if canonical_key in seen_keys:
        return "duplicate", candidate
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

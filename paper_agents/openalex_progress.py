"""Bounded, resumable OpenAlex metadata retrieval (opt-in).

HTTP runs outside write transactions. Whole-page dispositions and checkpoints
are committed with Scout candidates, never with a truncated selection.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import random
import time
import urllib.error

from paper_agents import db
from paper_agents.scout import (
    dedupe_candidates, openalex_rejection_reason, openalex_search_query,
    openalex_work_to_candidate,
)

ATTEMPT_BUDGET = 3
PAGE_SIZE = 10
REFRESH_SECONDS = 7 * 86400
TRAVERSAL_SECONDS = 30 * 86400


class OpenAlexProgress:
    def __init__(self, connection, source, *, now=None):
        self.connection = connection
        self.source = source
        self.now = now or datetime.now(timezone.utc)
        self.original = dict(connection.execute('SELECT key, value_json FROM openalex_search_state'))
        self.states = {key: json.loads(value) for key, value in self.original.items()}
        self.pages = []
        self.diagnostics = {"mode": "cursor_v1", "attempt_budget": ATTEMPT_BUDGET,
                            "budget_scope": "metadata_search_http_attempts", "attempts": 0,
                            "pages": [], "stop_reason": "request_budget_reached"}

    def _new_traversal(self, months):
        return {"cursor": "*", "cutoff": (self.now.date() - timedelta(days=months * 31)).isoformat(),
                "through": self.now.date().isoformat(), "started": self.now.timestamp(),
                "exhausted": False}

    def fetch(self, topics, *, freshness_months, max_candidates, errors):
        if self.connection.in_transaction:
            raise RuntimeError("OpenAlex HTTP must start outside a write transaction")
        clock = self.now.timestamp()
        control = self.states.setdefault('@source', {"not_before": 0, "sequence": 0})
        if control['not_before'] > clock:
            self.diagnostics.update(stop_reason="source_cooldown", not_before=control['not_before'],
                                    deferred_queries=list(topics))
            return []
        queries = []
        query_keys = set()
        for topic in dict.fromkeys(t for t in topics if t.strip()):
            contract = [1, self.source.api_url, openalex_search_query(topic), freshness_months,
                        'publication_date:desc', 'article|preprint|posted-content|report', PAGE_SIZE]
            key = hashlib.sha256(json.dumps(contract).encode()).hexdigest()
            if key in query_keys:
                continue
            query_keys.add(key)
            if key not in self.states:
                self.states[key] = {"last_served": -1, "last_refresh": clock,
                                    "deep": self._new_traversal(freshness_months)}
            queries.append((key, topic))
        # One attempt per query per run: no noisy query monopolizes the budget.
        queries.sort(key=lambda pair: self.states[pair[0]]['last_served'])
        known_keys = {row[0] for row in self.connection.execute('SELECT canonical_key FROM papers')}
        candidates = []
        attempted = []
        for key, topic in queries:
            if self.diagnostics['attempts'] >= ATTEMPT_BUDGET:
                break
            remaining = max_candidates - len(candidates)
            if remaining <= 0:
                self.diagnostics['stop_reason'] = 'candidate_budget_reached'
                break
            state = self.states[key]
            deep = state['deep']
            if clock - deep['started'] >= TRAVERSAL_SECONDS:
                state['deep'] = deep = self._new_traversal(freshness_months)
                state['last_refresh'] = clock
            refresh = clock - state['last_refresh'] >= REFRESH_SECONDS
            if deep['exhausted'] and not refresh:
                continue
            traversal = self._new_traversal(freshness_months) if refresh else deep
            if self.diagnostics['attempts']:
                time.sleep(self.source.request_delay)
            self.diagnostics['attempts'] += 1
            attempted.append(topic)
            control['sequence'] += 1
            state['last_served'] = control['sequence']
            try:
                payload = self.source.fetch_page(topic, cursor=traversal['cursor'],
                    cutoff=traversal['cutoff'], through=traversal['through'],
                    page_size=min(PAGE_SIZE, remaining))
                if payload['meta']['next_cursor'] == traversal['cursor']:
                    raise ValueError('OpenAlex continuation did not advance')
                dispositions = []
                accepted = []
                for work in payload['results']:
                    reason = openalex_rejection_reason(work)
                    candidate = None
                    if not reason:
                        candidate = openalex_work_to_candidate(work)
                        if not candidate.title or not candidate.source_id:
                            reason = 'missing_title_or_source_id'
                    dispositions.append({'id': work.get('id'), 'reason': reason or 'candidate',
                                         'work': work})
                    if not reason:
                        candidate.metadata['query_topic'] = topic
                        accepted.append(candidate)
                # Keep membership before cross-query dedupe; every rejected work is recorded too.
                page = {'cursor': traversal['cursor'], 'next_cursor': payload['meta']['next_cursor'],
                        'cutoff': traversal['cutoff'], 'through': traversal['through'],
                        'kind': 'freshness' if refresh else 'continuation',
                        'dispositions': dispositions}
                self.pages.append((key, topic, page))
                candidates = dedupe_candidates([*candidates, *accepted])
                self.diagnostics['pages'].append({'topic': topic, 'kind': page['kind'],
                    'raw_count': len(dispositions), 'accepted_count': len(accepted),
                    'rejected_count': len(dispositions) - len(accepted),
                    'unique_returned_ids': len({str(item['id']) for item in dispositions}),
                    'previously_unseen_count': len({db.canonical_key_for_candidate(c.as_dict())
                        for c in accepted} - known_keys),
                    'provider_end': payload['meta']['next_cursor'] is None})
                if refresh:
                    # Refresh does not overwrite or starve the deep checkpoint.
                    state['last_refresh'] = clock
                else:
                    deep['cursor'] = payload['meta']['next_cursor']
                    deep['exhausted'] = deep['cursor'] is None
            except urllib.error.HTTPError as error:
                if error.code == 429:
                    delay = cooldown_seconds(error.headers.get('Retry-After') if error.headers else None, self.now)
                    control['not_before'] = clock + delay
                    # Persist the cooldown even if subsequent candidate storage fails.
                    self._persist_cooldown(control['not_before'])
                    self.diagnostics.update(stop_reason='source_cooldown', not_before=control['not_before'])
                    break
                if error.code == 400 and traversal['cursor'] != '*':
                    # Bounded recovery; no immediate retry and no claim this was definitely a bad token.
                    state['deep'] = self._new_traversal(freshness_months)
                    errors.append(f'OpenAlex HTTP 400 for continuation: {topic}; traversal reset for later run')
                else:
                    errors.append(f'OpenAlex HTTP {error.code}: {topic}')
                self.diagnostics['stop_reason'] = 'source_error'
                break
            except (OSError, ValueError, KeyError, TypeError) as error:
                errors.append(f'OpenAlex page failed ({type(error).__name__}): {topic}')
                self.diagnostics['stop_reason'] = 'source_error'
                break
        if not attempted:
            self.diagnostics['stop_reason'] = 'traversals_exhausted_until_refresh'
        elif len(attempted) == len(queries) and self.diagnostics['stop_reason'] == 'request_budget_reached':
            self.diagnostics['stop_reason'] = 'query_round_complete'
        self.diagnostics.update(deferred_queries=[topic for _, topic in queries if topic not in attempted],
                                unique_count=len(candidates))
        return candidates

    def _persist_cooldown(self, not_before):
        old = self.original.get('@source')
        current = json.loads(old) if old else {'sequence': 0, 'not_before': 0}
        current['not_before'] = max(current['not_before'], not_before)
        encoded = json.dumps(current, sort_keys=True)
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            actual = self.connection.execute("SELECT value_json FROM openalex_search_state WHERE key='@source'").fetchone()
            if (actual[0] if actual else None) != old:
                raise RuntimeError('Concurrent OpenAlex retrieval changed state; retry later')
            self.connection.execute('INSERT OR REPLACE INTO openalex_search_state VALUES (?, ?)', ('@source', encoded))
        self.original['@source'] = encoded

    def checkpoint(self, scout_run_id):
        """Caller owns transaction including all candidate writes; detect concurrent runs."""
        actual = dict(self.connection.execute('SELECT key, value_json FROM openalex_search_state'))
        if actual != self.original:
            raise RuntimeError('Concurrent OpenAlex retrieval changed state; retry later')
        for key, value in self.states.items():
            self.connection.execute('INSERT OR REPLACE INTO openalex_search_state VALUES (?, ?)',
                                    (key, json.dumps(value, sort_keys=True)))
        for key, topic, page in self.pages:
            self.connection.execute('INSERT INTO openalex_page_dispositions '
                '(scout_run_id,query_key,topic,page_json) VALUES (?,?,?,?)',
                (scout_run_id, key, topic, json.dumps(page)))


def cooldown_seconds(value, now):
    try:
        delay = float(value)
        if not 0 <= delay < float('inf'):
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

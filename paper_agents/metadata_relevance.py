"""Bounded offline comparison of keyword and local-Qwen metadata relevance."""
from __future__ import annotations

import copy
import json
import math
import signal
import threading
import time
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Any, Callable
from contextlib import contextmanager

from paper_agents.curator_scoring import evaluate_candidate
from paper_agents.local_extract import DEFAULT_MODEL, DEFAULT_OLLAMA_URL
from paper_agents.ranking_quality_replay import canonical_hash

VERSION = "metadata-relevance-v1"
MAX_PAPERS = 15
MAX_PROMPT_CHARS = 14_000
MAX_RESPONSE_BYTES = 65_536


@contextmanager
def _hard_deadline(seconds: float):
    """Interrupt local provider I/O after a total connect+read wall deadline."""
    if seconds <= 0:
        raise TimeoutError("provider deadline")
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        raise RuntimeError("hard provider deadline requires the main thread")
    previous_handler = signal.getsignal(signal.SIGALRM)

    def expired(signum, frame):
        raise TimeoutError("provider deadline")

    signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)


def allowed_profile(profile: dict[str, Any]) -> dict[str, list[str]]:
    return {key: [str(v)[:300] for v in (profile.get(key) or []) if isinstance(v, str)][:12]
            for key in ("interests", "positive_signals", "negative_signals")}


def judge_input(candidate: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "paper_id": str(candidate.get("id") or ""),
        "title": str(candidate.get("title") or "")[:1000],
        "abstract": str(candidate.get("abstract") or "")[:5000],
        "discovery_query": str(candidate.get("discovery_query") or "")[:1000] or None,
        "profile": allowed_profile(profile),
    }


def build_prompt(candidate: dict[str, Any], profile: dict[str, Any]) -> str:
    schema = {"score": "integer 0-100", "status": "complete|unknown",
              "rationale": "brief explanation grounded only in supplied metadata",
              "query_match": "strong|partial|weak|unknown",
              "negative_preference_applicability": "applies|does_not_apply|unknown"}
    prompt = (
        "Judge current personal topical relevance using only title, abstract, discovery query, and "
        "the supplied preference profile. Relevance is not rigor, evidence quality, or usefulness. "
        "Treat discovery query as context, not proof. Apply negative preferences only when the "
        "metadata supports them; otherwise use unknown. Missing or insufficient metadata requires "
        "status unknown. Return one JSON object matching: " + json.dumps(schema) + "\nInput:\n" +
        json.dumps(judge_input(candidate, profile), ensure_ascii=False)
    )
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError("bounded relevance prompt exceeded")
    return prompt


def call_qwen(url: str, model: str, prompt: str, timeout: int) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("Ollama endpoint must be local HTTP")
    request = urllib.request.Request(url, data=json.dumps({
        "model": model, "prompt": prompt, "format": "json", "stream": False,
        "options": {"temperature": 0, "num_predict": 250},
    }).encode(), headers={"Content-Type": "application/json"})
    class NoRedirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects)
    with _hard_deadline(float(timeout)):
        with opener.open(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("provider response exceeded limit")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("invalid provider envelope")
    return value


def parse_judgment(envelope: dict[str, Any]) -> dict[str, Any]:
    raw = envelope.get("response")
    value = json.loads(raw) if isinstance(raw, str) else None
    if not isinstance(value, dict):
        raise ValueError("invalid judgment")
    status = value.get("status")
    score = value.get("score")
    if status not in {"complete", "unknown"}:
        raise ValueError("invalid judgment status")
    if status == "complete" and (isinstance(score, bool) or not isinstance(score, int)
                                  or not 0 <= score <= 100):
        raise ValueError("invalid judgment score")
    if status == "unknown":
        score = None
    rationale = value.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 1000:
        raise ValueError("invalid judgment rationale")
    query_match = value.get("query_match")
    negative = value.get("negative_preference_applicability")
    if query_match not in {"strong", "partial", "weak", "unknown"}:
        raise ValueError("invalid query_match")
    if negative not in {"applies", "does_not_apply", "unknown"}:
        raise ValueError("invalid negative preference applicability")
    return {"status": status, "score": score, "rationale": rationale.strip(),
            "query_match": query_match, "negative_preference_applicability": negative}


def keyword_baseline(candidate: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    result = evaluate_candidate({"title": candidate.get("title"), "abstract": candidate.get("abstract")}, profile)
    c = result["score_components"]
    raw = max(0.0, c["topic"] + c["profile"] - c["negative_penalty"] - c["off_domain_penalty"])
    return {"raw_score": round(raw, 3), "topic": c["topic"], "profile": c["profile"],
            "negative_penalty": c["negative_penalty"], "off_domain_penalty": c["off_domain_penalty"]}


def run_experiment(fixture: dict[str, Any], *, provider: Callable = call_qwen,
                   model: str = DEFAULT_MODEL, url: str = DEFAULT_OLLAMA_URL,
                   timeout: int = 45, deadline_seconds: int = 750,
                   target_ids: set[str] | None = None,
                   checkpoint: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    candidates = fixture.get("candidates")
    profile = fixture.get("profile")
    if not isinstance(candidates, list) or not isinstance(profile, dict) or not candidates:
        raise ValueError("fixture requires candidates and profile")
    if len(candidates) > MAX_PAPERS or not 1 <= timeout <= 120:
        raise ValueError("experiment limits exceeded")
    target_ids = {str(value) for value in target_ids} if target_ids else None
    if timeout > 45 and (target_ids is None or len(target_ids) != 1):
        raise ValueError("timeouts above 45 seconds require exactly one target paper")
    candidate_id_list = [str(candidate.get("id")) for candidate in candidates]
    candidate_ids = set(candidate_id_list)
    if len(candidate_ids) != len(candidate_id_list):
        raise ValueError("candidate paper ids must be unique")
    if target_ids and not target_ids.issubset(candidate_ids):
        raise ValueError("every target paper id must exist in the fixture")
    mode = ("single_paper_feasibility" if target_ids and timeout > 45
            else "targeted_comparison" if target_ids else "comparison")
    output = {"version": VERSION, "status": "running", "model": model,
              "mode": mode,
              "target_ids": sorted(target_ids) if target_ids else None,
              "fixture_candidates_sha256": canonical_hash(candidates),
              "profile_sha256": canonical_hash(profile), "calls": 0, "results": []}
    started = time.monotonic()
    for candidate in candidates:
        if target_ids and str(candidate.get("id")) not in target_ids:
            continue
        row = {"id": str(candidate.get("id")), "title": candidate.get("title"),
               "keyword": keyword_baseline(candidate, profile),
               "qwen": {"status": "unknown", "score": None, "rationale": "not assessed"}}
        call_started = time.monotonic()
        try:
            if not str(candidate.get("title") or "").strip() or not str(candidate.get("abstract") or "").strip():
                raise ValueError("insufficient metadata")
            prompt = build_prompt(candidate, profile)
            remaining = deadline_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("overall deadline")
            allowed_timeout = min(float(timeout), remaining)
            output["calls"] += 1
            envelope = provider(url, model, prompt, allowed_timeout)
            judgment = parse_judgment(envelope)
            judgment["usage"] = {
                "prompt_eval_count": envelope.get("prompt_eval_count")
                if isinstance(envelope.get("prompt_eval_count"), int)
                and not isinstance(envelope.get("prompt_eval_count"), bool)
                and envelope.get("prompt_eval_count") >= 0 else None,
                "eval_count": envelope.get("eval_count")
                if isinstance(envelope.get("eval_count"), int)
                and not isinstance(envelope.get("eval_count"), bool)
                and envelope.get("eval_count") >= 0 else None,
            }
            row["qwen"] = judgment
        except Exception as exc:
            row["qwen"] = {"status": "unknown", "score": None,
                           "rationale": "local assessment failed",
                           "error_code": "timeout" if isinstance(exc, TimeoutError) else "invalid_or_unavailable"}
        row["qwen"]["latency_seconds"] = round(time.monotonic() - call_started, 3)
        # Labels are attached only after the label-free judgment is complete.
        row["human_label"] = {"decision": candidate.get("decision"), "score": candidate.get("user_score")}
        output["results"].append(row)
        if checkpoint:
            checkpoint(copy.deepcopy(output))
    for key, score_path in (("keyword_rank", lambda r: r["keyword"]["raw_score"]),
                            ("qwen_rank", lambda r: r["qwen"].get("score"))):
        ranked = sorted(output["results"], key=lambda r: (
            score_path(r) is None, -(score_path(r) if score_path(r) is not None else -math.inf), r["id"]))
        for rank, row in enumerate(ranked, 1):
            row[key] = rank if score_path(row) is not None else None
    output["status"] = "complete"
    output["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return output

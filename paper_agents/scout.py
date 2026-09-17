from __future__ import annotations

from paper_agents import telemetry

from email.utils import parsedate_to_datetime
import hashlib
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from socket import timeout as SocketTimeout
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from paper_agents.openai_helpers import call_openai_json
from paper_agents.pdf_links import candidate_pdf_urls, extract_pdf_links_from_html, looks_like_direct_pdf_url


ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}
DEFAULT_SCOUT_TOPICS = [
    "AIOps",
    "AI for IT operations",
    "LLM for operations",
    "agentic operations",
    "autonomous operations",
    "incident response",
    "incident management",
    "root cause analysis",
    "failure diagnosis",
    "anomaly detection",
    "remediation",
    "mitigation",
    "observability",
    "telemetry analysis",
    "log analysis",
    "trace analysis",
    "monitoring systems",
    "developer productivity",
    "software maintenance",
    "debugging",
    "automated debugging",
    "program repair",
    "software reliability engineering",
    "cloud operations",
    "microservice diagnosis",
    "distributed systems debugging",
    "production engineering",
]
DEFAULT_FETCH_LIMIT = 50
DEFAULT_KEEP_LIMIT = 5
DEFAULT_FRESHNESS_MONTHS = 24
DEFAULT_SCOUT_DIR = Path("data/scout")
DEFAULT_PDF_DIR = Path("data/papers")
DEFAULT_ARXIV_REQUEST_DELAY = 3.0
DEFAULT_ARXIV_RETRIES = 3
DEFAULT_ARXIV_TIMEOUT = 60
SCOUT_SOURCES = ("arxiv", "semantic_scholar", "openalex")


@dataclass
class PaperCandidate:
    title: str
    url: str
    publication_date: str
    abstract: str
    relevance_score: float | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "publication_date": self.publication_date,
            "abstract": self.abstract,
            "relevance_score": self.relevance_score,
            "reason": self.reason,
        }


@dataclass
class ScoutCandidate:
    source: str
    source_id: str
    title: str
    abstract: str
    authors: list[str]
    published: str
    updated: str | None
    url: str
    pdf_url: str | None
    doi: str | None = None
    arxiv_id: str | None = None
    categories: list[str] = field(default_factory=list)
    primary_category: str | None = None
    score: float = 0.0
    matched_keywords: list[str] = field(default_factory=list)
    ranking_reason: str | None = None
    selected: bool = False
    pdf_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class PaperSource(Protocol):
    name: str

    def fetch(self, topics: list[str], max_results: int, freshness_months: int) -> list[ScoutCandidate]:
        """Return normalized candidate records for one source."""


class ArxivSource:
    name = "arxiv"

    def __init__(
        self,
        request_delay: float = DEFAULT_ARXIV_REQUEST_DELAY,
        retries: int = DEFAULT_ARXIV_RETRIES,
        timeout: int = DEFAULT_ARXIV_TIMEOUT,
        verbose: bool = True,
    ):
        self.request_delay = request_delay
        self.retries = retries
        self.timeout = timeout
        self.verbose = verbose
        self.cooldown_active = False
        self.last_diagnostics: dict[str, Any] = {}

    def _record(self, event: str, **details: Any) -> None:
        record = {"at": datetime.now(timezone.utc).isoformat(), "event": event, **details}
        self.last_diagnostics.setdefault("requests", []).append(record)
        if self.verbose:
            print(f"{record['at']} arXiv {event}: {details}", flush=True)

    def fetch(self, topics: list[str], max_results: int, freshness_months: int) -> list[ScoutCandidate]:
        if self.cooldown_active:
            raise RuntimeError("arXiv source cooldown: requests stopped for the remainder of this run after exhausted HTTP 429 retries")
        self.last_diagnostics = {"requests": [], "successful_topics": 0, "failed_topics": 0,
                                 "empty_topics": 0, "skipped_topics": 0}
        terms = [topic for topic in topics if topic.strip()] or DEFAULT_SCOUT_TOPICS
        per_topic = max(1, min(10, (max_results + len(terms) - 1) // len(terms)))
        cutoff = date.today() - timedelta(days=freshness_months * 31)
        candidates: list[ScoutCandidate] = []
        errors: list[str] = []

        for index, topic in enumerate(terms):
            if index:
                time.sleep(self.request_delay)
            if self.verbose:
                print(f"fetching arXiv topic {index + 1}/{len(terms)}: {topic} ({per_topic} requested)")
            try:
                entries = self._fetch_topic(topic, per_topic)
            except (OSError, ET.ParseError) as error:
                errors.append(f"{topic}: {error}")
                self.last_diagnostics["failed_topics"] += 1
                if isinstance(error, urllib.error.HTTPError) and error.code == 429:
                    self.cooldown_active = True
                    self.last_diagnostics.update(cooldown_active=True, cooldown_scope="remainder_of_run",
                                                 skipped_topics=len(terms) - index - 1)
                    self._record("cooldown", topic=topic, reason="HTTP 429 retries exhausted")
                    break
                if self.verbose:
                    print(f"arXiv topic failed: {topic}: {error}")
                continue
            self.last_diagnostics["successful_topics"] += 1
            self.last_diagnostics["empty_topics"] += int(not entries)
            if self.verbose:
                print(f"received {len(entries)} arXiv entries for topic: {topic}")

            for entry in entries:
                candidate = arxiv_entry_to_candidate(entry)
                candidate.metadata["query_topic"] = topic
                if not candidate.title or not candidate.url:
                    continue
                if candidate.published:
                    try:
                        if date.fromisoformat(candidate.published) < cutoff:
                            continue
                    except ValueError:
                        pass
                candidates.append(candidate)

        if errors and self.verbose:
            print(f"arXiv partial failures: {len(errors)}/{len(terms)} topics failed")
        if not candidates and errors:
            raise RuntimeError(
                f"arXiv fetch returned 0 candidates: {self.last_diagnostics['failed_topics']} failed, "
                f"{self.last_diagnostics['successful_topics']} successful "
                f"({self.last_diagnostics['empty_topics']} empty), "
                f"{self.last_diagnostics['skipped_topics']} skipped topics. "
                + "; ".join(errors[:3])
            )
        return dedupe_candidates(candidates)[:max_results]

    def _fetch_topic(self, topic: str, max_results: int) -> list[ET.Element]:
        params = urllib.parse.urlencode(
            {
                "search_query": f'all:"{topic}"',
                "start": 0,
                "max_results": max_results,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
        )
        url = f"https://export.arxiv.org/api/query?{params}"
        request = urllib.request.Request(url, headers={"User-Agent": "paper-agent/0.1"})

        last_error: OSError | ET.ParseError | None = None
        for attempt in range(self.retries + 1):
            self._record("request", topic=topic, attempt=attempt + 1)
            try:
                with telemetry.span("source.http", "TOOL", source=self.name, attempt=attempt + 1), urllib.request.urlopen(request, timeout=self.timeout) as response:
                    root = ET.fromstring(response.read())
                entries = list(root.findall("atom:entry", ARXIV_NS))
                self._record("success", topic=topic, attempt=attempt + 1, entries=len(entries))
                return entries
            except urllib.error.HTTPError as error:
                last_error = error
                self._record("http_error", topic=topic, attempt=attempt + 1, status=error.code,
                             retry_after=error.headers.get("Retry-After") if error.headers else None)
                if error.code != 429 or attempt >= self.retries:
                    raise
                delay = self._retry_delay(attempt, retry_after=error.headers.get("Retry-After") if error.headers else None)
                from paper_agents.runtime_config import packaged
                if packaged():
                    # Allow the source to recover; keep native scheduling unchanged.
                    delay = max(delay, 60 * (2 ** attempt))
                if self.verbose:
                    print(f"arXiv rate limited topic '{topic}', retrying in {delay:.0f}s")
                self._record("retry", topic=topic, attempt=attempt + 1, delay_seconds=delay)
                telemetry.event("retry", attempt=attempt + 1, delay_seconds=delay)
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError, SocketTimeout) as error:
                self._record("network_error", topic=topic, attempt=attempt + 1, error=str(error))
                last_error = error
                if attempt >= self.retries:
                    raise
                delay = self._retry_delay(attempt)
                if self.verbose:
                    print(f"arXiv request failed for topic '{topic}' ({error}), retrying in {delay:.0f}s")
                self._record("retry", topic=topic, attempt=attempt + 1, delay_seconds=delay)
                telemetry.event("retry", attempt=attempt + 1, delay_seconds=delay)
                time.sleep(delay)
            except ET.ParseError as error:
                self._record("parse_error", topic=topic, attempt=attempt + 1, error=str(error))
                last_error = error
                if attempt >= self.retries:
                    raise
                delay = self._retry_delay(attempt)
                if self.verbose:
                    print(f"arXiv returned malformed XML for topic '{topic}', retrying in {delay:.0f}s")
                self._record("retry", topic=topic, attempt=attempt + 1, delay_seconds=delay)
                telemetry.event("retry", attempt=attempt + 1, delay_seconds=delay)
                time.sleep(delay)

        if last_error:
            raise last_error
        return []

    def _retry_delay(self, attempt: int, retry_after: str | None = None) -> float:
        if retry_after and retry_after.isdigit():
            return float(retry_after)
        if retry_after:
            try:
                return max(0, parsedate_to_datetime(retry_after).timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                pass
        return self.request_delay * (2 ** attempt)


class SemanticScholarSource:
    name = "semantic_scholar"
    api_url = "https://api.semanticscholar.org/graph/v1/paper/search"

    def __init__(
        self,
        request_delay: float = DEFAULT_ARXIV_REQUEST_DELAY,
        retries: int = DEFAULT_ARXIV_RETRIES,
        timeout: int = DEFAULT_ARXIV_TIMEOUT,
        verbose: bool = True,
        api_key: str | None = None,
    ):
        self.request_delay = request_delay
        self.retries = retries
        self.timeout = timeout
        self.verbose = verbose
        self.cooldown_active = False
        self.last_diagnostics: dict[str, Any] = {}
        self.api_key = api_key if api_key is not None else os.getenv("SEMANTIC_SCHOLAR_API_KEY")

    def _record(self, event: str, **details: Any) -> None:
        record = {"at": datetime.now(timezone.utc).isoformat(), "event": event, **details}
        self.last_diagnostics.setdefault("requests", []).append(record)
        if self.verbose:
            print(f"{record['at']} Semantic Scholar {event}: {details}", flush=True)

    def fetch(self, topics: list[str], max_results: int, freshness_months: int) -> list[ScoutCandidate]:
        if self.cooldown_active:
            raise RuntimeError("Semantic Scholar source cooldown: requests stopped for the remainder of this run after exhausted HTTP 429 retries")
        self.last_diagnostics = {"requests": [], "successful_topics": 0, "failed_topics": 0,
                                 "empty_topics": 0, "skipped_topics": 0}
        terms = [topic for topic in topics if topic.strip()] or DEFAULT_SCOUT_TOPICS
        per_topic = max(1, min(10, (max_results + len(terms) - 1) // len(terms)))
        cutoff_year = (date.today() - timedelta(days=freshness_months * 31)).year
        candidates: list[ScoutCandidate] = []
        errors: list[str] = []

        for index, topic in enumerate(terms):
            if index:
                time.sleep(self.request_delay)
            if self.verbose:
                print(f"fetching Semantic Scholar topic {index + 1}/{len(terms)}: {topic} ({per_topic} requested)")
            try:
                papers = self._fetch_topic(topic, per_topic)
            except OSError as error:
                errors.append(f"{topic}: {error}")
                self.last_diagnostics["failed_topics"] += 1
                if isinstance(error, urllib.error.HTTPError) and error.code == 429:
                    self.cooldown_active = True
                    self.last_diagnostics.update(cooldown_active=True, cooldown_scope="remainder_of_run",
                                                 skipped_topics=len(terms) - index - 1)
                    self._record("cooldown", topic=topic, reason="HTTP 429 retries exhausted")
                    break
                if self.verbose:
                    print(f"Semantic Scholar topic failed: {topic}: {error}")
                continue
            self.last_diagnostics["successful_topics"] += 1
            self.last_diagnostics["empty_topics"] += int(not papers)
            if self.verbose:
                print(f"received {len(papers)} Semantic Scholar entries for topic: {topic}")

            for paper in papers:
                candidate = semantic_scholar_paper_to_candidate(paper)
                candidate.metadata["query_topic"] = topic
                if not candidate.title or not candidate.source_id:
                    continue
                if candidate.published:
                    try:
                        if int(candidate.published[:4]) < cutoff_year:
                            continue
                    except ValueError:
                        pass
                candidates.append(candidate)

        if errors and self.verbose:
            print(f"Semantic Scholar partial failures: {len(errors)}/{len(terms)} topics failed")
        if not candidates and errors:
            raise RuntimeError(
                f"Semantic Scholar fetch returned 0 candidates: {self.last_diagnostics['failed_topics']} failed, "
                f"{self.last_diagnostics['successful_topics']} successful "
                f"({self.last_diagnostics['empty_topics']} empty), "
                f"{self.last_diagnostics['skipped_topics']} skipped topics. "
                + "; ".join(errors[:3])
            )
        return dedupe_candidates(candidates)[:max_results]

    def _fetch_topic(self, topic: str, max_results: int) -> list[dict[str, Any]]:
        params = urllib.parse.urlencode(
            {
                "query": topic,
                "limit": max_results,
                "fields": ",".join(
                    [
                        "paperId",
                        "title",
                        "abstract",
                        "authors",
                        "year",
                        "publicationDate",
                        "url",
                        "openAccessPdf",
                        "externalIds",
                        "fieldsOfStudy",
                        "publicationTypes",
                        "venue",
                    ]
                ),
            }
        )
        request = urllib.request.Request(f"{self.api_url}?{params}", headers=self._headers())

        last_error: OSError | None = None
        for attempt in range(self.retries + 1):
            self._record("request", topic=topic, attempt=attempt + 1)
            try:
                with telemetry.span("source.http", "TOOL", source=self.name, attempt=attempt + 1), urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                papers = payload.get("data") or []
                papers = [paper for paper in papers if isinstance(paper, dict)]
                self._record("success", topic=topic, attempt=attempt + 1, entries=len(papers))
                return papers
            except urllib.error.HTTPError as error:
                last_error = error
                self._record("http_error", topic=topic, attempt=attempt + 1, status=error.code,
                             retry_after=error.headers.get("Retry-After") if error.headers else None)
                if error.code not in {429, 500, 502, 503, 504} or attempt >= self.retries:
                    raise
                delay = self._retry_delay(attempt, retry_after=error.headers.get("Retry-After") if error.headers else None)
                if self.verbose:
                    print(f"Semantic Scholar request failed for topic '{topic}' ({error.code}), retrying in {delay:.0f}s")
                self._record("retry", topic=topic, attempt=attempt + 1, delay_seconds=delay)
                telemetry.event("retry", attempt=attempt + 1, delay_seconds=delay)
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError, SocketTimeout, json.JSONDecodeError) as error:
                self._record("request_error", topic=topic, attempt=attempt + 1, error=str(error))
                last_error = error if isinstance(error, OSError) else OSError(str(error))
                if attempt >= self.retries:
                    raise last_error
                delay = self._retry_delay(attempt)
                if self.verbose:
                    print(f"Semantic Scholar request failed for topic '{topic}' ({error}), retrying in {delay:.0f}s")
                self._record("retry", topic=topic, attempt=attempt + 1, delay_seconds=delay)
                telemetry.event("retry", attempt=attempt + 1, delay_seconds=delay)
                time.sleep(delay)

        if last_error:
            raise last_error
        return []

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": "paper-agent/0.1"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    def _retry_delay(self, attempt: int, retry_after: str | None = None) -> float:
        if retry_after and retry_after.isdigit():
            return float(retry_after)
        if retry_after:
            try:
                return max(0, parsedate_to_datetime(retry_after).timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                pass
        return self.request_delay * (2 ** attempt)


class OpenAlexSource:
    name = "openalex"
    api_url = "https://api.openalex.org/works"

    def __init__(
        self,
        request_delay: float = DEFAULT_ARXIV_REQUEST_DELAY,
        retries: int = DEFAULT_ARXIV_RETRIES,
        timeout: int = DEFAULT_ARXIV_TIMEOUT,
        verbose: bool = True,
    ):
        self.request_delay = request_delay
        self.retries = retries
        self.timeout = timeout
        self.verbose = verbose
        self.last_diagnostics: dict[str, Any] = {}

    def fetch(self, topics: list[str], max_results: int, freshness_months: int) -> list[ScoutCandidate]:
        terms = [topic for topic in topics if topic.strip()] or DEFAULT_SCOUT_TOPICS
        per_topic = max(1, min(10, (max_results + len(terms) - 1) // len(terms)))
        cutoff = date.today() - timedelta(days=freshness_months * 31)
        candidates: list[ScoutCandidate] = []
        errors: list[str] = []
        raw_count = 0
        rejected_reasons: dict[str, int] = {}

        for index, topic in enumerate(terms):
            if index:
                time.sleep(self.request_delay)
            if self.verbose:
                print(f"fetching OpenAlex topic {index + 1}/{len(terms)}: {topic} ({per_topic} requested)")
            try:
                works = self._fetch_topic(topic, per_topic, cutoff)
            except OSError as error:
                errors.append(f"{topic}: {error}")
                if self.verbose:
                    print(f"OpenAlex topic failed: {topic}: {error}")
                continue
            if self.verbose:
                print(f"received {len(works)} OpenAlex entries for topic: {topic}")
            raw_count += len(works)

            for work in works:
                rejection_reason = openalex_rejection_reason(work)
                if rejection_reason:
                    rejected_reasons[rejection_reason] = rejected_reasons.get(rejection_reason, 0) + 1
                    continue
                candidate = openalex_work_to_candidate(work)
                candidate.metadata["query_topic"] = topic
                if not candidate.title or not candidate.source_id:
                    rejected_reasons["missing_title_or_source_id"] = rejected_reasons.get("missing_title_or_source_id", 0) + 1
                    continue
                candidates.append(candidate)

        self.last_diagnostics = {
            "raw_count": raw_count,
            "kept_count": len(candidates),
            "rejected_count": sum(rejected_reasons.values()),
            "rejected_reasons": rejected_reasons,
        }
        if self.verbose and rejected_reasons:
            reasons = ", ".join(f"{reason}={count}" for reason, count in sorted(rejected_reasons.items()))
            print(f"filtered {sum(rejected_reasons.values())} OpenAlex entries before storage: {reasons}")
        if errors and self.verbose:
            print(f"OpenAlex partial failures: {len(errors)}/{len(terms)} topics failed")
        if not candidates and errors:
            raise RuntimeError(
                "OpenAlex fetch returned 0 candidates because every topic failed: "
                + "; ".join(errors[:3])
            )
        return dedupe_candidates(candidates)[:max_results]

    def _fetch_topic(self, topic: str, max_results: int, cutoff: date) -> list[dict[str, Any]]:
        filters = [
            f"from_publication_date:{cutoff.isoformat()}",
            "type:article|preprint|posted-content|report",
        ]
        params = urllib.parse.urlencode(
            {
                "search": openalex_search_query(topic),
                "per_page": max_results,
                "sort": "publication_date:desc",
                "filter": ",".join(filters),
            }
        )
        request = urllib.request.Request(f"{self.api_url}?{params}", headers={"User-Agent": "paper-agent/0.1"})

        last_error: OSError | None = None
        for attempt in range(self.retries + 1):
            try:
                with telemetry.span("source.http", "TOOL", source=self.name, attempt=attempt + 1), urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                works = payload.get("results") or []
                return [work for work in works if isinstance(work, dict)]
            except urllib.error.HTTPError as error:
                last_error = OSError(openalex_http_error_message(error))
                if error.code not in {429, 500, 502, 503, 504} or attempt >= self.retries:
                    raise last_error
                delay = self._retry_delay(attempt, retry_after=error.headers.get("Retry-After"))
                if self.verbose:
                    print(f"OpenAlex request failed for topic '{topic}' ({last_error}), retrying in {delay:.0f}s")
                telemetry.event("retry", attempt=attempt + 1, delay_seconds=delay)
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError, SocketTimeout, json.JSONDecodeError) as error:
                last_error = error if isinstance(error, OSError) else OSError(str(error))
                if attempt >= self.retries:
                    raise last_error
                delay = self._retry_delay(attempt)
                if self.verbose:
                    print(f"OpenAlex request failed for topic '{topic}' ({error}), retrying in {delay:.0f}s")
                telemetry.event("retry", attempt=attempt + 1, delay_seconds=delay)
                time.sleep(delay)

        if last_error:
            raise last_error
        return []

    def _retry_delay(self, attempt: int, retry_after: str | None = None) -> float:
        if retry_after and retry_after.isdigit():
            return float(retry_after)
        return self.request_delay * (2 ** attempt)


def create_scout_source(
    source_name: str,
    *,
    request_delay: float = DEFAULT_ARXIV_REQUEST_DELAY,
    retries: int = DEFAULT_ARXIV_RETRIES,
    timeout: int = DEFAULT_ARXIV_TIMEOUT,
    verbose: bool = True,
) -> PaperSource:
    if source_name == "arxiv":
        return ArxivSource(request_delay=request_delay, retries=retries, timeout=timeout, verbose=verbose)
    if source_name == "semantic_scholar":
        return SemanticScholarSource(request_delay=request_delay, retries=retries, timeout=timeout, verbose=verbose)
    if source_name == "openalex":
        return OpenAlexSource(request_delay=request_delay, retries=retries, timeout=timeout, verbose=verbose)
    raise ValueError(f"Unsupported Scout source: {source_name}")


class ResearchScout:
    """Finds candidate papers and explains why they may match the profile."""

    def __init__(self, max_results: int = 10):
        self.max_results = max_results

    def run(self, profile: dict[str, Any]) -> list[dict[str, Any]]:
        raw_candidates = self._search_arxiv(profile)
        return self._score_candidates(profile, raw_candidates)

    def _search_arxiv(self, profile: dict[str, Any]) -> list[PaperCandidate]:
        query_terms = profile.get("interests", [])[:8]
        query = " OR ".join(f'all:"{term}"' for term in query_terms)
        params = urllib.parse.urlencode(
            {
                "search_query": query,
                "start": 0,
                "max_results": self.max_results,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
        )
        url = f"https://export.arxiv.org/api/query?{params}"

        with urllib.request.urlopen(url, timeout=30) as response:
            root = ET.fromstring(response.read())

        candidates: list[PaperCandidate] = []
        for entry in root.findall("atom:entry", ARXIV_NS):
            title = _clean(entry.findtext("atom:title", default="", namespaces=ARXIV_NS))
            abstract = _clean(entry.findtext("atom:summary", default="", namespaces=ARXIV_NS))
            published = entry.findtext("atom:published", default="", namespaces=ARXIV_NS)[:10]
            link = _paper_url(entry)
            if title and link:
                candidates.append(PaperCandidate(title, link, published, abstract))
        return candidates

    def _score_candidates(
        self, profile: dict[str, Any], candidates: list[PaperCandidate]
    ) -> list[dict[str, Any]]:
        system_prompt = """
You are Agent 1: Research Scout.
Score each candidate paper for this user's current interests.
Return only valid JSON in this shape:
{
  "candidates": [
    {
      "title": "...",
      "url": "...",
      "publication_date": "YYYY-MM-DD",
      "abstract": "...",
      "relevance_score": 0.0,
      "reason": "brief reason"
    }
  ]
}
Keep 5-8 candidates. Scores are 0 to 10. Prefer practical papers connected to SRE,
AIOps, observability, incident management, debugging, infrastructure automation,
or AI-assisted software engineering.
""".strip()
        payload = {
            "profile": profile,
            "candidates": [candidate.as_dict() for candidate in candidates],
        }
        result = call_openai_json(system_prompt, payload)
        return result.get("candidates", [])


def run_daily_scout(
    *,
    topics: list[str] | None = None,
    source: PaperSource | None = None,
    freshness_months: int = DEFAULT_FRESHNESS_MONTHS,
    fetch_limit: int = DEFAULT_FETCH_LIMIT,
    keep_limit: int = DEFAULT_KEEP_LIMIT,
    scout_dir: Path = DEFAULT_SCOUT_DIR,
    pdf_dir: Path = DEFAULT_PDF_DIR,
    run_date: date | None = None,
    download_pdfs: bool = True,
    request_delay: float = DEFAULT_ARXIV_REQUEST_DELAY,
    retries: int = DEFAULT_ARXIV_RETRIES,
    timeout: int = DEFAULT_ARXIV_TIMEOUT,
    seen_source_ids: set[str] | None = None,
    include_seen: bool = False,
    guidance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the deterministic daily scout MVP and return a run report."""
    topics = list(DEFAULT_SCOUT_TOPICS if topics is None else (topic for topic in topics if topic.strip()))
    run_date = run_date or date.today()
    source_name = source.name if source is not None else "arxiv"

    if not topics:
        return {
            "source": source_name,
            "run_date": run_date.isoformat(),
            "freshness_months": freshness_months,
            "fetched_count": 0,
            "candidate_count": 0,
            "stored_count": 0,
            "seen_filtered_count": 0,
            "guidance": guidance or {},
            "output_path": None,
            "candidates": [],
            "skipped_reason": "no_eligible_configured_topics",
        }

    source = source or ArxivSource(request_delay=request_delay, retries=retries, timeout=timeout)

    print(
        f"scout run: source={source.name} topics={len(topics)} "
        f"fetch_limit={fetch_limit} keep={keep_limit} freshness_months={freshness_months} "
        f"request_delay={request_delay}s retries={retries} timeout={timeout}s"
    )
    if guidance:
        print(
            "Scout guidance: "
            f"boost={guidance.get('boost_terms', [])[:5]} "
            f"avoid={guidance.get('avoid_terms', [])[:5]} "
            f"feedback_count={guidance.get('feedback_count', 0)}"
        )
    fetched = source.fetch(topics, max_results=fetch_limit, freshness_months=freshness_months)
    print(f"fetched {len(fetched)} raw candidates")
    candidates = dedupe_candidates(fetched)
    print(f"deduped to {len(candidates)} candidates")
    seen_source_ids = seen_source_ids or set()
    selectable = filter_seen_candidates(candidates, seen_source_ids, include_seen=include_seen)
    filtered_seen_count = len(candidates) - len(selectable)
    if filtered_seen_count:
        print(f"marked {filtered_seen_count} previously seen candidates as excluded")

    output_path = scout_dir / f"{run_date.isoformat()}.jsonl"
    write_candidates_jsonl(candidates, output_path)
    print(f"wrote scout metadata: {output_path}")

    return {
        "source": source.name,
        "run_date": run_date.isoformat(),
        "freshness_months": freshness_months,
        "fetched_count": len(fetched),
        "candidate_count": len(candidates),
        "stored_count": len(candidates),
        "seen_filtered_count": filtered_seen_count,
        "guidance": guidance or {},
        "output_path": str(output_path),
        "candidates": [scout_candidate_record(candidate) for candidate in candidates],
    }


def filter_seen_candidates(
    candidates: list[ScoutCandidate],
    seen_source_ids: set[str],
    *,
    include_seen: bool = False,
) -> list[ScoutCandidate]:
    if include_seen or not seen_source_ids:
        return candidates

    selectable: list[ScoutCandidate] = []
    for candidate in candidates:
        candidate.metadata["seen_before"] = candidate.source_id in seen_source_ids
        if candidate.metadata["seen_before"]:
            continue
        selectable.append(candidate)
    return selectable


def build_arxiv_query(topics: list[str]) -> str:
    terms = [topic for topic in topics if topic.strip()]
    if not terms:
        terms = DEFAULT_SCOUT_TOPICS
    return " OR ".join(f'all:"{term}"' for term in terms[:12])


def arxiv_entry_to_candidate(entry: ET.Element) -> ScoutCandidate:
    title = _clean(entry.findtext("atom:title", default="", namespaces=ARXIV_NS))
    abstract = _clean(entry.findtext("atom:summary", default="", namespaces=ARXIV_NS))
    published = entry.findtext("atom:published", default="", namespaces=ARXIV_NS)[:10]
    updated = entry.findtext("atom:updated", default="", namespaces=ARXIV_NS)[:10] or None
    url = _paper_url(entry)
    pdf_url = _pdf_url(entry)
    source_id = arxiv_id_from_url(url)
    authors = [
        _clean(author.findtext("atom:name", default="", namespaces=ARXIV_NS))
        for author in entry.findall("atom:author", ARXIV_NS)
    ]
    categories = [category.attrib.get("term", "") for category in entry.findall("atom:category", ARXIV_NS)]
    categories = [category for category in categories if category]
    primary = entry.find("atom:category", ARXIV_NS)
    primary_category = primary.attrib.get("term") if primary is not None else None

    return ScoutCandidate(
        source="arxiv",
        source_id=source_id,
        title=title,
        abstract=abstract,
        authors=[author for author in authors if author],
        published=published,
        updated=updated,
        url=url,
        pdf_url=pdf_url,
        categories=categories,
        primary_category=primary_category,
    )


def semantic_scholar_paper_to_candidate(paper: dict[str, Any]) -> ScoutCandidate:
    external_ids = paper.get("externalIds") if isinstance(paper.get("externalIds"), dict) else {}
    open_access_pdf = paper.get("openAccessPdf") if isinstance(paper.get("openAccessPdf"), dict) else {}
    paper_id = str(paper.get("paperId") or "").strip()
    title = _clean(str(paper.get("title") or ""))
    abstract = _clean(str(paper.get("abstract") or ""))
    authors = [
        _clean(str(author.get("name") or ""))
        for author in paper.get("authors") or []
        if isinstance(author, dict)
    ]
    publication_date = str(paper.get("publicationDate") or "").strip()
    year = paper.get("year")
    published = publication_date or (str(year) if year else "")
    url = str(paper.get("url") or "").strip()
    if not url and paper_id:
        url = f"https://www.semanticscholar.org/paper/{paper_id}"
    fields = paper.get("fieldsOfStudy") or []
    categories = [str(field).strip() for field in fields if str(field).strip()]
    publication_types = paper.get("publicationTypes") or []
    pdf_url = str(open_access_pdf.get("url") or "").strip() or None
    doi = external_ids.get("DOI") or external_ids.get("Doi")
    arxiv_id = external_ids.get("ArXiv") or external_ids.get("ARXIV") or external_ids.get("arXiv")

    metadata = {
        "external_ids": external_ids,
        "venue": paper.get("venue"),
        "publication_types": publication_types,
    }

    return ScoutCandidate(
        source="semantic_scholar",
        source_id=paper_id,
        title=title,
        abstract=abstract,
        authors=[author for author in authors if author],
        published=published,
        updated=None,
        url=url,
        pdf_url=pdf_url,
        doi=str(doi).strip() if doi else None,
        arxiv_id=str(arxiv_id).strip() if arxiv_id else None,
        categories=categories,
        primary_category=categories[0] if categories else None,
        metadata=metadata,
    )


def openalex_work_to_candidate(work: dict[str, Any]) -> ScoutCandidate:
    work_id = openalex_work_id(work.get("id"))
    title = _clean(str(work.get("title") or work.get("display_name") or ""))
    abstract = _clean(reconstruct_openalex_abstract(work.get("abstract_inverted_index")))
    authors = openalex_authors(work.get("authorships"))
    concepts = openalex_named_items(work.get("concepts"))
    keywords = openalex_named_items(work.get("keywords"))
    categories = concepts + [keyword for keyword in keywords if keyword not in concepts]
    publication_date = str(work.get("publication_date") or "").strip()
    year = work.get("publication_year")
    published = publication_date or (str(year) if year else "")
    primary_location = work.get("primary_location") if isinstance(work.get("primary_location"), dict) else {}
    best_oa_location = work.get("best_oa_location") if isinstance(work.get("best_oa_location"), dict) else {}
    open_access = work.get("open_access") if isinstance(work.get("open_access"), dict) else {}
    source = primary_location.get("source") if isinstance(primary_location.get("source"), dict) else {}
    url = (
        str(primary_location.get("landing_page_url") or "").strip()
        or str(work.get("doi") or "").strip()
        or str(work.get("id") or "").strip()
    )
    pdf_url = (
        str(primary_location.get("pdf_url") or "").strip()
        or str(best_oa_location.get("pdf_url") or "").strip()
        or str(open_access.get("oa_url") or "").strip()
        or None
    )
    primary_topic = work.get("primary_topic") if isinstance(work.get("primary_topic"), dict) else {}
    metadata = {
        "source_metadata": {
            "openalex_id": work.get("id"),
            "primary_location": primary_location,
            "best_oa_location": best_oa_location,
            "open_access": open_access,
            "source": source,
            "primary_topic": primary_topic,
            "locations": work.get("locations") or [],
        },
        "concepts": concepts,
        "keywords": keywords,
        "venue": source.get("display_name") if isinstance(source, dict) else None,
    }

    return ScoutCandidate(
        source="openalex",
        source_id=work_id,
        title=title,
        abstract=abstract,
        authors=authors,
        published=published,
        updated=None,
        url=url,
        pdf_url=pdf_url,
        doi=str(work.get("doi") or "").strip() or None,
        arxiv_id=None,
        categories=categories,
        primary_category=categories[0] if categories else None,
        metadata=metadata,
    )


OPENALEX_SEARCH_CONTEXT = ["software", "cloud", "operations", "observability"]
OPENALEX_ALLOWED_TYPES = {"article", "preprint", "posted-content", "report"}
OPENALEX_EXCLUDED_TYPES = {
    "book",
    "book-chapter",
    "book chapter",
    "reference-entry",
    "reference entry",
    "paratext",
    "editorial",
    "erratum",
}
OPENALEX_NOISE_TITLES = {"index", "contents", "front matter", "back matter"}
OPENALEX_BIOMEDICAL_TERMS = [
    "brain",
    "brain-computer",
    "parkinson",
    "deep brain stimulation",
    "biomedical",
    "clinical",
    "medical",
    "healthcare",
    "patient",
    "disease",
    "neural stimulation",
]


def openalex_search_query(topic: str) -> str:
    normalized_topic = _clean(topic)
    lower = normalize_text(normalized_topic)
    additions = [term for term in OPENALEX_SEARCH_CONTEXT if not count_phrase(lower, term)]
    return " ".join([normalized_topic, *additions]).strip()


def openalex_rejection_reason(work: dict[str, Any]) -> str | None:
    work_type = normalize_text(str(work.get("type") or work.get("type_crossref") or ""))
    if work_type in OPENALEX_EXCLUDED_TYPES:
        return f"excluded_type:{work_type.replace(' ', '_')}"
    if work_type and work_type not in OPENALEX_ALLOWED_TYPES:
        return f"unsupported_type:{work_type.replace(' ', '_')}"

    title = normalize_text(str(work.get("title") or work.get("display_name") or ""))
    if title in OPENALEX_NOISE_TITLES:
        return "noise_title"

    text = normalize_text(
        " ".join(
            [
                title,
                reconstruct_openalex_abstract(work.get("abstract_inverted_index")),
                " ".join(openalex_named_items(work.get("concepts"))),
                " ".join(openalex_named_items(work.get("keywords"))),
                str((work.get("primary_topic") or {}).get("display_name") if isinstance(work.get("primary_topic"), dict) else ""),
            ]
        )
    )
    biomedical_hits = [term for term in OPENALEX_BIOMEDICAL_TERMS if count_phrase(text, term)]
    context_hits = [term for term in DOMAIN_CONTEXT_TERMS if count_phrase(text, term)]
    if biomedical_hits and not context_hits:
        return "off_domain_biomedical"
    return None


def openalex_work_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.rstrip("/").rsplit("/", 1)[-1]


def reconstruct_openalex_abstract(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    positioned: list[tuple[int, str]] = []
    for token, positions in value.items():
        if not isinstance(positions, list):
            continue
        for position in positions:
            if isinstance(position, int):
                positioned.append((position, str(token)))
    return " ".join(token for _, token in sorted(positioned))


def openalex_authors(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    authors: list[str] = []
    for authorship in value:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author") if isinstance(authorship.get("author"), dict) else {}
        name = _clean(str(author.get("display_name") or ""))
        if name:
            authors.append(name)
    return authors


def openalex_named_items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = _clean(str(item.get("display_name") or item.get("name") or ""))
        if name and name not in names:
            names.append(name)
    return names


def openalex_http_error_message(error: urllib.error.HTTPError) -> str:
    detail = ""
    try:
        raw = error.read(4096)
    except OSError:
        raw = b""
    if raw:
        text = raw.decode("utf-8", errors="replace").strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            detail = text
        else:
            if isinstance(payload, dict):
                message = payload.get("message") or payload.get("error")
                detail = str(message or payload).strip()
            else:
                detail = str(payload).strip()
    base = f"HTTP Error {error.code}: {error.reason}"
    if detail:
        return f"{base} - {detail[:500]}"
    return base


def dedupe_candidates(candidates: list[ScoutCandidate]) -> list[ScoutCandidate]:
    seen: set[str] = set()
    unique: list[ScoutCandidate] = []
    for candidate in candidates:
        key = candidate_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def rank_candidates(candidates: list[ScoutCandidate], topics: list[str]) -> list[ScoutCandidate]:
    weighted_keywords = scout_keywords(topics)
    for candidate in candidates:
        score, matches = keyword_score(candidate, weighted_keywords)
        score, adjustments = adjust_domain_score(candidate, score, matches)
        candidate.score = round(score, 2)
        candidate.matched_keywords = matches
        reason_parts = []
        if matches:
            reason_parts.append("Matched " + ", ".join(matches[:8]))
        else:
            reason_parts.append("No configured keywords matched")
        if adjustments:
            reason_parts.append("; ".join(adjustments))
        candidate.ranking_reason = ". ".join(reason_parts) + "."
    return sorted(
        candidates,
        key=lambda candidate: (candidate.score, candidate.published, candidate.title.lower()),
        reverse=True,
    )


def scout_keywords(topics: list[str]) -> dict[str, float]:
    keywords: dict[str, float] = {
        "sre": 5.0,
        "site reliability": 5.0,
        "aiops": 5.0,
        "ai for it operations": 5.0,
        "it operations": 4.5,
        "llm for operations": 4.5,
        "agentic operations": 4.0,
        "autonomous operations": 4.0,
        "incident": 4.5,
        "incident response": 5.0,
        "incident management": 5.0,
        "root cause": 4.5,
        "root-cause": 4.5,
        "failure diagnosis": 4.5,
        "observability": 4.0,
        "telemetry analysis": 4.0,
        "log analysis": 4.0,
        "trace analysis": 4.0,
        "debugging": 4.0,
        "automated debugging": 4.5,
        "distributed systems debugging": 4.5,
        "microservice diagnosis": 4.5,
        "engineering productivity": 4.0,
        "developer productivity": 4.0,
        "software maintenance": 4.0,
        "program repair": 4.0,
        "software reliability engineering": 4.5,
        "efficiency": 3.5,
        "anomaly detection": 3.5,
        "remediation": 3.5,
        "mitigation": 3.5,
        "reliability": 3.0,
        "software operation": 3.0,
        "software engineering": 2.5,
        "llm": 2.5,
        "large language model": 2.5,
        "agent": 2.0,
        "workflow": 2.0,
        "automation": 2.0,
        "monitoring": 2.0,
        "monitoring systems": 2.5,
        "cloud": 1.5,
        "cloud operations": 3.5,
        "production": 1.5,
        "production engineering": 3.5,
    }
    for topic in topics:
        normalized = normalize_text(topic)
        if normalized:
            keywords.setdefault(normalized, 2.0)
    return keywords


def keyword_score(candidate: ScoutCandidate, keywords: dict[str, float]) -> tuple[float, list[str]]:
    title = normalize_text(candidate.title)
    abstract = normalize_text(candidate.abstract)
    category_text = normalize_text(" ".join(candidate.categories))
    score = 0.0
    matches: list[str] = []

    for keyword, weight in keywords.items():
        title_hits = count_phrase(title, keyword)
        abstract_hits = count_phrase(abstract, keyword)
        category_hits = count_phrase(category_text, keyword)
        if title_hits or abstract_hits or category_hits:
            score += title_hits * weight * 4
            score += min(abstract_hits, 3) * weight
            score += category_hits * weight * 0.5
            matches.append(keyword)

    return score, matches


DOMAIN_CONTEXT_TERMS = [
    "incident",
    "incident response",
    "incident management",
    "microservice",
    "distributed systems",
    "service",
    "production",
    "production engineering",
    "operations",
    "observability",
    "telemetry",
    "log analysis",
    "trace analysis",
    "monitoring",
    "cloud",
    "cloud operations",
    "debugging",
    "automated debugging",
    "failure diagnosis",
    "remediation",
    "mitigation",
    "software maintenance",
    "software reliability",
    "sre",
    "site reliability",
    "aiops",
    "it operations",
    "developer productivity",
]

OFF_DOMAIN_TERMS = [
    "pre-silicon",
    "side-channel",
    "processor",
    "hardware",
    "circuit",
    "chip",
    "semiconductor",
    "fpga",
    "verilog",
    "railway",
    "railway workshop",
    "rail incident",
    "workshop",
    "permit",
    "contract",
    "traffic incident",
    "vehicular",
    "vehicle",
    "vehicle incident",
    "medical incident",
    "healthcare incident",
    "power grid",
    "smart grid",
    "transportation",
    "transportation incident",
    "road",
]


def adjust_domain_score(
    candidate: ScoutCandidate, score: float, matches: list[str]
) -> tuple[float, list[str]]:
    text = normalize_text(" ".join([candidate.title, candidate.abstract, " ".join(candidate.categories)]))
    adjustments: list[str] = []

    context_hits = [term for term in DOMAIN_CONTEXT_TERMS if count_phrase(text, term)]
    off_domain_hits = [term for term in OFF_DOMAIN_TERMS if count_phrase(text, term)]

    has_root_cause = "root cause" in matches or "root-cause" in matches
    if has_root_cause and context_hits:
        score += min(len(context_hits), 4) * 2.0
        adjustments.append("boosted for root-cause plus ops context")
    elif has_root_cause:
        score -= 10.0
        adjustments.append("penalized for root-cause without ops context")

    if off_domain_hits:
        score -= min(len(off_domain_hits), 4) * 8.0
        adjustments.append("penalized off-domain terms: " + ", ".join(off_domain_hits[:4]))

    return max(score, 0.0), adjustments


def write_candidates_jsonl(candidates: list[ScoutCandidate], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for candidate in candidates:
            handle.write(json.dumps(scout_candidate_record(candidate), ensure_ascii=False, sort_keys=True) + "\n")


def scout_candidate_record(candidate: ScoutCandidate) -> dict[str, Any]:
    record = candidate.as_dict()
    for key in ["score", "matched_keywords", "ranking_reason", "selected"]:
        record.pop(key, None)
    return record


def download_pdf(candidate: ScoutCandidate, pdf_dir: Path, timeout: int = 60) -> str | None:
    source_dir = pdf_dir / candidate.source
    source_dir.mkdir(parents=True, exist_ok=True)
    filename = safe_filename(candidate.source_id or candidate.title) + ".pdf"
    destination = source_dir / filename
    if destination.exists() and destination.stat().st_size > 0:
        return str(destination)

    for url in candidate_pdf_urls(candidate.source, candidate.source_id, candidate.pdf_url):
        data = fetch_pdf_bytes(url, timeout=timeout)
        if data:
            destination.write_bytes(data)
            return str(destination)
    return None


def fetch_pdf_bytes(url: str, *, timeout: int = 60) -> bytes | None:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; paper-agent/0.1)"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
            final_url = response.geturl()
            content_type = response.headers.get("content-type", "")
    except OSError:
        return None

    if data.startswith(b"%PDF") or "application/pdf" in content_type.lower():
        return data
    if "text/html" not in content_type.lower():
        return None
    text = data.decode("utf-8", "ignore")
    for pdf_url in extract_pdf_links_from_html(final_url, text):
        pdf_data = fetch_pdf_bytes(pdf_url, timeout=timeout)
        if pdf_data:
            return pdf_data
    return None


def candidate_key(candidate: ScoutCandidate) -> str:
    if candidate.source and candidate.source_id:
        return f"{candidate.source}:{candidate.source_id}"
    return "title:" + normalize_text(candidate.title)


def arxiv_id_from_url(url: str) -> str:
    match = re.search(r"arxiv\.org/abs/([^?#]+)", url)
    if not match:
        return ""
    return match.group(1).replace("/", "_")


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    cleaned = cleaned.strip("-._")
    if cleaned:
        return cleaned[:120]
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def normalize_text(value: str) -> str:
    return " ".join(html.unescape(value).lower().split())


def count_phrase(text: str, phrase: str) -> int:
    if not text or not phrase:
        return 0
    escaped = re.escape(phrase).replace(r"\ ", r"\s+")
    pattern = rf"(?<![a-z0-9]){escaped}(?![a-z0-9])"
    return len(re.findall(pattern, text))


def _clean(value: str) -> str:
    return " ".join(html.unescape(value).split())


def _paper_url(entry: ET.Element) -> str:
    for link in entry.findall("atom:link", ARXIV_NS):
        if link.attrib.get("rel") == "alternate":
            return link.attrib.get("href", "")
    return ""


def _pdf_url(entry: ET.Element) -> str | None:
    for link in entry.findall("atom:link", ARXIV_NS):
        if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
            return link.attrib.get("href")
    paper_url = _paper_url(entry)
    if paper_url:
        return paper_url.replace("/abs/", "/pdf/")
    return None

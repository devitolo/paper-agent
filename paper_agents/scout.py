from __future__ import annotations

import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from socket import timeout as SocketTimeout
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol

from paper_agents.openai_helpers import call_openai_json


ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}
DEFAULT_SCOUT_TOPICS = [
    "AIOps",
    "incident management",
    "root cause analysis",
    "production operations",
    "developer productivity",
]
DEFAULT_FETCH_LIMIT = 50
DEFAULT_KEEP_LIMIT = 5
DEFAULT_FRESHNESS_MONTHS = 24
DEFAULT_SCOUT_DIR = Path("data/scout")
DEFAULT_PDF_DIR = Path("data/papers")
DEFAULT_ARXIV_REQUEST_DELAY = 3.0
DEFAULT_ARXIV_RETRIES = 3
DEFAULT_ARXIV_TIMEOUT = 60


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

    def fetch(self, topics: list[str], max_results: int, freshness_months: int) -> list[ScoutCandidate]:
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
                if self.verbose:
                    print(f"arXiv topic failed: {topic}: {error}")
                continue
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
                "arXiv fetch returned 0 candidates because every topic failed: "
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
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    root = ET.fromstring(response.read())
                return list(root.findall("atom:entry", ARXIV_NS))
            except urllib.error.HTTPError as error:
                last_error = error
                if error.code != 429 or attempt >= self.retries:
                    raise
                delay = self._retry_delay(attempt, retry_after=error.headers.get("Retry-After"))
                if self.verbose:
                    print(f"arXiv rate limited topic '{topic}', retrying in {delay:.0f}s")
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError, SocketTimeout) as error:
                last_error = error
                if attempt >= self.retries:
                    raise
                delay = self._retry_delay(attempt)
                if self.verbose:
                    print(f"arXiv request failed for topic '{topic}' ({error}), retrying in {delay:.0f}s")
                time.sleep(delay)
            except ET.ParseError as error:
                last_error = error
                if attempt >= self.retries:
                    raise
                delay = self._retry_delay(attempt)
                if self.verbose:
                    print(f"arXiv returned malformed XML for topic '{topic}', retrying in {delay:.0f}s")
                time.sleep(delay)

        if last_error:
            raise last_error
        return []

    def _retry_delay(self, attempt: int, retry_after: str | None = None) -> float:
        if retry_after and retry_after.isdigit():
            return float(retry_after)
        return self.request_delay * (2 ** attempt)


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
) -> dict[str, Any]:
    """Run the deterministic daily scout MVP and return a run report."""
    source = source or ArxivSource(request_delay=request_delay, retries=retries, timeout=timeout)
    topics = topics or DEFAULT_SCOUT_TOPICS
    run_date = run_date or date.today()

    print(
        f"scout run: source={source.name} topics={len(topics)} "
        f"fetch_limit={fetch_limit} keep={keep_limit} freshness_months={freshness_months} "
        f"request_delay={request_delay}s retries={retries} timeout={timeout}s"
    )
    fetched = source.fetch(topics, max_results=fetch_limit, freshness_months=freshness_months)
    print(f"fetched {len(fetched)} raw candidates")
    candidates = dedupe_candidates(fetched)
    print(f"deduped to {len(candidates)} candidates")
    ranked = rank_candidates(candidates, topics)
    seen_source_ids = seen_source_ids or set()
    selectable = filter_seen_candidates(ranked, seen_source_ids, include_seen=include_seen)
    filtered_seen_count = len(ranked) - len(selectable)
    if filtered_seen_count:
        print(f"filtered {filtered_seen_count} previously seen candidates")
    selected = selectable[:keep_limit]
    print(f"selected top {len(selected)} candidates")
    selected_ids = {candidate_key(candidate) for candidate in selected}

    for candidate in ranked:
        candidate.selected = candidate_key(candidate) in selected_ids

    if download_pdfs:
        for index, candidate in enumerate(selected, 1):
            print(f"downloading PDF {index}/{len(selected)}: {candidate.source_id or candidate.title}")
            candidate.pdf_path = download_pdf(candidate, pdf_dir)
            print(f"pdf path: {candidate.pdf_path or 'not available'}")

    output_path = scout_dir / f"{run_date.isoformat()}.jsonl"
    write_candidates_jsonl(ranked, output_path)
    print(f"wrote scout metadata: {output_path}")

    return {
        "source": source.name,
        "run_date": run_date.isoformat(),
        "freshness_months": freshness_months,
        "fetched_count": len(fetched),
        "candidate_count": len(candidates),
        "stored_count": len(ranked),
        "seen_filtered_count": filtered_seen_count,
        "selected_count": len(selected),
        "output_path": str(output_path),
        "candidates": [candidate.as_dict() for candidate in ranked],
        "selected": [candidate.as_dict() for candidate in selected],
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
        "incident": 4.5,
        "incident response": 5.0,
        "incident management": 5.0,
        "root cause": 4.5,
        "root-cause": 4.5,
        "observability": 4.0,
        "debugging": 4.0,
        "engineering productivity": 4.0,
        "developer productivity": 4.0,
        "efficiency": 3.5,
        "anomaly detection": 3.5,
        "reliability": 3.0,
        "software operation": 3.0,
        "software engineering": 2.5,
        "llm": 2.5,
        "large language model": 2.5,
        "agent": 2.0,
        "workflow": 2.0,
        "automation": 2.0,
        "monitoring": 2.0,
        "cloud": 1.5,
        "production": 1.5,
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
    "microservice",
    "service",
    "production",
    "operations",
    "observability",
    "cloud",
    "debugging",
    "sre",
    "site reliability",
    "aiops",
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
    "workshop",
    "permit",
    "contract",
    "traffic incident",
    "vehicular",
    "vehicle",
    "transportation",
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
            handle.write(json.dumps(candidate.as_dict(), ensure_ascii=False, sort_keys=True) + "\n")


def download_pdf(candidate: ScoutCandidate, pdf_dir: Path, timeout: int = 60) -> str | None:
    if not candidate.pdf_url:
        return None

    source_dir = pdf_dir / candidate.source
    source_dir.mkdir(parents=True, exist_ok=True)
    filename = safe_filename(candidate.source_id or candidate.title) + ".pdf"
    destination = source_dir / filename
    if destination.exists() and destination.stat().st_size > 0:
        return str(destination)

    request = urllib.request.Request(candidate.pdf_url, headers={"User-Agent": "paper-agent/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
    except OSError:
        return None

    if not data.startswith(b"%PDF"):
        return None

    destination.write_bytes(data)
    return str(destination)


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

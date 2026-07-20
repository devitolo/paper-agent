from __future__ import annotations

import html
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

from paper_agents.openai_helpers import call_openai_json


ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}


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


def _clean(value: str) -> str:
    return " ".join(html.unescape(value).split())


def _paper_url(entry: ET.Element) -> str:
    for link in entry.findall("atom:link", ARXIV_NS):
        if link.attrib.get("rel") == "alternate":
            return link.attrib.get("href", "")
    return ""

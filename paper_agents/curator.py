from __future__ import annotations

from typing import Any

from paper_agents.openai_helpers import call_openai_json


class ResearchCurator:
    """Filters Scout output into a short reading list."""

    def run(self, profile: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        system_prompt = """
You are Agent 2: Research Curator.
You primarily work from the Scout's candidates. Do not invent new papers.
Select the best 2-3 papers for this user's time.
Evaluate relevance, practical applicability, novelty, whether it is too theoretical,
and whether it seems worth reading or listening to.
Return only valid JSON in this shape:
{
  "recommendations": [
    {
      "title": "...",
      "url": "...",
      "publication_date": "YYYY-MM-DD",
      "why_worth_time": "brief explanation",
      "curator_score": 0.0
    }
  ]
}
""".strip()
        result = call_openai_json(
            system_prompt,
            {
                "profile": profile,
                "scout_candidates": candidates,
            },
        )
        return result.get("recommendations", [])

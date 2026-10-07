from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paper_agents.db import connect_db, init_db
from paper_agents.web import load_artifacts_for_paper, load_summary, render_match_rationale, source_badge_class, source_display_name
from paper_agents.zenml_pilot import SEED_EXCLUDED_URLS, import_zenml_pilot, select_zenml_candidates


class ZenMLPilotTests(unittest.TestCase):
    def sample_rows(self):
        return [
            {
                "_row_idx": 1,
                "created_at": "2026-10-01T00:00:00Z",
                "title": "HEMA builds an internal AI knowledge assistant with MCP",
                "industry": "Retail",
                "year": 2026,
                "source_url": "https://example.com/hema-ai-assistant",
                "company": "HEMA",
                "application_tags": ["Knowledge Management", "Internal Assistant"],
                "tools_tags": ["MCP", "Bedrock"],
                "extra_tags": [],
                "techniques_tags": ["RAG"],
                "short_summary": "HEMA built an internal AI assistant to help employees find operational knowledge.",
                "full_summary": "HEMA built an internal AI assistant using MCP and Bedrock so employees can find operational knowledge across systems.",
                "webflow_url": "https://www.zenml.io/llmops-database/hema",
            },
            {
                "_row_idx": 2,
                "created_at": "2026-10-02T00:00:00Z",
                "title": "DoorDash uses agents to clean up feature flags",
                "industry": "Delivery",
                "year": 2026,
                "source_url": "https://example.com/doordash-feature-flags",
                "company": "DoorDash",
                "application_tags": ["Developer Productivity"],
                "tools_tags": ["Agents"],
                "extra_tags": [],
                "techniques_tags": ["Code Maintenance"],
                "short_summary": "DoorDash used agents to improve engineering maintenance workflows.",
                "full_summary": "DoorDash used agents to identify and clean up stale feature flags in engineering workflows.",
                "webflow_url": "https://www.zenml.io/llmops-database/doordash",
            },
            {
                "_row_idx": 3,
                "created_at": "2026-10-03T00:00:00Z",
                "title": "Generic root cause analysis for logs",
                "industry": "Software",
                "year": 2026,
                "source_url": "https://example.com/rca-logs",
                "company": "RCA Co",
                "application_tags": ["Observability"],
                "tools_tags": [],
                "extra_tags": [],
                "techniques_tags": [],
                "short_summary": "A root cause incident log analysis example.",
                "full_summary": "A root cause incident log analysis example.",
                "webflow_url": "https://www.zenml.io/llmops-database/rca",
            },
            {
                "_row_idx": 4,
                "created_at": "2026-10-04T00:00:00Z",
                "title": "Seed reviewed item",
                "industry": "Software",
                "year": 2026,
                "source_url": next(iter(SEED_EXCLUDED_URLS)),
                "company": "Seed",
                "application_tags": ["Internal Assistant"],
                "tools_tags": [],
                "extra_tags": [],
                "techniques_tags": [],
                "short_summary": "Seed item should be skipped.",
                "full_summary": "Seed item should be skipped.",
                "webflow_url": "https://www.zenml.io/llmops-database/seed",
            },
        ]

    def test_selects_practical_engineering_items_and_skips_seed_urls(self):
        selected = select_zenml_candidates(self.sample_rows(), keep=3, excluded_urls=SEED_EXCLUDED_URLS)
        urls = [item["url"] for item in selected]
        self.assertIn("https://example.com/hema-ai-assistant", urls)
        self.assertIn("https://example.com/doordash-feature-flags", urls)
        self.assertNotIn(next(iter(SEED_EXCLUDED_URLS)), urls)

    def test_import_creates_review_queue_records_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "paper_agent.db"
            init_db(db_path)
            first = import_zenml_pilot(db_path=db_path, rows=self.sample_rows(), keep=2)
            second = import_zenml_pilot(db_path=db_path, rows=self.sample_rows(), keep=2)
            self.assertEqual(first["imported_count"], 2)
            self.assertEqual(second["imported_count"], 0)
            with connect_db(db_path) as connection:
                rows = connection.execute(
                    """
                    SELECT papers.id, papers.title, paper_sources.source, paper_sources.url
                    FROM papers
                    JOIN paper_sources ON paper_sources.paper_id = papers.id
                    WHERE paper_sources.source = 'zenml'
                    ORDER BY papers.title
                    """
                ).fetchall()
                self.assertEqual(len(rows), 2)
                artifacts = load_artifacts_for_paper(connection, rows[0][0])
                artifact = artifacts.get("triage_summary")
                self.assertIsNotNone(artifact)
                self.assertTrue(artifact["path"].startswith("data/zenml-pilot/"))
                self.assertFalse(Path(artifact["path"]).is_absolute())
                summary_path = db_path.parent.parent / artifact["path"]
                summary = load_summary({**artifact, "path": summary_path})
            self.assertEqual(source_display_name("zenml"), "ZenML")
            self.assertEqual(source_badge_class("zenml"), "source-badge-zenml")
            self.assertEqual(summary["source_type"], "zenml_summary")
            html = render_match_rationale({"summary": summary, "rationale": "ZenML pilot"}, "")
            self.assertIn("ZenML discovery summary", html)


if __name__ == "__main__":
    unittest.main()

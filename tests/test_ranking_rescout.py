"""Exercise actual Scout/Curator persistence across retries; isolate external work."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_agents import db
from paper_agents.pipeline import run_daily_pipeline
from paper_agents.scout import ScoutCandidate


class DeepeningSource:
    name = "arxiv"

    def __init__(self):
        self.calls = []

    def fetch(self, topics, *, max_results, freshness_months):
        self.calls.append(max_results)
        return [ScoutCandidate(
            source="arxiv", source_id=f"2601.9000{i}v1", title=f"Incident response method {i}",
            abstract="Operational incident response.", authors=["Example"],
            published="2026-01-01", updated=None, url=f"https://example.test/{i}",
            pdf_url=None, categories=["cs.SE"], metadata={},
        ) for i in range(min(max_results, 2))]


class RescoutRankingTests(unittest.TestCase):
    def test_rescout_does_not_persist_a_second_recommendation_for_the_first_paper(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pipeline.db"
            source = DeepeningSource()
            with (
                patch("paper_agents.pipeline.create_scout_source", return_value=source),
                patch("paper_agents.pipeline.load_profile", return_value={}),
                patch("paper_agents.pipeline.ReviewerAgent.run", return_value={"cards": []}),
            ):
                run_daily_pipeline(topics=["incident"], fetch_limit=1, keep_limit=2,
                                   max_scout_attempts=2, min_quality_score=25,
                                   db_path=path, mode="test", source_name="arxiv")
            self.assertEqual(source.calls, [1, 2, 4], "Must exercise a real rescout and bounded refill")
            with closing(db.connect_db(path)) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM curator_runs").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT COUNT(DISTINCT paper_id) FROM recommendations").fetchone()[0], 2)
                duplicates = connection.execute("""
                    SELECT paper_id, COUNT(*) FROM recommendations
                    GROUP BY paper_id HAVING COUNT(*) > 1
                """).fetchall()
                self.assertEqual(duplicates, [], "Earlier eligible Scout rows must not bypass repeat exclusion")

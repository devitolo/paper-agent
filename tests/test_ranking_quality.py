"""Ranking contracts and release gates; use only disposable SQLite databases."""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from paper_agents import db
from paper_agents.curator_agent import CuratorAgent, CuratorConfig, evaluate_candidate


class RankingQualityTests(unittest.TestCase):
    def score(self, title=None, abstract=None, profile=None, **metadata):
        return evaluate_candidate(
            {"title": title, "abstract": abstract, **metadata}, profile or {}
        )["score"]

    def test_missing_text_and_null_profile_lists_score_zero(self):
        self.assertEqual(self.score(profile={"interests": None,
                                           "positive_signals": None,
                                           "negative_signals": None}), 0)

    def test_keyword_substrings_do_not_match_unrelated_words(self):
        self.assertEqual(self.score("Cloudberry monitoringless incidents"), 0)

    def test_case_and_whitespace_do_not_change_score(self):
        self.assertEqual(self.score("Incident response"),
                         self.score("INCIDENT   RESPONSE"))

    def test_duplicate_positive_signals_do_not_inflate_score(self):
        self.assertEqual(self.score("specialized topic", profile={"interests": ["specialized topic"]}),
                         self.score("specialized topic", profile={
                             "interests": ["specialized topic"] * 3,
                             "positive_signals": ["specialized topic"]}))

    def test_evaluation_does_not_mutate_candidate_or_profile(self):
        candidate = {"title": "Incident response", "authors": ["Example"]}
        profile = {"interests": ["operations"]}
        before = copy.deepcopy((candidate, profile))
        evaluate_candidate(candidate, profile)
        self.assertEqual((candidate, profile), before)

    def test_negative_signal_lowers_unsaturated_score(self):
        self.assertLess(self.score("Incident railway", profile={"negative_signals": ["railway"]}),
                        self.score("Incident railway"))

    def test_source_and_pdf_url_alone_do_not_inflate_relevance(self):
        for source in ("arxiv", "openalex", "semantic_scholar"):
            with self.subTest(source=source):
                self.assertEqual(self.score("Incident response", source=source, pdf_url=None),
                                 self.score("Incident response", source=source,
                                            pdf_url="https://example.test/paper.pdf"))

    def test_keyword_only_metadata_does_not_receive_perfect_score(self):
        # Architect's saturation concern: no abstract, results, or evidence.
        self.assertLess(self.score("AIOps incident response root cause observability debugging remediation"), 100)

    def test_negative_feedback_still_discriminates_saturated_candidates(self):
        title = "AIOps incident response root cause observability debugging remediation railway"
        self.assertLess(self.score(title, profile={"negative_signals": ["railway"]}),
                        self.score(title))


class RankingPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ranking.db"
        db.init_db(self.path)
        self.connection = db.connect_db(self.path)
        self.addCleanup(self.connection.close)
        self.cycle = db.create_workflow_cycle(self.connection, mode="test", max_scout_attempts=3)

    def candidate(self, title, published=None):
        record = {"source": "arxiv", "source_id": str(len(self.ids)),
                  "title": title, "published": published, "abstract": None}
        paper_id, _ = db.upsert_paper(self.connection, record)
        self.ids.append(paper_id)
        return {**record, "paper_id": paper_id}

    def run_curator(self, candidates, **config):
        return CuratorAgent().run(
            self.connection, workflow_cycle_id=self.cycle, candidates=candidates,
            profile_version=None, scout_attempt_count=3,
            config=CuratorConfig(**config))

    def test_threshold_is_inclusive_and_committed_rows_match_ranked_output(self):
        self.ids = []
        candidates = [self.candidate("Unrelated mathematics"),
                      self.candidate("Incident"), self.candidate("Incident response")]
        boundary = evaluate_candidate(candidates[1], {})["score"]
        result = self.run_curator(candidates, min_quality_score=boundary)
        self.connection.commit()
        reader = db.connect_db(self.path)
        self.addCleanup(reader.close)
        rows = reader.execute(
            "SELECT paper_id, recommendation_order FROM recommendations ORDER BY recommendation_order"
        ).fetchall()
        self.assertEqual(rows, [(self.ids[2], 1), (self.ids[1], 2)])
        self.assertEqual([r["paper_id"] for r in result["recommendations"]],
                         [row[0] for row in rows])
        flags = dict(reader.execute("SELECT paper_id, quality_threshold_met FROM curator_evaluations"))
        self.assertEqual(flags, {self.ids[0]: 0, self.ids[1]: 1, self.ids[2]: 1})

    def test_equal_scores_prefer_newer_date_and_put_missing_date_last(self):
        self.ids = []
        candidates = [self.candidate("Incident", None),
                      self.candidate("Incident", "2026-01-01"),
                      self.candidate("Incident", "2026-09-01")]
        result = self.run_curator(candidates, min_quality_score=1)
        self.assertEqual([r["paper_id"] for r in result["recommendations"]], self.ids[::-1])

    def test_empty_pool_at_retry_limit_records_run_without_recommendations(self):
        result = self.run_curator([], min_quality_score=25)
        self.assertFalse(result["requested_rescout"])
        self.assertEqual(result["recommendations"], [])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM curator_runs").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()

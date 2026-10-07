from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_agents import db
from paper_agents.curator_agent import CuratorAgent, CuratorConfig
from paper_agents.curator_interest import positive_interest_descriptions, score_interest_fit


ABSTRACT = (
    "This paper presents and evaluates a production incident diagnosis system using logs, "
    "metrics, traces, and service dependencies across a realistic microservice environment."
)


class CuratorInterestFitTests(unittest.TestCase):
    def test_uses_only_positive_topical_interest_descriptions(self):
        profile = {
            "interests": ["incident diagnosis with operational telemetry"],
            "positive_signals": ["strong experimental evidence"],
            "negative_signals": ["toy benchmark", "weak evidence"],
        }
        self.assertEqual(
            positive_interest_descriptions(profile),
            ["incident diagnosis with operational telemetry"],
        )

    def test_missing_and_invalid_abstracts_are_explicit_and_never_scored(self):
        calls = []
        scorer = lambda *args: calls.append(args) or 1.0
        missing = score_interest_fit({"title": "Paper", "abstract": None}, ["AIOps"], scorer)
        invalid = score_interest_fit({"title": "Paper", "abstract": "short"}, ["AIOps"], scorer)
        self.assertEqual(missing["status"], "missing_abstract")
        self.assertEqual(invalid["status"], "invalid_abstract")
        self.assertIsNone(missing["score"])
        self.assertEqual(calls, [])

    def test_records_each_interest_and_maximum_with_model_identity(self):
        values = {"incident diagnosis": 3.5, "agent reliability": -1.0}
        result = score_interest_fit(
            {"title": "Operational AI", "abstract": ABSTRACT}, list(values),
            lambda _title, _abstract, interest: values[interest],
        )
        self.assertEqual(result["status"], "scored")
        self.assertEqual(result["score"], 3.5)
        self.assertEqual(result["matched_interest"], "incident diagnosis")
        self.assertEqual(len(result["per_interest"]), 2)
        self.assertTrue(result["model_id"])
        self.assertTrue(result["model_sha256"])
        self.assertEqual(result["preference_scope"], "positive_topical_interests_only")

    def test_curator_orders_threshold_passing_candidates_by_interest_fit_and_persists_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "interest.db"
            db.init_db(path)
            connection = db.connect_db(path)
            self.addCleanup(connection.close)
            cycle = db.create_workflow_cycle(connection, mode="test", max_scout_attempts=1)
            profile_id = db.ensure_profile_version(connection, {
                "interests": ["production incident diagnosis"],
                "positive_signals": ["strong evidence"],
                "negative_signals": ["toy benchmark"],
            })
            low_id, _ = db.upsert_paper(connection, {
                "source": "arxiv", "source_id": "low", "title": "Low fit operations paper",
                "abstract": ABSTRACT,
            })
            high_id, _ = db.upsert_paper(connection, {
                "source": "arxiv", "source_id": "high", "title": "High fit operations paper",
                "abstract": ABSTRACT,
            })

            result = CuratorAgent().run(
                connection, workflow_cycle_id=cycle,
                candidates=[
                    {"paper_id": low_id, "title": "Low fit operations paper", "abstract": ABSTRACT},
                    {"paper_id": high_id, "title": "High fit operations paper", "abstract": ABSTRACT},
                ],
                profile_version=db.current_profile_version(connection), scout_attempt_count=1,
                config=CuratorConfig(
                    min_quality_score=0, max_recommendations=2, max_scout_attempts=1,
                    interest_fit_scorer=lambda title, _abstract, _interest: 9.0 if "High" in title else -2.0,
                ),
            )

            self.assertEqual([row["paper_id"] for row in result["recommendations"]], [high_id, low_id])
            self.assertEqual(result["evaluations"][0]["interest_fit"]["rank"], 1)
            metadata = db.decode_json(connection.execute(
                "SELECT metadata_json FROM curator_runs WHERE id = ?", (result["curator_run_id"],)
            ).fetchone()[0], {})
            self.assertEqual(metadata["interest_fit_interests"], ["production incident diagnosis"])
            self.assertEqual(metadata["evaluations"][str(high_id)]["interest_fit"]["score"], 9.0)
            self.assertEqual(profile_id, db.current_profile_version(connection)["id"])

    def test_very_weak_interest_fit_is_rejected_before_qwen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "interest-gate.db"
            db.init_db(path)
            connection = db.connect_db(path)
            self.addCleanup(connection.close)
            cycle = db.create_workflow_cycle(connection, mode="test", max_scout_attempts=1)
            weak_id, _ = db.upsert_paper(connection, {
                "source": "openalex", "source_id": "weak", "title": "Weak fit paper", "abstract": ABSTRACT,
            })
            strong_id, _ = db.upsert_paper(connection, {
                "source": "openalex", "source_id": "strong", "title": "Strong fit paper", "abstract": ABSTRACT,
            })
            with patch("paper_agents.curator_agent.assess_evidence", return_value={
                "status": "unavailable", "research_type": "unknown", "evidence_quality": "unknown",
                "overclaim_risk": "unknown", "provenance": "source_abstract",
            }) as judge:
                result = CuratorAgent().run(
                    connection, workflow_cycle_id=cycle,
                    candidates=[{"paper_id": weak_id, "title": "Weak fit paper", "abstract": ABSTRACT},
                                {"paper_id": strong_id, "title": "Strong fit paper", "abstract": ABSTRACT}],
                    profile_version=None, scout_attempt_count=1,
                    config=CuratorConfig(
                        min_quality_score=0, max_recommendations=2, max_scout_attempts=1,
                        evidence_enabled=True, interest_descriptions=("production operations",),
                        interest_fit_scorer=lambda title, *_args: -4.0 if "Weak" in title else 2.0,
                    ),
                )
            self.assertEqual(judge.call_count, 1)
            weak = next(item for item in result["evaluations"] if item["paper_id"] == weak_id)
            self.assertEqual(weak["score"], 0.0)
            self.assertEqual(weak["score_components"]["interest_fit_gate"]["status"], "blocked")
            self.assertNotIn(weak_id, {item["paper_id"] for item in result["recommendations"]})


if __name__ == "__main__":
    unittest.main()

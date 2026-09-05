from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_agents import db
from paper_agents.curator_agent import CuratorAgent, CuratorConfig, evaluate_candidate
from paper_agents.curator_rescore import rescore_recommendations
from paper_agents.feedback import ingest_feedback_blob
from paper_agents.pipeline import run_daily_pipeline
from paper_agents.scout import ScoutCandidate


class CuratorV2ScoringTests(unittest.TestCase):
    def test_profile_prose_components_change_score_without_exact_phrase(self):
        candidate = {"title": "Observability for incident response", "abstract": "Production telemetry for debugging."}
        profile = {"interests": ["I want research using production telemetry and observability for practical debugging"]}
        before = evaluate_candidate(candidate, {})
        after = evaluate_candidate(candidate, profile)
        self.assertGreater(after["score"], before["score"])
        self.assertEqual(after["score_components"]["positive_matches"][0]["match"], "components")

    def test_repeated_keywords_have_no_incremental_value(self):
        text = "AIOps incident response root cause observability debugging remediation"
        once = evaluate_candidate({"title": text, "abstract": text}, {})
        repeated = evaluate_candidate({"title": (text + " ") * 100, "abstract": (text + " ") * 100}, {})
        self.assertEqual(once["score"], repeated["score"])
        self.assertLess(repeated["score"], 95)

    def test_negative_components_survive_high_topic_and_profile_scores(self):
        candidate = {"title": "AIOps incident response root cause observability debugging remediation",
                     "abstract": "A toy benchmark provides synthetic experiments with weak evidence.",
                     "evidence": {"full_text_triage": True, "pdf_artifact": True}}
        profile = {"interests": ["AIOps", "incident response", "observability", "debugging"]}
        before = evaluate_candidate(candidate, profile)
        after = evaluate_candidate(candidate, profile | {"negative_signals": ["Weak evidence from toy benchmark experiments"]})
        self.assertEqual(round(before["score"] - after["score"], 2), 12)
        self.assertIn("Penalized for negative signals", after["rationale"])

    def test_negative_prose_does_not_penalize_positive_evidence_by_domain_overlap(self):
        candidate = {"title": "Production engineering workflows", "abstract": "Strong evidence from production engineering workflows."}
        result = evaluate_candidate(candidate, {"negative_signals": ["Weak evidence in production engineering workflows"]})
        self.assertEqual(result["score_components"]["negative_penalty"], 0)

    def test_evidence_tiers_and_conflicting_abstract_only_provenance(self):
        candidate = {"title": "Incident response observability", "abstract": "Production debugging with telemetry."}
        source = evaluate_candidate(candidate, {})
        abstract = evaluate_candidate(candidate | {"evidence": {"abstract_only": True}}, {})
        full = evaluate_candidate(candidate | {"evidence": {"full_text_triage": True, "pdf_artifact": True}}, {})
        conflict = evaluate_candidate(candidate | {"evidence": {"full_text_triage": True, "abstract_only": True}}, {})
        self.assertEqual(source["score"], abstract["score"])
        self.assertEqual(conflict["score"], abstract["score"])
        self.assertGreater(full["score"], abstract["score"])
        self.assertEqual(evaluate_candidate({"title": "Unrelated mathematics", "evidence": {"full_text_triage": True}}, {})["score"], 0)


class CuratorV2PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "curator.db"
        db.init_db(self.path)
        self.connection = db.connect_db(self.path)
        self.addCleanup(self.connection.close)
        self.cycle = db.create_workflow_cycle(self.connection, mode="test", max_scout_attempts=3)
        db.ensure_profile_version(self.connection, {"interests": ["observability"]})
        self.paper, _ = db.upsert_paper(self.connection, {
            "source": "arxiv", "source_id": "v2-test", "title": "Observability incident response",
            "abstract": "Production observability for incident response.",
        })
        self.candidate = {"paper_id": self.paper, "title": "Observability incident response",
                          "abstract": "Production observability for incident response."}

    def run_curator(self, candidates=None, attempt=1):
        return CuratorAgent().run(self.connection, workflow_cycle_id=self.cycle,
                                 candidates=candidates if candidates is not None else [self.candidate],
                                 profile_version=db.current_profile_version(self.connection),
                                 scout_attempt_count=attempt, config=CuratorConfig(max_recommendations=2))

    def test_provenance_loaded_and_components_persisted(self):
        db.insert_artifact(self.connection, self.paper, artifact_type="pdf", path=Path("example.pdf"))
        db.insert_artifact(self.connection, self.paper, artifact_type="triage_summary", path=Path("summary.json"),
                           metadata={"abstract_only": True})
        result = self.run_curator()
        metadata = json.loads(self.connection.execute("SELECT metadata_json FROM curator_runs WHERE id = ?",
                                                      (result["curator_run_id"],)).fetchone()[0])
        self.assertEqual(metadata["evaluations"][str(self.paper)]["evidence"]["level"], "abstract_only_triage")
        self.assertEqual(result["evaluations"][0]["score_components"], metadata["evaluations"][str(self.paper)])

    def test_direct_curator_retries_cannot_duplicate_paper_or_fill_quota_with_it(self):
        first = self.run_curator([self.candidate, self.candidate])
        second = self.run_curator(attempt=2)
        self.assertEqual(len(first["recommendations"]), 1)
        self.assertEqual(second["recommendations"], [])
        self.assertTrue(second["requested_rescout"])
        self.assertEqual(self.connection.execute("SELECT count(*) FROM recommendations").fetchone()[0], 1)

    def test_rescore_preview_is_read_only_and_apply_is_audited_and_idempotent(self):
        result = self.run_curator()
        ingest_feedback_blob(self.connection, paper_id=self.paper, recommendation_id=None,
                             content="Decision: keep\nScore: 4.5\nReason: useful operational evidence", source="test")
        feedback_before = list(self.connection.execute("SELECT * FROM raw_feedback"))
        structured_before = list(self.connection.execute("SELECT * FROM structured_feedback"))
        self.connection.execute("UPDATE curator_evaluations SET score = 100, rationale = 'legacy'")
        self.connection.commit()
        day = datetime.now(timezone.utc).date().isoformat()
        before = self.path.read_bytes()
        preview = rescore_recommendations(self.path, day)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(preview["applied"])
        self.assertEqual(preview["changed_count"], 1)
        applied = rescore_recommendations(self.path, day, apply=True)
        self.assertEqual(applied["results"], preview["results"])
        metadata = json.loads(self.connection.execute("SELECT metadata_json FROM curator_runs WHERE id = ?",
                                                      (result["curator_run_id"],)).fetchone()[0])
        self.assertEqual(metadata["rescore_history"][0]["previous"]["score"], 100)
        self.assertEqual(rescore_recommendations(self.path, day, apply=True)["changed_count"], 0)
        self.assertEqual(rescore_recommendations(self.path, day, source="openalex")["recommendation_count"], 0)
        self.assertEqual(list(self.connection.execute("SELECT * FROM raw_feedback")), feedback_before)
        self.assertEqual(list(self.connection.execute("SELECT * FROM structured_feedback")), structured_before)

    def test_rescore_filters_recommendation_date_and_rolls_back_on_failure(self):
        result = self.run_curator()
        self.connection.execute("UPDATE recommendations SET created_at = '2026-09-05 03:00:00'")
        self.connection.execute("UPDATE curator_evaluations SET score = 100, rationale = 'legacy'")
        self.connection.commit()
        self.assertEqual(rescore_recommendations(self.path, "2026-09-04")["recommendation_count"], 0)
        before = list(self.connection.execute("SELECT * FROM curator_evaluations"))
        metadata_before = self.connection.execute("SELECT metadata_json FROM curator_runs WHERE id = ?",
                                                  (result["curator_run_id"],)).fetchone()[0]
        original_dumps = db.json_dumps

        def fail_after_audit(value):
            if isinstance(value, list):
                raise RuntimeError("injected failure after audit update")
            return original_dumps(value)

        with patch("paper_agents.curator_rescore.db.json_dumps", side_effect=fail_after_audit):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                rescore_recommendations(self.path, "2026-09-05", apply=True)
        self.assertEqual(list(self.connection.execute("SELECT * FROM curator_evaluations")), before)
        self.assertEqual(self.connection.execute("SELECT metadata_json FROM curator_runs WHERE id = ?",
                                                 (result["curator_run_id"],)).fetchone()[0], metadata_before)

    def test_rescore_rejects_invalid_date_before_any_write(self):
        self.connection.commit()
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            rescore_recommendations(self.path, "2026-99-99", apply=True)
        self.assertEqual(before, self.path.read_bytes())


class CuratorV2PipelineTests(unittest.TestCase):
    def test_reviewer_receives_all_unique_recommendations_across_attempts(self):
        class Source:
            name = "arxiv"

            def fetch(self, topics, *, max_results, freshness_months):
                return [ScoutCandidate(source="arxiv", source_id=str(i), title="Incident response observability",
                                       abstract="Production incident response.", authors=[], published="2026-01-01",
                                       updated=None, url="https://example.test", pdf_url=None, categories=[])
                        for i in range(min(max_results, 3))]

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pipeline.db"
            with (patch("paper_agents.pipeline.create_scout_source", return_value=Source()),
                  patch("paper_agents.pipeline.load_profile", return_value={}),
                  patch("paper_agents.pipeline.ReviewerAgent.run", return_value={"cards": []}) as reviewer):
                result = run_daily_pipeline(topics=["incident"], fetch_limit=1, keep_limit=3,
                                            max_scout_attempts=3, db_path=path)
            recommendations = reviewer.call_args.kwargs["recommendations"]
            self.assertEqual(len(recommendations), 3)
            self.assertEqual(len({row["paper_id"] for row in recommendations}), 3)
            self.assertEqual(result["curator"]["recommendations"], recommendations)
            with closing(db.connect_db(path)) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM recommendations").fetchone()[0], 3)

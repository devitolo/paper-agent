from __future__ import annotations

import tempfile
import unittest
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from paper_agents import db
from paper_agents.curator_agent import CuratorAgent, CuratorConfig
from paper_agents.curator_evidence import assess_evidence, evidence_input
from paper_agents.curator_scoring import evaluate_candidate
from paper_agents.curator_rescore import rescore_recommendations


def assessment(**overrides):
    return {
        "status": "ok", "research_type": "empirical", "evidence_quality": "strong",
        "overclaim_risk": "low", "confidence": "high", "provenance": "source_abstract",
        "experiment_or_evaluation": "Measured against a public production dataset.",
        "real_data_or_deployment": "Real incident data.",
        "implementation_detail": "Implementation details are stated.", "novelty": "Clear.",
        "rationale": "Concrete evaluation evidence is stated.", **overrides,
    }


class CuratorV3ScoringTests(unittest.TestCase):
    candidate = {
        "title": "AIOps incident response root cause analysis observability debugging",
        "abstract": "A production engineering system for incident response.",
    }

    def test_empirical_evidence_outranks_theory_with_same_relevance(self):
        empirical = evaluate_candidate(self.candidate | {"evidence_assessment": assessment()}, {})
        theory = evaluate_candidate(self.candidate | {"evidence_assessment": assessment(
            research_type="framework", evidence_quality="weak", overclaim_risk="high",
            experiment_or_evaluation="Qualitative-only discussion; no experiment.",
            real_data_or_deployment="Synthetic-only example.",
        )}, {})
        self.assertGreater(empirical["score"], theory["score"])
        self.assertLessEqual(theory["score"], 55)

    def test_abstract_only_keywords_cannot_reach_top_tier(self):
        result = evaluate_candidate(self.candidate | {
            "evidence": {"abstract_only": True}, "evidence_assessment": assessment(),
        }, {"interests": ["AIOps incident response observability debugging"]})
        self.assertLessEqual(result["score"], 85)

    def test_generic_profile_terms_do_not_inflate_score(self):
        candidate = self.candidate | {
            "abstract": "A production engineering operations reliability system for incident response.",
        }
        generic = evaluate_candidate(candidate, {"interests": ["engineering operations reliability"]})
        none = evaluate_candidate(candidate, {})
        specific = evaluate_candidate(candidate, {"interests": ["root cause analysis observability"]})
        self.assertEqual(generic["score"], none["score"])
        self.assertGreater(specific["score"], none["score"])

    def test_unavailable_judge_is_conservative_and_bounded(self):
        result = evaluate_candidate(self.candidate | {
            "evidence_assessment": {"status": "unavailable", "error": "timeout"},
        }, {"interests": ["root cause analysis observability"]})
        self.assertLessEqual(result["score"], 70)
        self.assertEqual(result["score_components"]["evidence_assessment"]["error"], "timeout")

    def test_broad_vision_without_demonstrated_work_stays_out_of_top_tier(self):
        result = evaluate_candidate({
            "title": "The Future of Site Reliability Engineering: AI-Driven Observability and Autonomous Operations in Multi-Cloud Environments",
            "abstract": "A broad framework for autonomous AIOps in a fictional multi-cloud company.",
            "evidence_assessment": assessment(
                research_type="position", evidence_quality="weak", overclaim_risk="high",
                experiment_or_evaluation="Qualitative-only comparison; no experiment or measurements.",
                real_data_or_deployment="Synthetic-only fictional company scenario.",
                implementation_detail="No implementation detail is stated.",
                rationale="Broad claims are not supported by a dataset, real deployment, or evaluation.",
            ),
        }, {"interests": ["AIOps observability incident response"]})
        self.assertLess(result["score"], 25)

    def test_all_scores_are_bounded(self):
        result = evaluate_candidate(self.candidate | {"evidence_assessment": assessment()}, {
            "interests": ["AIOps", "root cause analysis", "observability", "debugging"],
        })
        self.assertGreaterEqual(result["score"], 0)
        self.assertLessEqual(result["score"], 100)


class CuratorV3EvidenceCallTests(unittest.TestCase):
    def test_malformed_response_becomes_unavailable_assessment(self):
        with patch("paper_agents.curator_evidence.call_ollama", return_value={"response": "not-json"}):
            result = assess_evidence({"title": "Test", "abstract": "Some abstract"})
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(result["error"])

    def test_invalid_model_envelopes_and_empty_schemas_are_unavailable(self):
        for response in (None, [], {"response": "{}"}, {"response": "[]"}):
            with self.subTest(response=response), patch(
                "paper_agents.curator_evidence.call_ollama", return_value=response,
            ):
                result = assess_evidence({"title": "Test", "abstract": "Some abstract"})
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["model"], "qwen2.5:1.5b-instruct")
            self.assertIn("wall_clock_sec", result)

    def test_null_wrong_type_invalid_enum_and_missing_details_are_unavailable(self):
        valid = {
            "research_type": "empirical", "experiment_or_evaluation": "Measured evaluation.",
            "real_data_or_deployment": "Real dataset.", "implementation_detail": "Implementation stated.",
            "novelty": "New method.", "evidence_quality": "strong", "overclaim_risk": "low",
            "confidence": "high", "rationale": "Evidence is concrete.",
        }
        invalid = [
            valid | {"confidence": None, "rationale": None},
            valid | {"confidence": 1, "experiment_or_evaluation": []},
            valid | {"research_type": "made_up_type"},
            {key: value for key, value in valid.items() if key != "implementation_detail"},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), patch(
                "paper_agents.curator_evidence.call_ollama", return_value={"response": json.dumps(payload)},
            ):
                result = assess_evidence({"title": "Test", "abstract": "Some abstract"})
            self.assertEqual(result["status"], "unavailable")
            score = evaluate_candidate(CuratorV3ScoringTests.candidate | {
                "evidence": {"full_text_triage": True, "pdf_artifact": True},
                "evidence_assessment": result,
            }, {})["score"]
            self.assertLessEqual(score, 70)

    def test_placeholder_evidence_is_unavailable_and_earns_no_credit(self):
        fields = ("experiment_or_evaluation", "real_data_or_deployment", "implementation_detail", "novelty", "rationale")
        for field in fields:
            for placeholder in ("string", "  StRiNg\n", field, field.replace("_", " ")):
                with self.subTest(field=field, placeholder=placeholder), patch(
                    "paper_agents.curator_evidence.call_ollama",
                    return_value={"response": json.dumps(assessment(**{field: placeholder}))},
                ):
                    result = assess_evidence(CuratorV3ScoringTests.candidate)
                self.assertEqual(result["status"], "unavailable")
                self.assertIn(field, result["error"])
                self.assertEqual(result["evidence_quality"], "unknown")
                self.assertEqual(result["rationale"], "No usable evidence assessment.")
                evaluated = evaluate_candidate(CuratorV3ScoringTests.candidate | {
                    "evidence": {"full_text_triage": True, "pdf_artifact": True},
                    "evidence_assessment": result,
                }, {})
                self.assertEqual(evaluated["score_components"]["evidence_adjustment"], 0)
                self.assertLessEqual(evaluated["score"], 70)

    def test_short_technical_evidence_and_string_prose_remain_valid(self):
        payload = assessment(experiment_or_evaluation="A/B test", real_data_or_deployment="Linux",
                             implementation_detail="eBPF", novelty="String matching")
        with patch("paper_agents.curator_evidence.call_ollama", return_value={"response": json.dumps(payload)}):
            result = assess_evidence(CuratorV3ScoringTests.candidate)
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["error"])
        for field in ("experiment_or_evaluation", "real_data_or_deployment", "implementation_detail", "novelty"):
            self.assertEqual(result[field], payload[field])
        evaluated = evaluate_candidate(CuratorV3ScoringTests.candidate | {"evidence_assessment": result}, {})
        self.assertGreater(evaluated["score_components"]["evidence_adjustment"], 0)

    def test_timeout_becomes_unavailable_assessment(self):
        with patch("paper_agents.curator_evidence.call_ollama", side_effect=TimeoutError("timed out")):
            result = assess_evidence({"title": "Test", "abstract": "Some abstract"})
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("timed out", result["error"])

    def test_agent_persists_mocked_evidence_assessment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v3.db"
            db.init_db(path)
            connection = db.connect_db(path)
            self.addCleanup(connection.close)
            cycle = db.create_workflow_cycle(connection, mode="test", max_scout_attempts=1)
            paper_id, _ = db.upsert_paper(connection, {
                "source": "arxiv", "source_id": "v3", "title": "AIOps incident response",
                "abstract": "Measured production evaluation for observability.",
            })
            with patch("paper_agents.curator_agent.assess_evidence", return_value=assessment()) as judge:
                result = CuratorAgent().run(
                    connection, workflow_cycle_id=cycle,
                    candidates=[{"paper_id": paper_id, "title": "AIOps incident response",
                                 "abstract": "Measured production evaluation for observability."}],
                    profile_version=None, scout_attempt_count=1,
                    config=CuratorConfig(min_quality_score=0, max_scout_attempts=1, evidence_enabled=True),
                )
            self.assertEqual(judge.call_count, 1)
            self.assertEqual(result["evaluations"][0]["score_components"]["evidence_assessment"]["research_type"], "empirical")
            metadata = db.decode_json(connection.execute("SELECT metadata_json FROM curator_runs").fetchone()[0], {})
            self.assertTrue(metadata["evidence_enabled"])
            self.assertEqual(metadata["evaluations"][str(paper_id)]["evidence_assessment"]["status"], "ok")

    def test_judge_runs_without_holding_a_sqlite_write_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock.db"
            db.init_db(path)
            connection = db.connect_db(path)
            self.addCleanup(connection.close)
            cycle = db.create_workflow_cycle(connection, mode="test", max_scout_attempts=1)
            paper_id, _ = db.upsert_paper(connection, {
                "source": "arxiv", "source_id": "lock", "title": "AIOps incident response",
                "abstract": "Measured production evaluation for observability.",
            })
            writer_errors = []

            def judge(*_args, **_kwargs):
                with sqlite3.connect(path, timeout=0.05) as writer:
                    try:
                        writer.execute("UPDATE workflow_cycles SET state = state WHERE id = ?", (cycle,))
                    except sqlite3.OperationalError as exc:
                        writer_errors.append(str(exc))
                return assessment()

            with patch("paper_agents.curator_agent.assess_evidence", side_effect=judge):
                CuratorAgent().run(
                    connection, workflow_cycle_id=cycle,
                    candidates=[{"paper_id": paper_id, "title": "AIOps incident response",
                                 "abstract": "Measured production evaluation for observability."}],
                    profile_version=None, scout_attempt_count=1,
                    config=CuratorConfig(min_quality_score=1, max_scout_attempts=1, evidence_enabled=True),
                )
            self.assertEqual(writer_errors, [])

    def test_merged_full_text_triage_is_read_and_provenance_caps_source_abstract(self):
        with tempfile.TemporaryDirectory() as tmp:
            triage_path = Path(tmp) / "triage.json"
            triage_path.write_text(json.dumps({"merged": {
                "research_problem": "Measured root cause analysis.",
                "why_it_matters": "Production incident evidence.",
                "approach": "Implemented and evaluated on a public dataset.",
            }}), encoding="utf-8")
            text, provenance = evidence_input({"abstract": "Short abstract.", "evidence": {
                "triage_path": str(triage_path), "full_text_triage": True,
            }}, 7000)
        self.assertEqual(provenance, "full_text_extraction")
        self.assertIn("Measured root cause", text)
        result = evaluate_candidate(CuratorV3ScoringTests.candidate | {
            "evidence": {"full_text_triage": True, "pdf_artifact": True},
            "evidence_assessment": assessment(provenance="source_abstract"),
        }, {"interests": ["root cause analysis observability"]})
        self.assertLessEqual(result["score"], 85)

    def test_rescore_preserves_persisted_v3_evidence_assessment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rescore.db"
            db.init_db(path)
            connection = db.connect_db(path)
            self.addCleanup(connection.close)
            profile = {"interests": ["AIOps observability incident response"], "positive_signals": [], "negative_signals": []}
            db.ensure_profile_version(connection, profile)
            cycle = db.create_workflow_cycle(connection, mode="test", max_scout_attempts=1)
            paper_id, _ = db.upsert_paper(connection, {
                "source": "arxiv", "source_id": "rescore", "title": "AIOps framework",
                "abstract": "A broad framework for a fictional company.",
            })
            weak = assessment(research_type="framework", evidence_quality="weak", overclaim_risk="high",
                              experiment_or_evaluation="Qualitative-only; no experiment.",
                              real_data_or_deployment="Synthetic-only scenario.")
            with patch("paper_agents.curator_agent.assess_evidence", return_value=weak):
                created = CuratorAgent().run(
                    connection, workflow_cycle_id=cycle,
                    candidates=[{"paper_id": paper_id, "title": "AIOps framework",
                                 "abstract": "A broad framework for a fictional company."}],
                    profile_version=db.current_profile_version(connection), scout_attempt_count=1,
                    config=CuratorConfig(min_quality_score=0, max_scout_attempts=1, evidence_enabled=True),
                )
            connection.commit()
            recommendation_day = connection.execute(
                "SELECT date(created_at) FROM recommendations"
            ).fetchone()[0]
            preview = rescore_recommendations(path, recommendation_day)
            self.assertEqual(preview["results"][0]["score"], created["evaluations"][0]["score"])
            self.assertEqual(preview["results"][0]["components"]["evidence_assessment"]["research_type"], "framework")


if __name__ == "__main__":
    unittest.main()

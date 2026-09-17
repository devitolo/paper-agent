"""Independent acceptance gates for the offline ranking proposal."""
import json
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from paper_agents import db
from paper_agents.curator_quality_v4 import evaluate_candidate_v4, select_targeted_passages, validate_grounding
from paper_agents.curator_scoring import evaluate_candidate
from paper_agents.ranking_quality_replay import canonical_hash, replay_fixture


CANDIDATE = {"title": "AIOps incident response root cause analysis observability debugging",
             "abstract": "Production incident response and observability evaluation."}


def fixture_for(candidate):
    profile = {}
    return {"schema_version": 1, "profile": profile, "profile_sha256": canonical_hash(profile),
            "candidates": [candidate], "candidates_sha256": canonical_hash([candidate]),
            "partitions": {"development": [candidate["id"]], "heldout": []},
            "budgets": {"max_total_chars": 7000, "max_section_chars": 2000, "max_passages": 8},
            "evaluation": {"top_k": 1, "useful_min_score": 3}}


class RankingQualityIndependentTests(unittest.TestCase):
    def test_unavailable_text_does_not_become_negative_empirical_evidence(self):
        result = evaluate_candidate_v4(CANDIDATE, {}, {
            "contribution_type": "empirical", "experimental_claims_made": True,
            "claims": {"measurements": {"state": "unknown"}},
        }, select_targeted_passages(""))
        self.assertEqual(result["score_components"]["grounded_rigor"], 0)

    def test_explicit_denial_of_measurements_cannot_ground_positive_measurement_claim(self):
        quote = "We did not conduct experiments on 100 services or measure latency in this paper."
        selection = select_targeted_passages("Evaluation\n\n" + quote)
        passage = next(item for item in selection["passages"] if quote in item["text"])
        result = validate_grounding({"contribution_type": "empirical", "claims": {
            "measurements": {"state": "present", "citations": [
                {"passage_id": passage["id"], "quote": quote}]}},
        }, selection)
        self.assertFalse(result["claims"]["measurements"]["grounded"])

    def test_passive_denial_cannot_ground_measurements(self):
        quote = "Experiments were not conducted on 100 production services in this paper."
        selection = select_targeted_passages("Evaluation\n\n" + quote)
        passage = next(item for item in selection["passages"] if quote in item["text"])
        result = validate_grounding({"claims": {"measurements": {
            "state": "present", "citations": [{"passage_id": passage["id"], "quote": quote}]}}}, selection)
        self.assertFalse(result["claims"]["measurements"]["grounded"])

    def test_missing_baseline_does_not_negate_explicit_measurements(self):
        quote = "We measured latency of 180 ms across 24 services without a baseline comparison."
        selection = select_targeted_passages("Evaluation\n\n" + quote)
        passage = next(item for item in selection["passages"] if quote in item["text"])
        result = validate_grounding({"claims": {"measurements": {
            "state": "present", "citations": [{"passage_id": passage["id"], "quote": quote}]}}}, selection)
        self.assertTrue(result["claims"]["measurements"]["grounded"])

    def test_architecture_label_does_not_exempt_explicit_experimental_claims(self):
        selection = select_targeted_passages("Evaluation\n\nNo experiments or measurements were performed in this work.")
        assessment = {"contribution_type": "architecture", "experimental_claims_made": True,
                      "claims": {"measurements": {"state": "absent"}}}
        result = evaluate_candidate_v4(CANDIDATE, {}, assessment, selection)
        self.assertLess(result["score_components"]["grounded_rigor"], 0)

    def test_replay_baseline_preserves_recorded_v3_evidence(self):
        candidate = {"id": "fixture", **CANDIDATE, "document_text": "",
            "evidence": {"full_text_triage": True, "pdf_artifact": True},
            "evidence_assessment": {"status": "ok", "research_type": "empirical",
                                    "evidence_quality": "strong", "overclaim_risk": "low",
                                    "provenance": "full_text_extraction"}}
        report = replay_fixture(fixture_for(candidate))
        self.assertEqual(report["results"][0]["baseline_score"], evaluate_candidate(candidate, {})["score"])

    def test_replay_retains_summary_provenance_in_candidate_report(self):
        candidate = {"id": "fixture", **CANDIDATE,
                     "document_text": "Approach\n\nThe architecture requires ownership because shared services create ambiguity.",
                     "evidence_scope": "stored_triage_summary"}
        report = replay_fixture(fixture_for(candidate))
        self.assertIn("stored_triage_summary", json.dumps(report["results"][0]))

    def test_export_cli_refuses_to_overwrite_its_input_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            db.init_db(path)
            connection = db.connect_db(path)
            try:
                db.ensure_profile_version(connection, {"interests": []})
                connection.commit()
            finally:
                connection.close()
            before = path.read_bytes()
            result = subprocess.run([sys.executable, "scripts/export_ranking_quality_corpus.py",
                "--db", str(path), "--output", str(path)], capture_output=True, text=True, timeout=10)
            self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), hashlib.sha256(before).digest(),
                             "export overwrote input SQLite database")
            self.assertNotEqual(result.returncode, 0)

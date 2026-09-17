from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from paper_agents import db
from paper_agents.curator_quality_v4 import (
    evaluate_candidate_v4, select_targeted_passages, validate_grounding,
)
from paper_agents.curator_scoring import SCORING_VERSION as PRODUCTION_SCORING_VERSION
from paper_agents.ranking_quality_replay import canonical_hash, replay_fixture
from paper_agents.feedback import ingest_feedback_blob
from scripts.export_ranking_quality_corpus import export_fixture


DOCUMENT = """Introduction

This paper discusses enterprise AI platform operations and governance.

Architecture

The control plane requires explicit policy ownership because shared services otherwise hide accountability.

The design improves consistency but adds approval latency and operating overhead.

Evaluation

We evaluated 24 production services and measured deployment latency at 180 ms.

Compared with the existing baseline, the system reduced failed deployments by 18 percent.

Limitations

The study is limited to one organization and may not generalize to regulated environments.

Conclusion

Platform teams should adopt staged policy checks and publish ownership boundaries before deployment.
"""


def citation(selection, section, sentence):
    passage = next(item for item in selection["passages"]
                   if item["section"] == section and sentence in item["text"])
    return {"passage_id": passage["id"], "quote": sentence}


class PassageSelectionTests(unittest.TestCase):
    def test_selects_late_sections_with_exact_offsets_and_budget_metadata(self):
        result = select_targeted_passages(DOCUMENT, max_total_chars=1200, max_section_chars=400)
        self.assertEqual({item["section"] for item in result["passages"]},
                         {"method", "evaluation", "limitations", "conclusion"})
        for item in result["passages"]:
            self.assertEqual(DOCUMENT[item["start"]:item["end"]], item["text"])
        self.assertLessEqual(result["selected_chars"], 1200)
        self.assertTrue(result["truncated"])

    def test_unavailable_and_unselected_are_not_whole_paper_absence(self):
        unavailable = select_targeted_passages("")
        self.assertTrue(all(section["state"] == "unavailable"
                            for section in unavailable["sections"].values()))
        sparse = select_targeted_passages("A short introduction with no relevant section.")
        self.assertTrue(all(section["state"] == "not_inspected"
                            for section in sparse["sections"].values()))

    def test_keyword_fallback_handles_unconventional_heading(self):
        result = select_targeted_passages(
            "What we built\n\nOur architecture requires ownership because policy failures propagate."
        )
        method = next(item for item in result["passages"] if item["section"] == "method")
        self.assertEqual(method["selection_reason"], "keyword_fallback")

    def test_keyword_fallback_ignores_incidental_late_section_term(self):
        result = select_targeted_passages(
            "Applications produced third-party statistics across many organizations. "
            + ("Operational context without section evidence. " * 8)
            + "A cited study reported external results."
        )

        self.assertFalse(any(item["section"] == "evaluation" for item in result["passages"]))

    def test_keyword_fallback_supports_explicit_plural_section_terms(self):
        cases = {
            "method": "Methods describe explicit ownership boundaries.",
            "evaluation": "Experiments measured latency across 24 services.",
            "limitations": "Constraints include one participating organization.",
            "conclusion": "Recommendations prioritize staged policy checks.",
        }
        for kind, text in cases.items():
            with self.subTest(kind=kind):
                result = select_targeted_passages(text)
                self.assertTrue(any(item["section"] == kind for item in result["passages"]))

    def test_keyword_fallback_preserves_word_boundaries_for_plural_terms(self):
        result = select_targeted_passages(
            "Systematicity and experimentalism are words, not section evidence."
        )

        self.assertEqual(result["passages"], [])


class ContributionAwareScoringTests(unittest.TestCase):
    candidate = {
        "title": "Enterprise AI platform architecture for production operations",
        "abstract": "Governance and observability tradeoffs for reliable production engineering.",
    }
    profile = {"interests": ["enterprise AI platforms", "production engineering", "observability"]}

    def setUp(self):
        self.selection = select_targeted_passages(DOCUMENT)

    def test_empirical_credit_requires_grounded_measurement_and_comparison(self):
        measured = "We evaluated 24 production services and measured deployment latency at 180 ms."
        compared = "Compared with the existing baseline, the system reduced failed deployments by 18 percent."
        assessment = {
            "contribution_type": "empirical", "experimental_claims_made": True,
            "claims": {
                "measurements": {"state": "present", "citations": [citation(self.selection, "evaluation", measured)]},
                "baselines": {"state": "present", "citations": [citation(self.selection, "evaluation", compared)]},
            },
        }
        supported = evaluate_candidate_v4(self.candidate, self.profile, assessment, self.selection)
        unsupported = evaluate_candidate_v4(
            self.candidate, self.profile,
            {"contribution_type": "empirical", "experimental_claims_made": True}, self.selection,
        )
        self.assertGreater(supported["score_components"]["grounded_rigor"], 0)
        self.assertEqual(unsupported["score_components"]["grounded_rigor"], 0)

    def test_architecture_can_earn_credit_without_experiments(self):
        reasoning = "The control plane requires explicit policy ownership because shared services otherwise hide accountability."
        tradeoff = "The design improves consistency but adds approval latency and operating overhead."
        action = "Platform teams should adopt staged policy checks and publish ownership boundaries before deployment."
        assessment = {"contribution_type": "architecture", "claims": {
            "reasoning": {"state": "present", "citations": [citation(self.selection, "method", reasoning)]},
            "tradeoffs": {"state": "present", "citations": [citation(self.selection, "method", tradeoff)]},
            "actionable_insight": {"state": "present", "citations": [citation(self.selection, "conclusion", action)]},
        }}
        result = evaluate_candidate_v4(self.candidate, self.profile, assessment, self.selection)
        self.assertEqual(result["score_components"]["contribution_type"], "architecture")
        self.assertEqual(result["score_components"]["grounded_rigor"], 19)

    def test_correct_location_without_claim_support_earns_no_credit(self):
        irrelevant = "We evaluated 24 production services and measured deployment latency at 180 ms."
        passage = next(item for item in self.selection["passages"] if irrelevant in item["text"])
        assessment = {"contribution_type": "architecture", "claims": {
            "tradeoffs": {"state": "present", "citations": [
                {"passage_id": passage["id"], "quote": irrelevant}
            ]}
        }}
        grounded = validate_grounding(assessment, self.selection)
        self.assertFalse(grounded["claims"]["tradeoffs"]["grounded"])

    def test_unknown_and_malformed_assessment_cannot_add_credit(self):
        for assessment in ({}, {"contribution_type": "nonsense", "claims": "bad"}):
            result = evaluate_candidate_v4(self.candidate, self.profile, assessment, self.selection)
            self.assertEqual(result["score_components"]["grounded_rigor"], 0)


class OfflineReplayTests(unittest.TestCase):
    def fixture(self):
        profile = {"interests": ["enterprise AI platform architecture", "production operations"]}
        selection = select_targeted_passages(DOCUMENT)
        reasoning = "The control plane requires explicit policy ownership because shared services otherwise hide accountability."
        claims = {kind: {"state": "unknown", "citations": []} for kind in (
            "measurements", "baselines", "reasoning", "tradeoffs", "actionable_insight", "limitations"
        )}
        claims["reasoning"] = {
            "state": "present", "citations": [citation(selection, "method", reasoning)]
        }
        fixture = {
            "schema_version": 1, "corpus_id": "contract-only-not-quality-evidence",
            "profile_id": "sanitized-test", "profile": profile,
            "profile_sha256": canonical_hash(profile),
            "model_config": {"mode": "stored_judgments", "calls": 0},
            "budgets": {"max_total_chars": 7000, "max_section_chars": 2000, "max_passages": 8},
            "evaluation": {"top_k": 1, "useful_min_score": 3},
            "partitions": {"development": ["architecture"], "heldout": ["unrelated"]},
            "candidates": [
                {"id": "architecture", **self.candidate, "document_text": DOCUMENT,
                 "decision": "keep", "user_score": 3,
                 "proposed_assessment": {
                     "schema_version": 1, "status": "complete",
                     "contribution_type": "architecture", "experimental_claims_made": False,
                     "overclaim_risk": "low", "claims": claims,
                 }},
                {"id": "unrelated", "title": "Abstract algebra", "abstract": "Group theory.",
                 "document_text": "", "decision": "reject", "user_score": 1,
                 "proposed_assessment": {}},
            ],
        }
        fixture["candidates_sha256"] = canonical_hash(fixture["candidates"])
        return fixture

    candidate = ContributionAwareScoringTests.candidate

    def test_replay_is_deterministic_network_free_and_preserves_inputs(self):
        fixture = self.fixture()
        before = copy.deepcopy(fixture)
        first = replay_fixture(fixture)
        second = replay_fixture(fixture)
        self.assertEqual(fixture, before)
        self.assertEqual(first["model_calls"], 0)
        self.assertEqual(first["network_calls"], 0)
        for left, right in zip(first["results"], second["results"]):
            self.assertEqual({k: v for k, v in left.items() if k != "runtime_seconds"},
                             {k: v for k, v in right.items() if k != "runtime_seconds"})
        self.assertEqual(PRODUCTION_SCORING_VERSION, "evidence-aware-v3")
        self.assertEqual(first["partitions"]["development"]["proposed"]["missed_useful"], [])
        self.assertEqual(first["partitions"]["development"]["proposed"]["useful_denominator"], 1)
        self.assertIn("profile_interpretation_audit", first)
        self.assertIn("score_without_grounded_rigor",
                      first["results"][0]["proposed_components"])

    def test_replay_marks_missing_assessments_incomplete(self):
        fixture = self.fixture()
        report = replay_fixture(fixture)
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertFalse(report["quality_claim_ready"])
        self.assertIn("missing_proposed_assessments", report["quality_claim_blockers"])
        self.assertEqual(report["assessment_coverage"]["valid_count"], 1)
        self.assertEqual(report["assessment_coverage"]["missing_ids"], ["unrelated"])
        rows = {row["id"]: row for row in report["results"]}
        self.assertEqual(rows["architecture"]["proposed_assessment_status"], "valid")
        self.assertEqual(rows["unrelated"]["proposed_assessment_status"], "missing")

    def test_replay_rejects_changed_profile_and_partition_leakage(self):
        fixture = self.fixture()
        fixture["profile"]["interests"].append("changed")
        with self.assertRaisesRegex(ValueError, "profile_sha256"):
            replay_fixture(fixture)
        fixture = self.fixture()
        fixture["partitions"]["heldout"].append("architecture")
        with self.assertRaisesRegex(ValueError, "exactly one partition"):
            replay_fixture(fixture)

    def test_replay_rejects_changed_candidates_and_unfrozen_partition(self):
        fixture = self.fixture()
        fixture["candidates"][0]["title"] = "changed"
        with self.assertRaisesRegex(ValueError, "candidates_sha256"):
            replay_fixture(fixture)
        fixture = self.fixture()
        fixture["partitions"]["heldout"].remove("unrelated")
        fixture["partitions"]["unassigned"] = ["unrelated"]
        with self.assertRaisesRegex(ValueError, "unassigned"):
            replay_fixture(fixture)

    def test_cli_writes_only_requested_report(self):
        fixture = self.fixture()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixture.json"
            path.write_text(json.dumps(fixture), encoding="utf-8")
            before = path.read_bytes()
            report = replay_fixture(json.loads(path.read_text()))
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(report["corpus_id"], "contract-only-not-quality-evidence")


class SanitizedExportTests(unittest.TestCase):
    def test_export_is_read_only_and_omits_raw_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "quality.db"
            db.init_db(path)
            connection = db.connect_db(path)
            profile = {"interests": ["enterprise AI platforms"],
                       "positive_signals": [], "negative_signals": []}
            db.ensure_profile_version(connection, profile)
            paper_id, _ = db.upsert_paper(connection, {
                "source": "ssrn", "source_id": "example", "title": "Platform architecture",
                "abstract": "Enterprise AI governance and production tradeoffs.",
            })
            ingest_feedback_blob(
                connection, paper_id=paper_id, recommendation_id=None,
                content="Decision: keep\nScore: 3\nReason: private reviewer narrative",
                source="test",
            )
            triage_path = root / "triage.json"
            triage_path.write_text(json.dumps({"merged": {
                "approach": "The architecture requires ownership because shared services create ambiguity."
            }}), encoding="utf-8")
            db.insert_artifact(connection, paper_id, artifact_type="triage_summary", path=triage_path,
                               metadata={"full_text_available": True})
            connection.commit()
            connection.close()
            before = path.read_bytes()
            fixture = export_fixture(path, development_ids=set(), heldout_ids=set(),
                                     assign_unlisted="development")
            self.assertEqual(path.read_bytes(), before)
            serialized = json.dumps(fixture)
            self.assertNotIn("private reviewer narrative", serialized)
            self.assertNotIn(str(triage_path), serialized)
            self.assertEqual(fixture["candidates"][0]["decision"], "keep")
            self.assertEqual(fixture["candidates"][0]["user_score"], 3)
            self.assertEqual(fixture["candidates_sha256"], canonical_hash(fixture["candidates"]))

    def test_cli_collision_guards_cover_replay_symlink_and_export_hardlink(self):
        fixture = OfflineReplayTests().fixture()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frozen = root / "frozen.json"
            frozen.write_text(json.dumps(fixture), encoding="utf-8")
            alias = root / "fixture-link.json"
            alias.symlink_to(frozen)
            before = frozen.read_bytes()
            replay = subprocess.run(
                [sys.executable, "-m", "paper_agents.ranking_quality_replay",
                 str(frozen), "--output", str(alias)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(replay.returncode, 0)
            self.assertEqual(hashlib.sha256(frozen.read_bytes()).digest(), hashlib.sha256(before).digest())

            database = root / "state.db"
            db.init_db(database)
            connection = db.connect_db(database)
            db.ensure_profile_version(connection, {"interests": []})
            connection.commit()
            connection.close()
            hardlink = root / "state-alias.db"
            os.link(database, hardlink)
            before = database.read_bytes()
            export = subprocess.run(
                [sys.executable, "scripts/export_ranking_quality_corpus.py",
                 "--db", str(database), "--output", str(hardlink)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(export.returncode, 0)
            self.assertEqual(hashlib.sha256(database.read_bytes()).digest(), hashlib.sha256(before).digest())


if __name__ == "__main__":
    unittest.main()

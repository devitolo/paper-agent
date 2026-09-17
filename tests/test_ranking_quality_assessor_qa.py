"""Independent offline-assessor safety tests; no model or source calls."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess
import sys

from paper_agents import db
from paper_agents.curator_quality_v4 import CLAIM_KINDS
from paper_agents.curator_quality_v4 import select_targeted_passages
from paper_agents.ranking_quality_assessor import assess_fixture, build_assessment_prompt, _read_primary_text
from paper_agents.ranking_quality_replay import canonical_hash


QUOTE = "The architecture requires explicit ownership because shared services hide accountability."


class AssessorIndependentTests(unittest.TestCase):
    def test_target_618_is_processed_before_budget_despite_earlier_artifact(self):
        target = copy.deepcopy(self.fixture["candidates"][0])
        target["id"] = "618"
        self.fixture["candidates"].append(target)
        self.fixture["partitions"]["development"].append("618")
        self.fixture["candidates_sha256"] = canonical_hash(self.fixture["candidates"])
        _, report = assess_fixture(self.fixture, self.path, conversion_only=True,
            max_papers=1, target_ids={"618"}, primary_paths={"618": self.primary})
        self.assertEqual(report["attempted"], 1)
        self.assertEqual([item["id"] for item in report["conversion_reviews"]], ["618"])
        self.assertEqual(report["model_calls"], 0)

    def test_cli_rejects_both_destination_aliases_and_existing_files_without_mutation(self):
        fixture = self.root / "fixture.json"
        fixture.write_text(json.dumps(self.fixture))
        symbolic = self.root / "symbolic"
        symbolic.symlink_to(self.primary)
        hard = self.root / "hard"
        hard.hardlink_to(self.primary)
        existing = self.root / "existing"
        existing.write_text("preserve this report")
        protected = [fixture, self.path, self.primary, symbolic, hard, existing]
        original = {path: path.read_bytes() for path in protected}
        script = Path(__file__).resolve().parents[1] / "scripts" / "assess_ranking_quality_corpus.py"
        for destination in ("--output", "--report"):
            for source in protected:
                with self.subTest(destination=destination, source=source.name):
                    other = self.root / "unused.json"
                    arguments = {"--output": other, "--report": other}
                    arguments[destination] = source
                    result = subprocess.run([sys.executable, str(script), str(fixture),
                        "--db", str(self.path), "--conversion-only",
                        *[part for key, path in arguments.items() for part in (key, str(path))]],
                        capture_output=True, text=True, timeout=10)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(other.exists())
                    for path, data in original.items():
                        self.assertEqual(path.read_bytes(), data)

    def test_conversion_only_preserves_inputs_and_never_calls_provider(self):
        before = copy.deepcopy(self.fixture)
        database_before = self.path.read_bytes()
        with patch("paper_agents.ranking_quality_assessor.call_assessor_ollama") as network:
            output, report = assess_fixture(
                self.fixture, self.path, conversion_only=True,
                provider=network, max_source_chars=10000,
            )
        network.assert_not_called()
        self.assertEqual(report["model_calls"], 0)
        self.assertEqual(report["converted"], 1)
        self.assertEqual(self.fixture, before)
        self.assertEqual(self.path.read_bytes(), database_before)
        self.assertEqual(output["candidates"][0]["proposed_assessment"], {})
        for passage in report["conversion_reviews"][0]["passages"]:
            self.assertEqual(passage["text"], output["candidates"][0]["document_text"][passage["start"]:passage["end"]])

    def test_conversion_only_reports_source_truncation(self):
        self.primary.write_text("Method\n\n" + QUOTE + " " * 11000)
        _, report = assess_fixture(self.fixture, self.path, conversion_only=True, max_source_chars=10000)
        self.assertEqual(report["model_calls"], 0)
        self.assertTrue(report["conversion_reviews"][0]["source_truncated"])
        self.assertEqual(report["conversion_reviews"][0]["source_chars_read"], 10000)

    def test_cli_refuses_output_that_overwrites_explicit_primary_source(self):
        fixture = self.root / "fixture.json"
        fixture.write_text(json.dumps(self.fixture))
        original = self.primary.read_bytes()
        script = Path(__file__).resolve().parents[1] / "scripts" / "assess_ranking_quality_corpus.py"
        result = subprocess.run([
            sys.executable, str(script), str(fixture), "--db", str(self.path),
            "--output", str(self.primary), "--report", str(self.root / "report.json"),
            "--conversion-only", "--primary-pdf",
            self.fixture["candidates"][0]["id"] + "=" + str(self.primary),
        ], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0, "CLI must reject overwriting primary evidence")
        self.assertEqual(self.primary.read_bytes(), original)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "state.db"
        db.init_db(self.path)
        con = db.connect_db(self.path)
        try:
            paper_id, _ = db.upsert_paper(con, {"source": "test", "source_id": "fixture", "title": "Architecture"})
            self.primary = self.root / "primary.txt"
            self.primary.write_text("Method\n\n" + QUOTE)
            db.insert_artifact(con, paper_id, artifact_type="pdf", path=self.primary)
            con.commit()
        finally:
            con.close()
        profile = {"interests": [], "positive_signals": [], "negative_signals": []}
        candidate = {"id": str(paper_id), "title": "Architecture", "document_text": "summary",
                     "proposed_assessment": {}, "decision": "keep", "user_score": 3}
        self.fixture = {"schema_version": 1, "profile": profile, "profile_sha256": canonical_hash(profile),
                        "candidates": [candidate], "candidates_sha256": canonical_hash([candidate]),
                        "partitions": {"development": [str(paper_id)], "heldout": []},
                        "budgets": {"max_total_chars": 7000, "max_section_chars": 2000, "max_passages": 8}}

    def envelope(self):
        claims = {kind: {"state": "unknown", "citations": []} for kind in CLAIM_KINDS}
        claims["reasoning"] = {"state": "present", "citations": [{"passage_id": "p1", "quote": QUOTE}]}
        return {"response": json.dumps({"schema_version": 1, "status": "complete",
            "contribution_type": "architecture", "experimental_claims_made": False,
            "overclaim_risk": "low", "claims": claims})}

    def test_missing_usage_is_unknown_not_reported_zero(self):
        _, report = assess_fixture(self.fixture, self.path, provider=lambda *args: self.envelope())
        self.assertIsNone(report.get("reported_prompt_tokens"))
        self.assertIsNone(report.get("reported_output_tokens"))

    def test_provider_failure_diagnostics_exclude_private_content(self):
        def failed(*args):
            raise RuntimeError("PRIVATE_REVIEW token=PRIVATE_SECRET /private/source.pdf")
        _, report = assess_fixture(self.fixture, self.path, provider=failed)
        self.assertNotIn("PRIVATE", json.dumps(report))
        self.assertNotIn("/private/source.pdf", json.dumps(report))

    def test_invalid_frozen_profile_hash_is_rejected_before_provider_call(self):
        self.fixture["profile"]["interests"].append("changed")
        calls = []
        with self.assertRaises(ValueError):
            assess_fixture(self.fixture, self.path, provider=lambda *args: calls.append(args) or self.envelope())
        self.assertEqual(calls, [])

    def test_fixture_cannot_increase_hard_selected_text_budget(self):
        self.fixture["budgets"] = {"max_total_chars": 100000, "max_section_chars": 100000, "max_passages": 80}
        with self.assertRaises(ValueError):
            assess_fixture(self.fixture, self.path, provider=lambda *args: self.envelope())

    def test_failed_reassessment_does_not_leave_stale_success_in_output(self):
        self.fixture["candidates"][0]["proposed_assessment"] = json.loads(self.envelope()["response"])
        self.fixture["candidates_sha256"] = canonical_hash(self.fixture["candidates"])
        def failed(*args):
            raise TimeoutError("timed out")
        output, report = assess_fixture(self.fixture, self.path, provider=failed)
        self.assertEqual(report["completed"], 0)
        self.assertEqual(output["candidates"][0]["proposed_assessment"], {})

    def test_rating_and_review_changes_leave_entire_prompt_unchanged(self):
        selection = {"passages": [{"id": "p1", "section": "method", "text": QUOTE}]}
        before = build_assessment_prompt(self.fixture["candidates"][0], self.fixture["profile"], selection)
        changed = copy.deepcopy(self.fixture)
        changed["candidates"][0].update(decision="reject", user_score=1, raw_review="PRIVATE_REVIEW",
                                        expected_rank=1, baseline_score=100, proposed_score=0)
        changed["profile"]["notes"] = "PRIVATE_NOTES"
        after = build_assessment_prompt(changed["candidates"][0], changed["profile"], selection)
        self.assertEqual(before, after)

    def test_inline_heading_body_is_preferred_over_intro_keyword_mentions(self):
        evidence = "We evaluated 24 production services and measured deployment latency at 180 ms."
        text = ("Introduction\n\nAn evaluation is important for production services.\n\n"
                "Prior evaluation research motivates this work.\n\n"
                "Evaluation\n" + evidence)
        selected = select_targeted_passages(text)
        self.assertTrue(any(evidence in passage["text"] for passage in selected["passages"]
                            if passage["section"] == "evaluation"))

    def test_pdf_timeout_is_bounded_sanitized_and_never_calls_model(self):
        pdf = self.root / "primary.pdf"
        pdf.write_bytes(b"%PDF-fixture")
        con = db.connect_db(self.path)
        try:
            con.execute("UPDATE artifacts SET path=?", (str(pdf),))
            con.commit()
        finally:
            con.close()
        with patch("paper_agents.ranking_quality_assessor.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(["PRIVATE_PATH"], 30)) as converter:
            output, report = assess_fixture(self.fixture, self.path,
                provider=lambda *args: self.fail("model must not run after conversion timeout"))
        self.assertLessEqual(converter.call_args.kwargs["timeout"], 30)
        self.assertEqual(report["failures"][0]["error_code"], "timeout")
        self.assertNotIn("PRIVATE_PATH", json.dumps(report))
        self.assertEqual(output["candidates"][0]["proposed_assessment"], {})

    def test_malformed_usage_does_not_crash_or_double_count_success(self):
        response = self.envelope() | {"prompt_eval_count": [], "eval_count": True}
        _, report = assess_fixture(self.fixture, self.path, provider=lambda *args: response)
        self.assertEqual(report["completed"], 1)
        self.assertEqual(report["failures"], [])
        self.assertIsNone(report["reported_prompt_tokens"])
        self.assertIsNone(report["reported_output_tokens"])

    def test_nullable_profile_lists_do_not_crash_assessment(self):
        self.fixture["profile"]["interests"] = None
        self.fixture["profile_sha256"] = canonical_hash(self.fixture["profile"])
        _, report = assess_fixture(self.fixture, self.path, provider=lambda *args: self.envelope())
        self.assertEqual(report["completed"], 1)

    def test_mismatched_crop_pages_are_not_silently_reported_complete(self):
        pdf = self.root / "primary.pdf"
        pdf.write_bytes(b"%PDF-fixture")
        def convert(command, **kwargs):
            destination = Path(command[-1])
            destination.write_text("LEFT_PAGE_ONE\fLEFT_PAGE_TWO" if destination.name == "left.txt" else "RIGHT_PAGE_ONE")
        with patch("paper_agents.ranking_quality_assessor.subprocess.run", side_effect=convert):
            text, truncated = _read_primary_text(pdf, 10000)
        self.assertTrue("LEFT_PAGE_TWO" in text or truncated,
                        "unequal crop page streams must preserve unmatched text or flag incomplete extraction")

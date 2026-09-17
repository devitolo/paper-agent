from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from paper_agents import db
from paper_agents.curator_quality_v4 import CLAIM_KINDS
from paper_agents.ranking_quality_assessor import assess_fixture, build_assessment_prompt
from paper_agents.ranking_quality_replay import canonical_hash, replay_fixture


def fixture(candidate):
    profile = {"interests": ["enterprise AI platforms"], "notes": "PRIVATE PROFILE NOTES"}
    result = {
        "schema_version": 1, "profile": profile, "profile_sha256": canonical_hash(profile),
        "profile_id": "profile19", "candidates": [candidate],
        "partitions": {"development": [candidate["id"]], "heldout": []},
        "budgets": {"max_total_chars": 7000, "max_section_chars": 2000, "max_passages": 8},
        "evaluation": {"top_k": 1, "useful_min_score": 3},
    }
    result["candidates_sha256"] = canonical_hash(result["candidates"])
    return result


def valid_response():
    claims = {kind: {"state": "unknown", "citations": []} for kind in CLAIM_KINDS}
    claims["reasoning"] = {"state": "present", "citations": [{
        "passage_id": "p1",
        "quote": "The architecture requires explicit ownership because shared services hide accountability.",
    }]}
    return {"schema_version": 1, "status": "complete", "contribution_type": "architecture",
            "experimental_claims_made": False, "overclaim_risk": "low", "claims": claims}


class RankingQualityAssessorTests(unittest.TestCase):
    def test_prompt_allowlist_excludes_labels_scores_and_profile_notes(self):
        candidate = {"id": "1", "title": "Platform architecture", "decision": "SECRET_REJECT",
                     "user_score": 1, "raw_review": "PRIVATE REVIEW"}
        selection = {"passages": [{"id": "p1", "section": "method", "text": "primary text"}]}
        prompt = build_assessment_prompt(candidate, {
            "interests": ["platforms"], "notes": "PRIVATE PROFILE NOTES"
        }, selection)
        self.assertNotIn("SECRET_REJECT", prompt)
        self.assertNotIn("PRIVATE REVIEW", prompt)
        self.assertNotIn("PRIVATE PROFILE NOTES", prompt)
        self.assertIn("primary text", prompt)

    def test_read_only_primary_text_assessment_is_grounded_and_replayable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.db"
            db.init_db(database)
            connection = db.connect_db(database)
            paper_id, _ = db.upsert_paper(connection, {
                "source": "test", "source_id": "one", "title": "Platform architecture",
                "abstract": "abstract",
            })
            primary = root / "paper.txt"
            primary.write_text(
                "Method\n\nThe architecture requires explicit ownership because shared services hide accountability.",
                encoding="utf-8",
            )
            db.insert_artifact(connection, paper_id, artifact_type="pdf", path=primary)
            connection.commit()
            connection.close()
            candidate = {"id": str(paper_id), "title": "Platform architecture",
                         "abstract": "abstract", "decision": "reject", "user_score": 1,
                         "document_text": "summary", "proposed_assessment": {}}
            frozen = fixture(candidate)
            before = database.read_bytes()
            prompts = []

            def provider(url, model, prompt, timeout):
                prompts.append(prompt)
                return {"response": json.dumps(valid_response()),
                        "prompt_eval_count": 100, "eval_count": 50}

            assessed, report = assess_fixture(frozen, database, provider=provider)
            self.assertEqual(database.read_bytes(), before)
            self.assertEqual(report["completed"], 1)
            self.assertEqual(report["model_calls"], 1)
            self.assertNotIn('"decision"', prompts[0])
            self.assertNotIn('"user_score"', prompts[0])
            replay = replay_fixture(assessed)
            self.assertTrue(replay["evaluation_complete"])
            self.assertGreater(replay["assessment_coverage"]["usable_citation_count"], 0)

    def test_missing_primary_pdf_is_unknown_without_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            db.init_db(database)
            frozen = fixture({"id": "1", "title": "No PDF", "decision": "keep",
                              "user_score": 3, "document_text": "summary",
                              "proposed_assessment": {}})
            assessed, report = assess_fixture(
                frozen, database, provider=lambda *args: self.fail("provider called"),
            )
            self.assertEqual(report["model_calls"], 0)
            self.assertEqual(report["skipped"][0]["reason"], "primary_pdf_unavailable")
            self.assertEqual(assessed["candidates"][0]["proposed_assessment"], {})

    def test_conversion_only_emits_reviewable_offsets_without_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.db"
            db.init_db(database)
            primary = root / "primary.txt"
            primary.write_text("Method\n\n" + valid_response()["claims"]["reasoning"]["citations"][0]["quote"])
            candidate = {"id": "618", "title": "Architecture", "decision": "keep",
                         "user_score": 3, "document_text": "summary", "proposed_assessment": {}}
            assessed, report = assess_fixture(
                fixture(candidate), database, max_papers=1, conversion_only=True,
                primary_paths={"618": primary},
                provider=lambda *args: self.fail("conversion-only mode called provider"),
            )
            self.assertEqual(report["model_calls"], 0)
            self.assertEqual(report["converted"], 1)
            passage = report["conversion_reviews"][0]["passages"][0]
            text = assessed["candidates"][0]["document_text"]
            self.assertEqual(text[passage["start"]:passage["end"]], passage["text"])
            self.assertEqual(assessed["candidates"][0]["proposed_assessment"], {})


if __name__ == "__main__":
    unittest.main()

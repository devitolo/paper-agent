from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from paper_agents import db
from paper_agents.bootstrap import import_legacy_scout_files
from paper_agents.curator_agent import CuratorAgent, CuratorConfig
from paper_agents.scout import ScoutCandidate
from paper_agents.scout import run_daily_scout
from paper_agents.scout_agent import ScoutAgent, ScoutConfig


class FakeSource:
    name = "fake"

    def __init__(self, candidates):
        self.candidates = candidates

    def fetch(self, topics, max_results, freshness_months):
        return self.candidates[:max_results]


def candidate(source_id: str, title: str, *, source: str = "arxiv", arxiv_id: str | None = None) -> ScoutCandidate:
    metadata = {"query_topic": "AIOps"}
    if arxiv_id:
        metadata["arxiv_id"] = arxiv_id
    return ScoutCandidate(
        source=source,
        source_id=source_id,
        title=title,
        abstract="LLM incident management root cause production operations observability cloud debugging.",
        authors=["Example Author"],
        published="2026-01-01",
        updated=None,
        url=f"https://example.test/{source_id}",
        pdf_url=f"https://example.test/{source_id}.pdf",
        categories=["cs.SE"],
        metadata=metadata,
    )


class BackendV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "paper_agent.db"
        db.init_db(self.db_path)
        self.connection = db.connect_db(self.db_path)
        self.cycle_id = db.create_workflow_cycle(self.connection, mode="test", max_scout_attempts=3)
        self.profile_id = db.ensure_profile_version(
            self.connection,
            {
                "interests": ["AIOps", "incident management", "root cause analysis"],
                "positive_signals": ["production operations"],
                "negative_signals": ["railway"],
            },
        )

    def tearDown(self):
        self.connection.close()
        self.tmp.cleanup()

    def test_scout_candidate_records_do_not_have_preference_scores(self):
        columns = [row[1] for row in self.connection.execute("PRAGMA table_info(scout_candidates)")]
        self.assertNotIn("score", columns)
        self.assertNotIn("ranking_reason", columns)
        self.assertNotIn("selected", columns)


    def test_legacy_scout_daily_output_has_no_preference_scores(self):
        output_dir = Path(self.tmp.name) / "scout"
        output = run_daily_scout(
            topics=["AIOps"],
            source=FakeSource([candidate("2601.dailyv1", "Daily Scout")]),
            fetch_limit=1,
            keep_limit=1,
            scout_dir=output_dir,
            download_pdfs=False,
        )
        self.assertNotIn("selected", output)
        record = output["candidates"][0]
        self.assertNotIn("score", record)
        self.assertNotIn("ranking_reason", record)
        self.assertNotIn("selected", record)

    def test_previously_discovered_paper_is_excluded_on_later_scout_run(self):
        source = FakeSource([candidate("2601.1v1", "Incident RCA")])
        agent = ScoutAgent(source=source)
        config = ScoutConfig(topics=["AIOps"], max_candidates=5)
        first = agent.run(self.connection, workflow_cycle_id=self.cycle_id, attempt_number=1, config=config)
        second = agent.run(self.connection, workflow_cycle_id=self.cycle_id, attempt_number=2, config=config)
        self.assertEqual(first["eligible_count"], 1)
        self.assertEqual(second["eligible_count"], 0)
        row = self.connection.execute(
            """
            SELECT excluded, exclusion_reason
            FROM scout_candidates
            WHERE scout_run_id = ?
            """,
            (second["scout_run_id"],),
        ).fetchone()
        self.assertEqual(row, (1, "previously_discovered"))

    def test_same_arxiv_identity_dedupes_across_sources(self):
        arxiv_id, is_new = db.upsert_paper(
            self.connection,
            {
                "source": "arxiv",
                "source_id": "2301.03797v2",
                "title": "Incident LLM",
                "url": "https://arxiv.org/abs/2301.03797",
            },
        )
        microsoft_id, microsoft_is_new = db.upsert_paper(
            self.connection,
            {
                "source": "microsoft_research",
                "source_id": "incident-llm",
                "arxiv_id": "2301.03797v2",
                "title": "Incident LLM",
                "url": "https://www.microsoft.com/research/...",
            },
        )
        self.assertTrue(is_new)
        self.assertFalse(microsoft_is_new)
        self.assertEqual(arxiv_id, microsoft_id)

    def test_curator_stores_all_evaluations_and_caps_recommendations(self):
        source = FakeSource([candidate(f"2601.{i}v1", f"Incident RCA {i}") for i in range(5)])
        scout_result = ScoutAgent(source=source).run(
            self.connection,
            workflow_cycle_id=self.cycle_id,
            attempt_number=1,
            config=ScoutConfig(topics=["AIOps"], max_candidates=5),
        )
        candidates = db.eligible_candidates_for_cycle(self.connection, self.cycle_id)
        result = CuratorAgent().run(
            self.connection,
            workflow_cycle_id=self.cycle_id,
            candidates=candidates,
            profile_version=db.current_profile_version(self.connection),
            scout_attempt_count=1,
            config=CuratorConfig(max_recommendations=5, min_quality_score=1, max_scout_attempts=3),
        )
        self.assertEqual(scout_result["eligible_count"], 5)
        self.assertEqual(len(result["evaluations"]), 5)
        self.assertEqual(len(result["recommendations"]), 3)
        evaluation_count = self.connection.execute("SELECT COUNT(*) FROM curator_evaluations").fetchone()[0]
        recommendation_count = self.connection.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0]
        self.assertEqual(evaluation_count, 5)
        self.assertEqual(recommendation_count, 3)

    def test_curator_can_return_fewer_than_three_and_rescout_is_bounded(self):
        paper_id, _ = db.upsert_paper(
            self.connection,
            {
                "source": "arxiv",
                "source_id": "2601.unrelatedv1",
                "title": "Unrelated math",
                "abstract": "No relevant terms.",
            },
        )
        candidates = [{"paper_id": paper_id, "scout_candidate_id": None, "title": "Unrelated math", "abstract": "No relevant terms."}]
        result = CuratorAgent().run(
            self.connection,
            workflow_cycle_id=self.cycle_id,
            candidates=candidates,
            profile_version=db.current_profile_version(self.connection),
            scout_attempt_count=3,
            config=CuratorConfig(max_recommendations=3, min_quality_score=50, max_scout_attempts=3),
        )
        self.assertEqual(result["recommendations"], [])
        self.assertFalse(result["requested_rescout"])

    def test_scouting_guidance_is_versioned_and_only_latest_active_loads(self):
        first = db.create_scouting_guidance(self.connection, curator_run_id=None, guidance_text="old")
        second = db.create_scouting_guidance(self.connection, curator_run_id=None, guidance_text="new")
        active = db.active_scouting_guidance(self.connection)
        self.assertNotEqual(first, second)
        self.assertEqual(active["id"], second)
        inactive_count = self.connection.execute("SELECT COUNT(*) FROM scouting_guidance WHERE active = 0").fetchone()[0]
        self.assertEqual(inactive_count, 1)


    def test_legacy_scout_import_recovers_selected_recommendations(self):
        jsonl = Path(self.tmp.name) / "2026-07-29.jsonl"
        jsonl.write_text(
            '\n'.join(
                [
                    '{"source":"arxiv","source_id":"2601.selectedv1","title":"Selected paper","url":"https://arxiv.org/abs/2601.selectedv1","pdf_url":"https://arxiv.org/pdf/2601.selectedv1","published":"2026-07-29","abstract":"incident management","score":67,"matched_keywords":["incident"],"ranking_reason":"good","selected":true}',
                    '{"source":"arxiv","source_id":"2601.otherv1","title":"Other paper","url":"https://arxiv.org/abs/2601.otherv1","pdf_url":"https://arxiv.org/pdf/2601.otherv1","published":"2026-07-29","abstract":"incident management","score":20,"matched_keywords":["incident"],"ranking_reason":"ok","selected":false}',
                ]
            )
            + "\n"
        )
        self.connection.close()
        output = import_legacy_scout_files([jsonl], db_path=self.db_path)
        self.connection = db.connect_db(self.db_path)
        self.assertEqual(output["runs"][0]["candidate_count"], 2)
        self.assertEqual(output["runs"][0]["recommendation_count"], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 2)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM curator_evaluations").fetchone()[0], 2)
        recommended = self.connection.execute(
            """
            SELECT paper_sources.source_id
            FROM recommendations
            JOIN paper_sources ON paper_sources.paper_id = recommendations.paper_id
            """
        ).fetchone()[0]
        self.assertEqual(recommended, "2601.selectedv1")

    def test_raw_feedback_is_immutable_and_can_have_multiple_parse_attempts(self):
        raw_id, created = db.create_raw_feedback(self.connection, content="Great paper. Score 5.")
        same_raw_id, duplicate_created = db.create_raw_feedback(self.connection, content="Great paper. Score 5.")
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(raw_id, same_raw_id)
        failed = db.create_feedback_parse_attempt(
            self.connection,
            raw_feedback_id=raw_id,
            parser_name="feedback-agent",
            parser_version="v1",
            model="test",
            status="failed",
            error="bad json",
        )
        succeeded = db.create_feedback_parse_attempt(
            self.connection,
            raw_feedback_id=raw_id,
            parser_name="feedback-agent",
            parser_version="v2",
            model="test",
            status="succeeded",
            output={"decision": "keep"},
        )
        self.assertNotEqual(failed, succeeded)
        content = self.connection.execute("SELECT content FROM raw_feedback WHERE id = ?", (raw_id,)).fetchone()[0]
        attempts = self.connection.execute("SELECT COUNT(*) FROM feedback_parse_attempts WHERE raw_feedback_id = ?", (raw_id,)).fetchone()[0]
        self.assertEqual(content, "Great paper. Score 5.")
        self.assertEqual(attempts, 2)

    def test_profile_updates_create_versions_with_provenance(self):
        raw_id, _ = db.create_raw_feedback(self.connection, content="I liked applied incident papers.")
        attempt_id = db.create_feedback_parse_attempt(
            self.connection,
            raw_feedback_id=raw_id,
            parser_name="feedback-agent",
            parser_version="v1",
            model="test",
            status="succeeded",
            output={"decision": "keep"},
        )
        structured_id = db.create_structured_feedback(
            self.connection,
            parse_attempt_id=attempt_id,
            paper_id=None,
            decision="keep",
            score=5,
            observations=["applied"],
            preference_signals=["incident management"],
        )
        version_id = db.create_profile_version(
            self.connection,
            {"interests": ["incident management"]},
            source_structured_feedback_id=structured_id,
            change_summary="Added incident management preference",
        )
        current = db.current_profile_version(self.connection)
        self.assertEqual(current["id"], version_id)
        self.assertEqual(current["source_structured_feedback_id"], structured_id)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM profile_versions").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()

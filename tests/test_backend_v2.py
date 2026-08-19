from __future__ import annotations

import json
import io
import sqlite3
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from paper_agents import cli
from paper_agents import db
from paper_agents.feedback import apply_feedback_to_profile, call_gemini_json, gemini_timeout_seconds, ingest_feedback_blob, rebuild_feedback_profile
from paper_agents.cli import run_feedback_add, run_feedback_apply, run_feedback_rebuild_profile
from paper_agents.bootstrap import import_legacy_scout_files
from paper_agents.curator_agent import CuratorAgent, CuratorConfig
from paper_agents.curator_agent import evaluate_candidate
from paper_agents.scout import DEFAULT_SCOUT_TOPICS
from paper_agents.scout import OpenAlexSource
from paper_agents.scout import SemanticScholarSource
from paper_agents.scout import ScoutCandidate
from paper_agents.scout import openalex_http_error_message
from paper_agents.scout import openalex_rejection_reason
from paper_agents.scout import openalex_work_to_candidate
from paper_agents.scout import semantic_scholar_paper_to_candidate
from paper_agents.scout import rank_candidates
from paper_agents.scout import run_daily_scout
from paper_agents.scout import scout_candidate_record
from paper_agents.reviewer_agent import card_from_recommendation
from paper_agents.reviewer_agent import recommended_papers_missing_triage
from paper_agents.scout_agent import ScoutAgent, ScoutConfig
from paper_agents import web


class FakeSource:
    name = "fake"

    def __init__(self, candidates):
        self.candidates = candidates

    def fetch(self, topics, max_results, freshness_months):
        return self.candidates[:max_results]


class FakeHttpResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


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

    def test_default_scout_topics_cover_practical_ops_clusters(self):
        topics = {topic.lower() for topic in DEFAULT_SCOUT_TOPICS}
        expected = {
            "aiops",
            "ai for it operations",
            "llm for operations",
            "agentic operations",
            "incident response",
            "failure diagnosis",
            "observability",
            "telemetry analysis",
            "log analysis",
            "trace analysis",
            "developer productivity",
            "software maintenance",
            "automated debugging",
            "program repair",
            "software reliability engineering",
            "cloud operations",
            "microservice diagnosis",
            "distributed systems debugging",
            "production engineering",
        }
        self.assertTrue(expected.issubset(topics))

    def test_semantic_scholar_normalizes_external_ids_and_pdf(self):
        candidate = semantic_scholar_paper_to_candidate(
            {
                "paperId": "abc123",
                "title": "LLM Incident Response",
                "abstract": "Root cause analysis for cloud operations.",
                "authors": [{"name": "Ada Lovelace"}, {"name": "Grace Hopper"}],
                "year": 2026,
                "publicationDate": "2026-08-01",
                "url": "https://www.semanticscholar.org/paper/abc123",
                "openAccessPdf": {"url": "https://example.test/paper.pdf"},
                "externalIds": {"DOI": "10.1234/example", "ArXiv": "2608.12345"},
                "fieldsOfStudy": ["Computer Science"],
                "publicationTypes": ["JournalArticle"],
                "venue": "ExampleConf",
            }
        )

        self.assertEqual(candidate.source, "semantic_scholar")
        self.assertEqual(candidate.source_id, "abc123")
        self.assertEqual(candidate.doi, "10.1234/example")
        self.assertEqual(candidate.arxiv_id, "2608.12345")
        self.assertEqual(candidate.pdf_url, "https://example.test/paper.pdf")
        self.assertEqual(candidate.authors, ["Ada Lovelace", "Grace Hopper"])
        self.assertEqual(candidate.published, "2026-08-01")
        self.assertEqual(candidate.categories, ["Computer Science"])
        self.assertEqual(candidate.metadata["external_ids"]["DOI"], "10.1234/example")

    def test_semantic_scholar_fetch_uses_mocked_api_response(self):
        payload = {
            "data": [
                {
                    "paperId": "abc123",
                    "title": "Semantic Scholar AIOps Paper",
                    "abstract": "AIOps for incident response.",
                    "authors": [{"name": "Example Author"}],
                    "year": 2026,
                    "url": "https://www.semanticscholar.org/paper/abc123",
                    "openAccessPdf": {"url": "https://example.test/abc123.pdf"},
                    "externalIds": {"DOI": "10.5555/abc123"},
                    "fieldsOfStudy": ["Computer Science"],
                }
            ]
        }
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["timeout"] = timeout
            return FakeHttpResponse(payload)

        source = SemanticScholarSource(request_delay=0, retries=0, timeout=12, verbose=False, api_key="test-key")
        with patch("paper_agents.scout.urllib.request.urlopen", fake_urlopen):
            candidates = source.fetch(["AIOps"], max_results=3, freshness_months=24)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source, "semantic_scholar")
        self.assertEqual(candidates[0].source_id, "abc123")
        self.assertEqual(candidates[0].doi, "10.5555/abc123")
        self.assertEqual(candidates[0].metadata["query_topic"], "AIOps")
        self.assertIn("api.semanticscholar.org", captured["url"])
        self.assertEqual(captured["timeout"], 12)
        self.assertEqual(captured["headers"].get("X-api-key"), "test-key")

    def test_scout_daily_cli_selects_semantic_scholar_source(self):
        captured = {}

        def fake_run_daily_scout(**kwargs):
            captured["source"] = kwargs["source"]
            return {"source": kwargs["source"].name, "candidates": []}

        with patch("paper_agents.cli.run_daily_scout", fake_run_daily_scout):
            with patch("paper_agents.cli.print_section"):
                with patch(
                    "sys.argv",
                    [
                        "paper_agents.cli",
                        "scout-daily",
                        "--source",
                        "semantic_scholar",
                        "--fetch",
                        "1",
                        "--no-download",
                        "--db",
                        str(self.db_path),
                    ],
                ):
                    cli.main()

        self.assertEqual(captured["source"].name, "semantic_scholar")

    def test_openalex_normalizes_abstract_metadata_and_pdf(self):
        candidate = openalex_work_to_candidate(
            {
                "id": "https://openalex.org/W123456789",
                "doi": "https://doi.org/10.1234/openalex",
                "title": "OpenAlex Microservice Diagnosis",
                "abstract_inverted_index": {
                    "Root": [0],
                    "cause": [1],
                    "analysis": [2],
                    "for": [3],
                    "microservices": [4],
                },
                "authorships": [
                    {"author": {"display_name": "Ada Lovelace"}},
                    {"author": {"display_name": "Grace Hopper"}},
                ],
                "publication_year": 2026,
                "publication_date": "2026-08-02",
                "primary_location": {
                    "landing_page_url": "https://example.test/openalex-paper",
                    "pdf_url": "https://example.test/openalex-paper.pdf",
                    "source": {"display_name": "Example Venue"},
                },
                "open_access": {"is_oa": True, "oa_url": "https://example.test/oa"},
                "concepts": [{"display_name": "Software engineering"}],
                "keywords": [{"display_name": "microservice diagnosis"}],
                "primary_topic": {"display_name": "Computer science"},
                "locations": [{"landing_page_url": "https://example.test/alternate"}],
            }
        )

        self.assertEqual(candidate.source, "openalex")
        self.assertEqual(candidate.source_id, "W123456789")
        self.assertEqual(candidate.doi, "https://doi.org/10.1234/openalex")
        self.assertEqual(candidate.abstract, "Root cause analysis for microservices")
        self.assertEqual(candidate.authors, ["Ada Lovelace", "Grace Hopper"])
        self.assertEqual(candidate.published, "2026-08-02")
        self.assertEqual(candidate.url, "https://example.test/openalex-paper")
        self.assertEqual(candidate.pdf_url, "https://example.test/openalex-paper.pdf")
        self.assertEqual(candidate.categories, ["Software engineering", "microservice diagnosis"])
        self.assertEqual(candidate.metadata["venue"], "Example Venue")
        self.assertEqual(candidate.metadata["source_metadata"]["openalex_id"], "https://openalex.org/W123456789")

    def test_openalex_fetch_uses_mocked_api_response(self):
        payload = {
            "results": [
                {
                    "id": "https://openalex.org/W123456789",
                    "doi": "https://doi.org/10.5555/openalex",
                    "display_name": "OpenAlex AIOps Paper",
                    "abstract_inverted_index": {"AIOps": [0], "observability": [1]},
                    "authorships": [{"author": {"display_name": "Example Author"}}],
                    "publication_year": 2026,
                    "primary_location": {
                        "landing_page_url": "https://example.test/openalex",
                        "pdf_url": None,
                    },
                    "best_oa_location": {"pdf_url": "https://example.test/openalex.pdf"},
                    "open_access": {"is_oa": True, "oa_url": "https://example.test/oa"},
                    "concepts": [{"display_name": "Computer science"}],
                    "keywords": [{"display_name": "AIOps"}],
                }
            ]
        }
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            return FakeHttpResponse(payload)

        source = OpenAlexSource(request_delay=0, retries=0, timeout=14, verbose=False)
        with patch("paper_agents.scout.urllib.request.urlopen", fake_urlopen):
            candidates = source.fetch(["AIOps"], max_results=3, freshness_months=24)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source, "openalex")
        self.assertEqual(candidates[0].source_id, "W123456789")
        self.assertEqual(candidates[0].doi, "https://doi.org/10.5555/openalex")
        self.assertEqual(candidates[0].pdf_url, "https://example.test/openalex.pdf")
        self.assertEqual(candidates[0].metadata["query_topic"], "AIOps")
        self.assertIn("api.openalex.org", captured["url"])
        parsed = urllib.parse.urlparse(captured["url"])
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(query["search"], ["AIOps software cloud operations observability"])
        self.assertEqual(query["per_page"], ["3"])
        self.assertEqual(query["sort"], ["publication_date:desc"])
        self.assertIn("from_publication_date:", query["filter"][0])
        self.assertIn("type:article|preprint|posted-content|report", query["filter"][0])
        self.assertNotIn("select", query)
        self.assertEqual(captured["timeout"], 14)

    def test_openalex_filters_noisy_off_domain_records(self):
        payload = {
            "results": [
                {"id": "https://openalex.org/W-index", "display_name": "Index", "type": "book-chapter"},
                {
                    "id": "https://openalex.org/W-brain",
                    "display_name": "Volitional deep brain stimulation following brain-computer interface training for Parkinson's disease",
                    "type": "article",
                    "abstract_inverted_index": {"Parkinson": [0], "patient": [1], "clinical": [2]},
                },
                {
                    "id": "https://openalex.org/W-aiops",
                    "display_name": "AIOps Root Cause Analysis for Cloud Incidents",
                    "type": "article",
                    "abstract_inverted_index": {"Root": [0], "cause": [1], "analysis": [2], "for": [3], "cloud": [4], "incidents": [5]},
                    "concepts": [{"display_name": "Software engineering"}],
                    "keywords": [{"display_name": "AIOps"}],
                },
            ]
        }

        def fake_urlopen(request, timeout):
            return FakeHttpResponse(payload)

        source = OpenAlexSource(request_delay=0, retries=0, timeout=14, verbose=False)
        with patch("paper_agents.scout.urllib.request.urlopen", fake_urlopen):
            candidates = source.fetch(["microservice diagnosis"], max_results=3, freshness_months=24)

        self.assertEqual([candidate.source_id for candidate in candidates], ["W-aiops"])
        self.assertEqual(source.last_diagnostics["raw_count"], 3)
        self.assertEqual(source.last_diagnostics["rejected_reasons"]["excluded_type:book-chapter"], 1)
        self.assertEqual(source.last_diagnostics["rejected_reasons"]["off_domain_biomedical"], 1)

    def test_openalex_rejection_keeps_biomedical_record_with_ops_context(self):
        reason = openalex_rejection_reason(
            {
                "display_name": "Clinical incident response observability platform",
                "type": "article",
                "abstract_inverted_index": {
                    "Healthcare": [0],
                    "incident": [1],
                    "response": [2],
                    "observability": [3],
                    "cloud": [4],
                },
            }
        )

        self.assertIsNone(reason)

    def test_openalex_http_error_message_includes_response_body(self):
        error = urllib.error.HTTPError(
            "https://api.openalex.org/works",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"message":"Invalid query parameter: select"}'),
        )

        try:
            self.assertIn("Invalid query parameter", openalex_http_error_message(error))
        finally:
            error.close()

    def test_scout_daily_cli_selects_openalex_source(self):
        captured = {}

        def fake_run_daily_scout(**kwargs):
            captured["source"] = kwargs["source"]
            return {"source": kwargs["source"].name, "candidates": []}

        with patch("paper_agents.cli.run_daily_scout", fake_run_daily_scout):
            with patch("paper_agents.cli.print_section"):
                with patch(
                    "sys.argv",
                    [
                        "paper_agents.cli",
                        "scout-daily",
                        "--source",
                        "openalex",
                        "--fetch",
                        "1",
                        "--no-download",
                        "--db",
                        str(self.db_path),
                    ],
                ):
                    cli.main()

        self.assertEqual(captured["source"].name, "openalex")

    def test_domain_filter_penalizes_physical_incident_domains(self):
        software = ScoutCandidate(
            source="arxiv",
            source_id="2601.softwarev1",
            title="LLM failure diagnosis for microservice incident response",
            abstract="Telemetry analysis and log analysis for cloud operations and production engineering.",
            authors=[],
            published="2026-01-01",
            updated=None,
            url="https://example.test/software",
            pdf_url=None,
            categories=["cs.SE"],
        )
        physical = ScoutCandidate(
            source="arxiv",
            source_id="2601.physicalv1",
            title="Root cause analysis for railway traffic incident management",
            abstract="Vehicular transportation incident mitigation for road and smart grid operations.",
            authors=[],
            published="2026-01-01",
            updated=None,
            url="https://example.test/physical",
            pdf_url=None,
            categories=["cs.CY"],
        )

        ranked = rank_candidates([physical, software], DEFAULT_SCOUT_TOPICS)

        self.assertEqual(ranked[0].source_id, "2601.softwarev1")
        self.assertLess(physical.score, software.score)
        self.assertIn("penalized off-domain terms", physical.ranking_reason or "")

    def test_curator_penalizes_off_domain_incident_papers(self):
        profile = {
            "interests": ["incident management", "root cause analysis", "failure diagnosis"],
            "positive_signals": ["cloud operations", "microservice diagnosis"],
            "negative_signals": [],
        }
        software = evaluate_candidate(
            {
                "paper_id": 1,
                "title": "Microservice root cause analysis for cloud incident response",
                "abstract": "LLM failure diagnosis over logs, traces, and telemetry for production engineering.",
            },
            profile,
        )
        physical = evaluate_candidate(
            {
                "paper_id": 2,
                "title": "Railway traffic incident root cause analysis",
                "abstract": "Vehicular transportation incident mitigation for road operations and smart grid failures.",
            },
            profile,
        )

        self.assertGreater(software["score"], 0)
        self.assertLess(physical["score"], software["score"])
        self.assertIn("off-domain signals", physical["rationale"])

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

    def test_scout_candidate_record_shape_stays_metadata_only(self):
        ranked = rank_candidates([candidate("2601.shapev1", "Debugging shape check")], DEFAULT_SCOUT_TOPICS)
        record = scout_candidate_record(ranked[0])
        self.assertIn("source_id", record)
        self.assertIn("metadata", record)
        self.assertNotIn("score", record)
        self.assertNotIn("matched_keywords", record)
        self.assertNotIn("ranking_reason", record)
        self.assertNotIn("selected", record)

    def test_previously_discovered_paper_is_excluded_on_later_scout_run(self):
        source = FakeSource([candidate("2601.1v1", "Incident RCA")])
        agent = ScoutAgent(source=source)
        config = ScoutConfig(topics=["AIOps"], max_candidates=5)
        first = agent.run(self.connection, workflow_cycle_id=self.cycle_id, attempt_number=1, config=config)
        second = agent.run(self.connection, workflow_cycle_id=self.cycle_id, attempt_number=2, config=config)
        self.assertEqual(first["eligible_count"], 1)
        self.assertEqual(second["eligible_count"], 1)
        row = self.connection.execute(
            """
            SELECT excluded, exclusion_reason
            FROM scout_candidates
            WHERE scout_run_id = ?
            """,
            (second["scout_run_id"],),
        ).fetchone()
        self.assertEqual(row, (0, None))

    def test_previously_discovered_paper_from_prior_cycle_is_excluded(self):
        source = FakeSource([candidate("2601.priorv1", "Incident RCA")])
        config = ScoutConfig(topics=["AIOps"], max_candidates=5)
        first = ScoutAgent(source=source).run(self.connection, workflow_cycle_id=self.cycle_id, attempt_number=1, config=config)
        next_cycle_id = db.create_workflow_cycle(self.connection, mode="test", max_scout_attempts=1)

        second = ScoutAgent(source=source).run(self.connection, workflow_cycle_id=next_cycle_id, attempt_number=1, config=config)

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


    def test_reviewer_card_uses_extraction_summary_fields(self):
        card = card_from_recommendation(
            {
                "paper_id": 42,
                "title": "Interesting Ops Paper",
                "url": "https://example.test/paper",
                "published": "2026-07-30",
                "score": 72.5,
                "rationale": "Strong production operations match.",
            },
            index=1,
            pdf_path=Path("data/papers/example.pdf"),
            output_path=Path("data/extractions/example.summary.json"),
            extraction={
                "merged": {
                    "paper_date": "2026-07-29",
                    "research_problem": "Automating incident review",
                    "why_it_matters": "It saves on-call time",
                    "approach": "Local triage extraction",
                },
                "merge_strategy": "synthesis",
                "chunk_count": 3,
            },
        )
        self.assertEqual(card["recommendation_order"], 1)
        self.assertEqual(card["paper_date"], "2026-07-29")
        self.assertEqual(card["research_problem"], "Automating incident review")
        self.assertEqual(card["why_it_matters"], "It saves on-call time")
        self.assertEqual(card["approach"], "Local triage extraction")
        self.assertEqual(card["merge_strategy"], "synthesis")
        self.assertEqual(card["chunk_count"], 3)

    def test_reviewer_backfill_finds_recommended_papers_missing_triage(self):
        missing_paper_id, missing_recommendation_id = self._seed_review_recommendation(source_id="2607.missingv1")
        complete_paper_id, _ = self._seed_review_recommendation(source_id="2607.completev1")
        db.insert_artifact(
            self.connection,
            missing_paper_id,
            artifact_type="pdf",
            path=Path("data/papers/arxiv/2607.missingv1.pdf"),
        )
        db.insert_artifact(
            self.connection,
            complete_paper_id,
            artifact_type="pdf",
            path=Path("data/papers/arxiv/2607.completev1.pdf"),
        )
        db.insert_artifact(
            self.connection,
            complete_paper_id,
            artifact_type="triage_summary",
            path=Path("data/extractions/arxiv/2607.completev1.summary.json"),
        )

        rows = recommended_papers_missing_triage(self.connection)

        self.assertEqual([row["paper_id"] for row in rows], [missing_paper_id])
        self.assertEqual(rows[0]["recommendation_id"], missing_recommendation_id)


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

    def test_feedback_blob_ingestion_stores_raw_blob_exactly_and_structured_parse(self):
        paper_id, recommendation_id = self._seed_review_recommendation()
        content = "Decision: keep\nScore: 5\nUseful because it studies real incidents.\n"

        output = ingest_feedback_blob(
            self.connection,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            content=content,
            source="test",
            status="not_interested",
        )

        raw = self.connection.execute(
            "SELECT paper_id, recommendation_id, content FROM raw_feedback WHERE id = ?",
            (output["raw_feedback_id"],),
        ).fetchone()
        parse_attempt = self.connection.execute(
            "SELECT raw_feedback_id, parser_name, parser_version, status, output_json FROM feedback_parse_attempts WHERE id = ?",
            (output["parse_attempt_id"],),
        ).fetchone()
        structured = self.connection.execute(
            "SELECT paper_id, decision, score, observations_json, preference_signals_json FROM structured_feedback WHERE id = ?",
            (output["structured_feedback_id"],),
        ).fetchone()

        self.assertEqual(raw, (paper_id, recommendation_id, content))
        self.assertEqual(parse_attempt[:4], (output["raw_feedback_id"], "feedback-agent", "deterministic-v1", "succeeded"))
        self.assertEqual(json.loads(parse_attempt[4])["decision"], "keep")
        self.assertEqual(structured[0:3], (paper_id, "keep", 5))
        self.assertEqual(json.loads(structured[3]), ["Useful because it studies real incidents."])
        self.assertEqual(json.loads(structured[4]), [])

    def test_feedback_blob_duplicate_dedupes_raw_feedback_but_records_attempt(self):
        paper_id, recommendation_id = self._seed_review_recommendation()
        content = "Decision: reject\nScore: 2\nToo theoretical."

        first = ingest_feedback_blob(
            self.connection,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            content=content,
            source="test",
            status=None,
        )
        second = ingest_feedback_blob(
            self.connection,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            content=content,
            source="test",
            status=None,
        )

        self.assertTrue(first["raw_feedback_created"])
        self.assertFalse(second["raw_feedback_created"])
        self.assertEqual(first["raw_feedback_id"], second["raw_feedback_id"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM raw_feedback").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM feedback_parse_attempts").fetchone()[0], 2)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM structured_feedback").fetchone()[0], 2)

    def test_feedback_blob_derives_decision_from_status_when_blob_has_no_decision(self):
        paper_id, recommendation_id = self._seed_review_recommendation()

        output = ingest_feedback_blob(
            self.connection,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            content="This is useful later, but not urgent.",
            source="test",
            status="read_later",
        )

        structured = self.connection.execute(
            "SELECT decision, score FROM structured_feedback WHERE id = ?",
            (output["structured_feedback_id"],),
        ).fetchone()
        self.assertEqual(structured, ("maybe", None))

    def test_feedback_add_cli_ingests_file_content(self):
        paper_id, recommendation_id = self._seed_review_recommendation()
        feedback_path = Path(self.tmp.name) / "feedback.txt"
        feedback_path.write_text("Decision: reject\nScore: 1\nNot useful.\n", encoding="utf-8")
        self.connection.commit()

        output = run_feedback_add(
            [
                "--db",
                str(self.db_path),
                "--paper-id",
                str(paper_id),
                "--recommendation-id",
                str(recommendation_id),
                "--status",
                "not_interested",
                "--file",
                str(feedback_path),
            ]
        )

        structured = self.connection.execute(
            "SELECT decision, score FROM structured_feedback WHERE id = ?",
            (output["structured_feedback_id"],),
        ).fetchone()
        raw = self.connection.execute(
            "SELECT content FROM raw_feedback WHERE id = ?",
            (output["raw_feedback_id"],),
        ).fetchone()
        self.assertEqual(structured, ("reject", 1))
        self.assertEqual(raw[0], "Decision: reject\nScore: 1\nNot useful.\n")

    def test_unapplied_structured_feedback_excludes_applied_rows(self):
        paper_id, recommendation_id = self._seed_review_recommendation()
        first = ingest_feedback_blob(
            self.connection,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            content="Decision: keep\nScore: 5\nUseful.",
            source="test",
        )
        second = ingest_feedback_blob(
            self.connection,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            content="Decision: maybe\nScore: 3\nMaybe useful.",
            source="test",
        )

        rows = db.unapplied_structured_feedback(self.connection)
        self.assertEqual([row["id"] for row in rows], [first["structured_feedback_id"], second["structured_feedback_id"]])

        db.create_feedback_profile_applications(self.connection, [first["structured_feedback_id"]], self.profile_id)
        rows = db.unapplied_structured_feedback(self.connection)
        self.assertEqual([row["id"] for row in rows], [second["structured_feedback_id"]])

    def test_feedback_profile_dry_run_does_not_write_profile_or_applications(self):
        paper_id, recommendation_id = self._seed_review_recommendation()
        feedback = ingest_feedback_blob(
            self.connection,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            content="Decision: keep\nScore: 5\nVery applied.",
            source="test",
        )
        calls = []

        def provider(payload, model):
            calls.append((payload, model))
            return {
                "profile": {
                    "interests": ["incident management"],
                    "positive_signals": ["real incidents"],
                    "negative_signals": ["toy benchmarks"],
                    "notes": "Prefers applied work.",
                },
                "change_summary": "Added applied incident preference.",
            }

        output = apply_feedback_to_profile(self.connection, dry_run=True, model="gemini-test", provider_fn=provider)

        self.assertEqual(output["status"], "dry_run")
        self.assertEqual(output["structured_feedback_ids"], [feedback["structured_feedback_id"]])
        self.assertEqual(output["change_summary"], "Added applied incident preference.")
        self.assertEqual(calls[0][1], "gemini-test")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM profile_versions").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM feedback_profile_applications").fetchone()[0], 0)
        attempt = self.connection.execute(
            "SELECT provider, model, dry_run, status, profile_version_id FROM feedback_profile_apply_attempts"
        ).fetchone()
        self.assertEqual(attempt, ("gemini", "gemini-test", 1, "succeeded", None))

    def test_gemini_timeout_is_configurable_by_environment(self):
        with patch.dict("os.environ", {"PAPER_AGENT_GEMINI_TIMEOUT_SECONDS": "240"}):
            self.assertEqual(gemini_timeout_seconds(), 240)

        with patch.dict("os.environ", {"PAPER_AGENT_GEMINI_TIMEOUT_SECONDS": "not-a-number"}):
            self.assertEqual(gemini_timeout_seconds(), 180)

    def test_call_gemini_json_uses_configured_timeout(self):
        payload = {"current_profile": {}, "structured_feedback": []}
        with patch.dict("os.environ", {"PAPER_AGENT_GEMINI_TIMEOUT_SECONDS": "240"}):
            with patch("paper_agents.feedback.subprocess.run") as run:
                run.return_value.stdout = '{"profile":{"interests":[],"positive_signals":[],"negative_signals":[],"notes":""},"change_summary":"ok"}'
                call_gemini_json(payload)

        self.assertEqual(run.call_args.kwargs["timeout"], 240)

    def test_call_gemini_json_timeout_error_includes_timeout(self):
        payload = {"current_profile": {}, "structured_feedback": []}
        with patch.dict("os.environ", {"PAPER_AGENT_GEMINI_TIMEOUT_SECONDS": "240"}):
            with patch("paper_agents.feedback.subprocess.run", side_effect=subprocess.TimeoutExpired(["gemini"], 240)):
                with self.assertRaisesRegex(RuntimeError, "timed out after 240 seconds"):
                    call_gemini_json(payload)

    def test_feedback_profile_apply_creates_profile_version_and_application_rows(self):
        paper_id, recommendation_id = self._seed_review_recommendation()
        first = ingest_feedback_blob(
            self.connection,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            content="Decision: keep\nScore: 5\nVery applied.",
            source="test",
        )
        second = ingest_feedback_blob(
            self.connection,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            content="Decision: reject\nScore: 1\nToo synthetic.",
            source="test",
        )

        def provider(payload, model):
            self.assertEqual([row["id"] for row in payload["structured_feedback"]], [first["structured_feedback_id"], second["structured_feedback_id"]])
            return {
                "profile": {
                    "interests": ["production incidents"],
                    "positive_signals": ["field evidence"],
                    "negative_signals": ["synthetic-only evaluation"],
                    "notes": "Updated from two feedback rows.",
                },
                "change_summary": "Prefer field evidence and reject synthetic-only work.",
            }

        output = apply_feedback_to_profile(self.connection, dry_run=False, provider_fn=provider)

        self.assertEqual(output["status"], "applied")
        self.assertEqual(output["structured_feedback_ids"], [first["structured_feedback_id"], second["structured_feedback_id"]])
        current = db.current_profile_version(self.connection)
        applications = self.connection.execute(
            "SELECT structured_feedback_id, profile_version_id FROM feedback_profile_applications ORDER BY structured_feedback_id"
        ).fetchall()
        self.assertEqual(current["id"], output["profile_version_id"])
        self.assertEqual(current["profile"]["interests"], ["production incidents"])
        self.assertEqual(current["change_summary"], "Prefer field evidence and reject synthetic-only work.")
        self.assertEqual(
            applications,
            [
                (first["structured_feedback_id"], output["profile_version_id"]),
                (second["structured_feedback_id"], output["profile_version_id"]),
            ],
        )
        attempt = self.connection.execute(
            "SELECT dry_run, status, error, profile_version_id FROM feedback_profile_apply_attempts"
        ).fetchone()
        self.assertEqual(attempt, (0, "succeeded", None, output["profile_version_id"]))
        self.assertEqual(db.unapplied_structured_feedback(self.connection), [])

    def test_feedback_profile_apply_failure_records_attempt_and_remains_retryable(self):
        paper_id, recommendation_id = self._seed_review_recommendation()
        feedback = ingest_feedback_blob(
            self.connection,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            content="Decision: keep\nScore: 5\nVery applied.",
            source="test",
        )

        def provider(payload, model):
            raise RuntimeError("provider unavailable")

        output = apply_feedback_to_profile(self.connection, dry_run=False, provider_fn=provider)

        attempt = self.connection.execute(
            """
            SELECT status, error, profile_version_id, structured_feedback_ids_json
            FROM feedback_profile_apply_attempts
            """
        ).fetchone()
        self.assertEqual(output["status"], "failed")
        self.assertEqual(output["error"], "provider unavailable")
        self.assertEqual(attempt[0:3], ("failed", "provider unavailable", None))
        self.assertEqual(json.loads(attempt[3]), [feedback["structured_feedback_id"]])
        self.assertEqual([row["id"] for row in db.unapplied_structured_feedback(self.connection)], [feedback["structured_feedback_id"]])

    def test_feedback_profile_no_feedback_does_not_call_provider(self):
        calls = []

        def provider(payload, model):
            calls.append(payload)
            return {"profile": {}, "change_summary": "Should not happen"}

        output = apply_feedback_to_profile(self.connection, dry_run=True, provider_fn=provider)

        self.assertEqual(output["status"], "no_feedback")
        self.assertEqual(output["structured_feedback_ids"], [])
        self.assertEqual(calls, [])

    def test_feedback_apply_cli_no_feedback_exits_cleanly(self):
        self.connection.commit()

        output = run_feedback_apply(["--db", str(self.db_path), "--dry-run"])

        self.assertEqual(output["status"], "no_feedback")
        self.assertEqual(output["message"], "No structured feedback rows found for profile apply.")

    def test_feedback_profile_rebuild_dry_run_reads_all_feedback_without_writes(self):
        paper_id, recommendation_id = self._seed_review_recommendation()
        first = ingest_feedback_blob(
            self.connection,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            content="Decision: keep\nScore: 5\nVery applied.",
            source="test",
        )
        second = ingest_feedback_blob(
            self.connection,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            content="Decision: reject\nScore: 1\nToo synthetic.",
            source="test",
        )
        db.create_feedback_profile_applications(self.connection, [first["structured_feedback_id"]], self.profile_id)
        calls = []

        def provider(payload, model):
            calls.append(payload)
            return {
                "profile": {
                    "interests": ["rebuilt profile"],
                    "positive_signals": ["applied"],
                    "negative_signals": ["synthetic"],
                    "notes": "Compressed.",
                },
                "change_summary": "Rebuilt from all feedback.",
            }

        output = rebuild_feedback_profile(self.connection, dry_run=True, provider_fn=provider)

        self.assertEqual(output["status"], "dry_run")
        self.assertEqual(output["structured_feedback_ids"], [first["structured_feedback_id"], second["structured_feedback_id"]])
        self.assertEqual([row["id"] for row in calls[0]["structured_feedback"]], [first["structured_feedback_id"], second["structured_feedback_id"]])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM profile_versions").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM feedback_profile_applications").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM feedback_profile_apply_attempts").fetchone()[0], 1)

    def test_feedback_profile_rebuild_apply_creates_new_active_profile_without_application_rows(self):
        paper_id, recommendation_id = self._seed_review_recommendation()
        ingest_feedback_blob(
            self.connection,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            content="Decision: keep\nScore: 5\nVery applied.",
            source="test",
        )

        def provider(payload, model):
            return {
                "profile": {
                    "interests": ["rebuilt profile"],
                    "positive_signals": ["applied"],
                    "negative_signals": ["synthetic"],
                    "notes": "Compressed.",
                },
                "change_summary": "Full profile rebuild.",
            }

        output = rebuild_feedback_profile(self.connection, dry_run=False, provider_fn=provider)

        current = db.current_profile_version(self.connection)
        self.assertEqual(output["status"], "rebuilt")
        self.assertEqual(current["id"], output["profile_version_id"])
        self.assertEqual(current["profile"]["interests"], ["rebuilt profile"])
        self.assertEqual(current["change_summary"], "Full profile rebuild.")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM feedback_profile_applications").fetchone()[0], 0)
        attempt = self.connection.execute(
            "SELECT status, profile_version_id FROM feedback_profile_apply_attempts"
        ).fetchone()
        self.assertEqual(attempt, ("succeeded", output["profile_version_id"]))

    def test_feedback_rebuild_profile_cli_no_feedback_exits_cleanly(self):
        self.connection.commit()

        output = run_feedback_rebuild_profile(["--db", str(self.db_path), "--dry-run"])

        self.assertEqual(output["status"], "no_feedback")
        self.assertEqual(output["message"], "No structured feedback rows found for profile rebuild.")

    def test_review_queue_renders_dense_feedback_controls(self):
        self._seed_review_recommendation()
        self.connection.commit()

        html = web.render_review_queue(self.db_path)

        self.assertIn('<form method="get" action="/" class="queue-controls">', html)
        self.assertIn('<select name="filter"', html)
        self.assertIn('<select name="source"', html)
        self.assertIn('<select name="sort"', html)
        self.assertIn('<select name="view"', html)
        self.assertIn("All sources", html)
        self.assertIn("arXiv", html)
        self.assertIn('href="/health"', html)
        self.assertIn('class="source-badge source-badge-arxiv"', html)
        self.assertIn('class="action-rail"', html)
        self.assertIn('<h1 class="brand-title">', html)
        self.assertIn('srcset="/assets/logo_dark.png"', html)
        self.assertIn('src="/assets/logo_light.png"', html)
        self.assertIn('<span>System</span><strong>72.5</strong>', html)
        self.assertIn('data-copy-value="https://example.test/2607.reviewv1"', html)
        self.assertIn("Feedback<textarea name=\"notes\">", html)
        self.assertNotIn("No ChatGPT review yet", html)
        self.assertNotIn("Copy prompt/link", html)
        self.assertNotIn("<span>score</span>", html)
        self.assertNotIn(">Notes<textarea", html)

    def test_review_queue_shows_user_feedback_score_prominently(self):
        paper_id, recommendation_id = self._seed_review_recommendation()
        ingest_feedback_blob(
            self.connection,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            content="Decision: maybe\nScore: 2\nUseful but not urgent.",
            source="test",
            status="read_later",
        )
        self.connection.commit()

        html = web.render_review_queue(self.db_path)

        self.assertIn('<div class="user-score"><span>Your score</span><strong>2/5</strong></div>', html)
        self.assertIn('class="score score-secondary"', html)
        self.assertIn('<span>System</span><strong>72.5</strong>', html)

    def test_source_badge_class_distinguishes_sources(self):
        self.assertEqual(web.source_badge_class("arxiv"), "source-badge-arxiv")
        self.assertEqual(web.source_badge_class("openalex"), "source-badge-openalex")
        self.assertEqual(web.source_badge_class("semantic_scholar"), "source-badge-semantic-scholar")
        self.assertEqual(web.source_badge_class("custom_source"), "source-badge-unknown")

    def test_review_queue_filters_by_source_and_shows_multi_source_label(self):
        paper_id, _ = self._seed_review_recommendation()
        db.upsert_paper_source(
            self.connection,
            paper_id,
            {
                "source": "openalex",
                "source_id": "W123",
                "url": "https://openalex.org/W123",
            },
        )
        other_paper_id, _ = self._seed_review_recommendation(source_id="2607.semanticv1")
        db.upsert_paper_source(
            self.connection,
            other_paper_id,
            {
                "source": "semantic_scholar",
                "source_id": "S123",
                "url": "https://www.semanticscholar.org/paper/S123",
            },
        )
        self.connection.commit()

        html = web.render_review_queue(self.db_path, source_value="openalex")

        self.assertIn('<option value="openalex" selected>OpenAlex</option>', html)
        self.assertIn("arXiv +1", html)
        self.assertIn("Dense Review Paper", html)
        self.assertNotIn("2607.semanticv1", html)

    def test_review_queue_uses_source_abstract_when_triage_summary_missing(self):
        self._seed_review_recommendation()
        self.connection.commit()

        html = web.render_review_queue(self.db_path)

        self.assertIn("Source Abstract", html)
        self.assertIn("Applied incident review automation.", html)

    def test_review_queue_feedback_save_still_inserts_status_and_notes(self):
        paper_id, _ = self._seed_review_recommendation()
        self.connection.commit()

        web.save_feedback(self.db_path, paper_id=paper_id, status="reviewed", notes="Dense feedback blob")

        row = self.connection.execute(
            "SELECT status, notes FROM feedback WHERE paper_id = ? ORDER BY id DESC",
            (paper_id,),
        ).fetchone()
        self.assertEqual(row, ("reviewed", "Dense feedback blob"))

    def test_review_queue_quick_status_controls_are_status_only(self):
        self._seed_review_recommendation()
        self.connection.commit()

        html = web.render_review_queue(self.db_path)

        self.assertIn('name="action" value="feedback"', html)
        self.assertIn('data-quick-status="1">Read later</button>', html)
        self.assertIn('<span class="submit-state" aria-live="polite"></span>', html)

    def test_review_queue_status_only_save_does_not_ingest_existing_notes(self):
        paper_id, recommendation_id = self._seed_review_recommendation()
        self.connection.commit()
        calls = []

        result = web.save_feedback(
            self.db_path,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            status="read_later",
            notes="Decision: keep\nScore: 5\nExisting pasted feedback.",
            feedback_content="",
            profile_provider_fn=lambda payload, model: calls.append(payload),
        )

        self.assertTrue(result["feedback_saved"])
        self.assertFalse(result["feedback_ingested"])
        self.assertEqual(calls, [])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM feedback WHERE paper_id = ?", (paper_id,)).fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM raw_feedback WHERE paper_id = ?", (paper_id,)).fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM structured_feedback").fetchone()[0], 0)

    def test_review_queue_feedback_save_ingests_non_empty_feedback_blob(self):
        paper_id, recommendation_id = self._seed_review_recommendation()
        self.connection.commit()

        result = web.save_feedback(
            self.db_path,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            status="interested",
            notes="Decision: keep\nScore: 4\nGood fit.",
            feedback_content="Decision: keep\nScore: 4\nGood fit.\n",
            source="review_queue_ui",
            profile_provider_fn=lambda payload, model: {
                "profile": {
                    "interests": ["auto-applied"],
                    "positive_signals": ["good fit"],
                    "negative_signals": [],
                    "notes": "Applied from UI.",
                },
                "change_summary": "Auto-applied UI feedback.",
            },
        )

        raw = self.connection.execute("SELECT content FROM raw_feedback WHERE paper_id = ?", (paper_id,)).fetchone()
        structured = self.connection.execute("SELECT decision, score FROM structured_feedback WHERE paper_id = ?", (paper_id,)).fetchone()
        lightweight = self.connection.execute("SELECT status, notes FROM feedback WHERE paper_id = ?", (paper_id,)).fetchone()
        current = db.current_profile_version(self.connection)
        self.assertEqual(raw[0], "Decision: keep\nScore: 4\nGood fit.\n")
        self.assertEqual(structured, ("keep", 4))
        self.assertEqual(lightweight, ("interested", "Decision: keep\nScore: 4\nGood fit."))
        self.assertEqual(result["profile_apply"]["status"], "applied")
        self.assertEqual(current["profile"]["interests"], ["auto-applied"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM feedback_profile_applications").fetchone()[0], 1)

    def test_review_queue_feedback_save_preserves_feedback_when_profile_apply_fails(self):
        paper_id, recommendation_id = self._seed_review_recommendation()
        self.connection.commit()

        def provider(payload, model):
            raise RuntimeError("provider unavailable")

        result = web.save_feedback(
            self.db_path,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            status="interested",
            notes="Decision: keep\nScore: 4\nGood fit.",
            feedback_content="Decision: keep\nScore: 4\nGood fit.\n",
            source="review_queue_ui",
            profile_provider_fn=provider,
        )

        self.assertEqual(result["profile_apply_error"], "provider unavailable")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM feedback WHERE paper_id = ?", (paper_id,)).fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM raw_feedback WHERE paper_id = ?", (paper_id,)).fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM structured_feedback WHERE paper_id = ?", (paper_id,)).fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM profile_versions").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM feedback_profile_applications").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT status, error FROM feedback_profile_apply_attempts").fetchone(), ("failed", "provider unavailable"))
        self.assertEqual(len(db.unapplied_structured_feedback(self.connection)), 1)

    def test_review_queue_empty_feedback_does_not_apply_profile(self):
        paper_id, recommendation_id = self._seed_review_recommendation()
        self.connection.commit()
        calls = []

        result = web.save_feedback(
            self.db_path,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            status="reviewed",
            notes="",
            feedback_content="",
            profile_provider_fn=lambda payload, model: calls.append(payload),
        )

        self.assertTrue(result["feedback_saved"])
        self.assertFalse(result["feedback_ingested"])
        self.assertEqual(calls, [])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM feedback WHERE paper_id = ?", (paper_id,)).fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM structured_feedback").fetchone()[0], 0)

    def test_review_queue_logo_assets_are_checked_in(self):
        self.assertTrue((web.ASSET_DIR / "logo_light.png").is_file())
        self.assertTrue((web.ASSET_DIR / "logo_dark.png").is_file())
        self.assertIn("logo_light.png", web.LOGO_ASSETS)
        self.assertIn("logo_dark.png", web.LOGO_ASSETS)

    def test_health_summary_empty_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty_db = Path(tmp) / "paper_agent.db"
            db.init_db(empty_db)

            summary = db.health_summary(empty_db, days=21)

        self.assertEqual(summary["db"]["integrity"], "ok")
        self.assertIsNone(summary["latest_cycle"])
        self.assertEqual(summary["top"]["papers_waiting_in_queue"], 0)
        self.assertTrue(any("No workflow cycle" in warning["message"] for warning in summary["warnings"]))

    def test_health_summary_healthy_run_with_artifacts(self):
        paper_id, _ = self._seed_review_recommendation()
        db.insert_artifact(self.connection, paper_id, artifact_type="pdf", path=Path("data/papers/test.pdf"))
        db.insert_artifact(
            self.connection,
            paper_id,
            artifact_type="triage_summary",
            path=Path("data/extractions/test.summary.json"),
        )
        db.update_workflow_state(self.connection, self.cycle_id, "recommendations_ready")
        self.connection.commit()

        summary = db.health_summary(self.db_path, days=21)

        self.assertEqual(summary["db"]["integrity"], "ok")
        self.assertEqual(summary["artifact_health"]["recommendation_count"], 1)
        self.assertEqual(summary["artifact_health"]["pdf_count"], 1)
        self.assertEqual(summary["artifact_health"]["triage_summary_count"], 1)
        self.assertEqual(summary["top"]["papers_waiting_in_queue"], 1)

    def test_health_summary_warns_on_zero_eligible_scout_run(self):
        self._seed_scout_candidate(source="openalex", excluded=True, exclusion_reason="already_seen")
        self.connection.commit()

        summary = db.health_summary(self.db_path, days=21, source="openalex")

        self.assertEqual(summary["top"]["latest_zero_eligible_source"], "openalex")
        self.assertTrue(any("0 eligible" in warning["message"] for warning in summary["warnings"]))
        self.assertEqual(summary["source_breakdown"]["exclusion_reasons"][0]["reason"], "already_seen")

    def test_health_summary_source_split_and_filter(self):
        self._seed_scout_candidate(source="arxiv", excluded=False, source_id="2607.sourcev1")
        self._seed_scout_candidate(source="openalex", excluded=True, source_id="W-source", exclusion_reason="history")
        self.connection.commit()

        all_summary = db.health_summary(self.db_path, days=21)
        openalex_summary = db.health_summary(self.db_path, days=21, source="openalex")

        all_sources = {row["source"] for row in all_summary["source_breakdown"]["funnel"]}
        filtered_sources = {row["source"] for row in openalex_summary["daily"]["scout"]}
        self.assertIn("arxiv", all_sources)
        self.assertIn("openalex", all_sources)
        self.assertEqual(filtered_sources, {"openalex"})

    def test_health_summary_reports_feedback_profile_warnings(self):
        paper_id, recommendation_id = self._seed_review_recommendation()
        ingest = ingest_feedback_blob(
            self.connection,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            content="Decision: keep\nScore: 5\nGreat operational fit.",
            source="test",
            status="interested",
        )
        db.create_feedback_profile_apply_attempt(
            self.connection,
            provider="gemini",
            model=None,
            structured_feedback_ids=[ingest["structured_feedback_id"]],
            dry_run=False,
            status="failed",
            error="provider unavailable",
        )
        self.connection.commit()

        summary = db.health_summary(self.db_path, days=21)

        self.assertEqual(summary["feedback_profile"]["unapplied_structured_feedback_count"], 1)
        self.assertEqual(summary["feedback_profile"]["recent_apply_failures_count"], 1)
        messages = [warning["message"] for warning in summary["warnings"]]
        self.assertTrue(any("Gemini/profile apply failed" in message for message in messages))
        self.assertTrue(any("Structured feedback is waiting" in message for message in messages))

    def test_health_page_renders_dashboard_controls(self):
        self._seed_scout_candidate(source="openalex", excluded=True, exclusion_reason="history")
        self.connection.commit()

        html = web.render_health_page(self.db_path, days=21, source_value="openalex")

        self.assertIn("Project Paper Health", html)
        self.assertIn('<form method="get" action="/health"', html)
        self.assertIn('href="/">Review queue</a>', html)
        self.assertIn('<select name="days"', html)
        self.assertIn('<option value="openalex" selected>OpenAlex</option>', html)
        self.assertIn('class="health-graphs"', html)
        self.assertIn('class="health-svg daily-trend-svg"', html)
        self.assertIn('class="health-svg source-breakdown-svg"', html)
        self.assertIn('class="health-svg recommendation-gap-svg"', html)
        self.assertIn("Candidates -> eligible -> recommendations", html)
        self.assertIn("Daily Trend", html)
        self.assertIn("Source Breakdown", html)
        self.assertIn("Recommendation Gap", html)
        self.assertIn("Feedback/Profile Activity", html)

    def _seed_review_recommendation(self, *, source_id: str = "2607.reviewv1") -> tuple[int, int]:
        paper_id, _ = db.upsert_paper(
            self.connection,
            {
                "source": "arxiv",
                "source_id": source_id,
                "title": "Dense Review Paper",
                "url": f"https://example.test/{source_id}",
                "published": "2026-07-30",
                "abstract": "Applied incident review automation.",
            },
        )
        curator_run_id = db.create_curator_run(
            self.connection,
            workflow_cycle_id=self.cycle_id,
            profile_version_id=self.profile_id,
            scout_attempt_count=1,
            max_scout_attempts=3,
            min_quality_score=25,
            max_recommendations=3,
            model="test",
        )
        db.insert_curator_evaluation(
            self.connection,
            curator_run_id=curator_run_id,
            paper_id=paper_id,
            scout_candidate_id=None,
            score=72.5,
            rationale="Strong match.",
            matched_signals=["incident", "automation"],
            quality_threshold_met=True,
        )
        recommendation_id = db.insert_recommendation(
            self.connection,
            curator_run_id=curator_run_id,
            paper_id=paper_id,
            recommendation_order=1,
            rationale="Strong match.",
        )
        return paper_id, recommendation_id

    def _seed_scout_candidate(
        self,
        *,
        source: str,
        excluded: bool,
        source_id: str = "health-source",
        exclusion_reason: str | None = None,
    ) -> tuple[int, int, int]:
        scout_run_id = db.insert_scout_run(
            self.connection,
            workflow_cycle_id=self.cycle_id,
            attempt_number=1,
            source=source,
            target_candidates=1,
            max_candidates=1,
            freshness_months=24,
            topics=["microservice diagnosis"],
            guidance_id=None,
        )
        paper_id, is_new = db.upsert_paper(
            self.connection,
            {
                "source": source,
                "source_id": source_id,
                "title": f"{source} health paper",
                "url": f"https://example.test/{source_id}",
                "published": "2026-08-01",
                "abstract": "Microservice diagnosis for production engineering.",
            },
        )
        candidate_id = db.insert_scout_candidate(
            self.connection,
            scout_run_id=scout_run_id,
            paper_id=paper_id,
            retrieval_order=1,
            is_new=is_new,
            excluded=excluded,
            exclusion_reason=exclusion_reason,
            source_query="microservice diagnosis",
        )
        db.complete_scout_run(self.connection, scout_run_id)
        return scout_run_id, paper_id, candidate_id


if __name__ == "__main__":
    unittest.main()

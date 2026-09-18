from __future__ import annotations

import json
import io
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from datetime import date, datetime
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
from paper_agents.scout_guidance import ScoutGuidance, build_scout_guidance, topics_with_guidance
from paper_agents.reviewer_agent import card_from_recommendation
from paper_agents.reviewer_agent import download_pdf_for_recommendation
from paper_agents.reviewer_agent import recommended_papers_missing_triage
from paper_agents.reviewer_agent import ReviewerAgent
from paper_agents.reviewer_agent import ReviewerConfig
from paper_agents.topic_inventory import OPENALEX_ROTATING_TOPICS, scout_topic_inventory
from paper_agents.topic_agent import (
    TopicProposal,
    apply_topic_proposal,
    suggest_topic_proposal,
    topic_proposal_to_json,
    validate_topic_proposal,
)
from paper_agents.topics import (
    DuplicateTopicError,
    TopicEntry,
    create_topic_from_fast_path,
    load_topic_config,
    save_topic_config,
    select_topics_for_source,
    topic_inventory_from_config,
)
from paper_agents.scout_agent import ScoutAgent, ScoutConfig
from paper_agents.pipeline import run_daily_pipeline
from paper_agents import web


class FakeSource:
    name = "fake"

    def __init__(self, candidates):
        self.candidates = candidates

    def fetch(self, topics, max_results, freshness_months):
        return self.candidates[:max_results]


class RecordingSource(FakeSource):
    def __init__(self, candidates):
        super().__init__(candidates)
        self.max_results_calls: list[int] = []
        self.topics_calls: list[list[str]] = []

    def fetch(self, topics, max_results, freshness_months):
        self.max_results_calls.append(max_results)
        self.topics_calls.append(list(topics))
        return super().fetch(topics, max_results, freshness_months)


class DegradedArxivSource(RecordingSource):
    name = "arxiv"

    def __init__(self, candidates):
        super().__init__(candidates)
        self.last_diagnostics = {}

    def fetch(self, topics, max_results, freshness_months):
        result = super().fetch(topics, max_results, freshness_months)
        self.last_diagnostics = {
            "coverage_mode": "single_result_406_fallback",
            "coverage_reduced": True,
        }
        return result


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

    def test_topic_config_load_save_round_trip(self):
        config_path = Path(self.tmp.name) / "topics.yaml"
        topics = [
            TopicEntry(
                id="datalake-operations",
                label="Datalake operations",
                query="datalake operations reliability observability production engineering",
                sources=["arxiv", "semantic_scholar", "openalex"],
                cadence="daily",
                priority="high",
                enabled=True,
            )
        ]

        save_topic_config(topics, config_path)
        loaded = load_topic_config(config_path)

        self.assertEqual(loaded[0].id, "datalake-operations")
        self.assertEqual(loaded[0].query, "datalake operations reliability observability production engineering")
        self.assertEqual(loaded[0].sources, ["arxiv", "semantic_scholar", "openalex"])
        self.assertEqual(loaded[0].priority, "high")

    def test_fast_path_topic_creation_defaults(self):
        topic = create_topic_from_fast_path("datalake operations")

        self.assertEqual(topic.label, "Datalake operations")
        self.assertEqual(topic.query, "datalake operations reliability observability production engineering")
        self.assertEqual(topic.sources, ["arxiv", "semantic_scholar", "openalex"])
        self.assertEqual(topic.cadence, "daily")
        self.assertEqual(topic.priority, "normal")
        self.assertTrue(topic.enabled)

    def test_source_topic_selection_rotates_enabled_config_topics(self):
        config_path = Path(self.tmp.name) / "topics.yaml"
        save_topic_config(
            [
                TopicEntry("a-topic", "A topic", "query a", ["openalex"], "weekly", "normal", True),
                TopicEntry("b-topic", "B topic", "query b", ["openalex"], "weekly", "normal", True),
                TopicEntry("disabled-topic", "Disabled", "query disabled", ["openalex"], "weekly", "high", False),
                TopicEntry("manual-topic", "Manual", "query manual", ["openalex"], "manual", "high", True),
            ],
            config_path,
        )

        selected = select_topics_for_source(
            "openalex",
            today=date(2026, 1, 2),
            cadences=("daily", "weekly"),
            path=config_path,
        )

        self.assertEqual(selected, ["query a"])

    def test_source_topic_selection_uses_distinct_am_pm_two_topic_batches(self):
        config_path = Path(self.tmp.name) / "topics.yaml"
        save_topic_config(
            [
                TopicEntry(f"topic-{index}", f"Topic {index}", f"query {index}", ["openalex"], "daily", "normal", True)
                for index in range(4)
            ],
            config_path,
        )

        am_topics = select_topics_for_source(
            "openalex", today=date(2026, 1, 2), path=config_path, count=2, slot=0,
        )
        pm_topics = select_topics_for_source(
            "openalex", today=date(2026, 1, 2), path=config_path, count=2, slot=1,
        )

        self.assertEqual(len(am_topics), 2)
        self.assertEqual(len(pm_topics), 2)
        self.assertFalse(set(am_topics) & set(pm_topics))
        self.assertEqual(set(am_topics) | set(pm_topics), {"query 0", "query 1", "query 2", "query 3"})

    def test_source_topic_selection_does_not_repeat_scarce_am_topics_in_pm(self):
        config_path = Path(self.tmp.name) / "topics.yaml"
        for pool_size in range(1, 6):
            with self.subTest(pool_size=pool_size):
                save_topic_config(
                    [
                        TopicEntry(f"topic-{index}", f"Topic {index}", f"query {index}", ["openalex"], "daily", "normal", True)
                        for index in range(pool_size)
                    ],
                    config_path,
                )
                am_topics = select_topics_for_source(
                    "openalex", today=date(2026, 1, 2), path=config_path, count=2, slot=0,
                )
                pm_topics = select_topics_for_source(
                    "openalex", today=date(2026, 1, 2), path=config_path, count=2, slot=1,
                )

                self.assertEqual(len(am_topics), min(2, pool_size))
                self.assertEqual(len(pm_topics), min(2, max(0, pool_size - 2)))
                self.assertFalse(set(am_topics) & set(pm_topics))

    def test_source_topic_selection_returns_empty_for_intentionally_ineligible_pool(self):
        config_path = Path(self.tmp.name) / "topics.yaml"
        save_topic_config(
            [TopicEntry("disabled", "Disabled", "disabled query", ["arxiv"], "daily", "normal", False)],
            config_path,
        )

        self.assertEqual(select_topics_for_source("arxiv", path=config_path, count=2), [])

    def test_source_topic_selection_bounds_missing_config_fallback_to_batch_size(self):
        config_path = Path(self.tmp.name) / "missing.yaml"

        selected = select_topics_for_source(
            "arxiv", path=config_path, fallback_topics=["one", "two", "three"], count=2, slot=0,
        )

        self.assertEqual(len(selected), 2)

    def test_missing_config_inventory_matches_execution_selection_for_every_source_and_slot(self):
        config_path = Path(self.tmp.name) / "missing.yaml"
        for now, slot in ((datetime(2026, 1, 2, 3, 0), 0), (datetime(2026, 1, 2, 10, 0), 1)):
            with self.subTest(now=now, slot=slot):
                inventory = topic_inventory_from_config(config_path, now=now)
                for source in ("arxiv", "openalex", "semantic_scholar"):
                    item = next(row for row in inventory if row["source"] == source)
                    self.assertEqual(item["next_slot"], slot)
                    self.assertEqual(
                        item["active_topics"],
                        select_topics_for_source(
                            source,
                            today=now.date(),
                            cadences=("daily", "weekly") if source == "openalex" else ("daily",),
                            path=config_path,
                            count=2,
                            slot=slot,
                        ),
                    )

    def test_topic_inventory_uses_the_actual_next_source_run_slot(self):
        config_path = Path(self.tmp.name) / "topics.yaml"
        save_topic_config(
            [
                TopicEntry(f"topic-{index}", f"Topic {index}", f"query {index}", ["openalex"], "daily", "normal", True)
                for index in range(4)
            ],
            config_path,
        )

        inventory = topic_inventory_from_config(config_path, now=datetime(2026, 1, 2, 10, 0))
        openalex = next(item for item in inventory if item["source"] == "openalex")

        self.assertEqual(openalex["next_slot"], 1)
        self.assertEqual(
            openalex["active_topics"],
            select_topics_for_source("openalex", today=date(2026, 1, 2), path=config_path, count=2, slot=1),
        )

    def test_pipeline_skips_intentionally_empty_topic_selection_without_source_call(self):
        with patch("paper_agents.pipeline.create_scout_source") as create_source:
            result = run_daily_pipeline(topics=[], db_path=Path(self.tmp.name) / "pipeline.db")

        self.assertEqual(result["scout_results"], [])
        self.assertEqual(result["skipped_reason"], "no_eligible_configured_topics")
        create_source.assert_not_called()

    def test_daily_scout_empty_topics_does_not_construct_a_default_source(self):
        with patch("paper_agents.scout.ArxivSource") as arxiv_source:
            result = run_daily_scout(topics=[])

        self.assertEqual(result["skipped_reason"], "no_eligible_configured_topics")
        arxiv_source.assert_not_called()

    def test_scout_daily_cli_empty_selection_does_not_apply_guidance_or_construct_source(self):
        with (
            patch("paper_agents.cli.select_topics_for_source", return_value=[]),
            patch("paper_agents.cli.load_scout_guidance", return_value=ScoutGuidance(boost_terms=["incident response"])) as guidance,
            patch("paper_agents.cli.create_scout_source") as create_source,
            patch("paper_agents.cli.run_daily_scout") as run_scout,
            patch("paper_agents.cli.print_section") as print_section,
            patch("sys.argv", ["paper_agents.cli", "scout-daily", "--db", str(self.db_path)]),
        ):
            cli.main()

        guidance.assert_not_called()
        create_source.assert_not_called()
        run_scout.assert_not_called()
        self.assertEqual(print_section.call_args.args[1]["skipped_reason"], "no_eligible_configured_topics")

    def test_scout_daily_cli_topic_override_still_wins(self):
        captured = {}

        def fake_run_daily_scout(**kwargs):
            captured["topics"] = kwargs["topics"]
            return {"source": kwargs["source"].name, "candidates": []}

        with patch("paper_agents.cli.run_daily_scout", fake_run_daily_scout):
            with patch("paper_agents.cli.print_section"):
                with patch(
                    "sys.argv",
                    [
                        "paper_agents.cli",
                        "scout-daily",
                        "--topic",
                        "manual override",
                        "--fetch",
                        "1",
                        "--no-download",
                        "--db",
                        str(self.db_path),
                    ],
                ):
                    cli.main()

        self.assertEqual(captured["topics"], ["manual override"])

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

    def test_semantic_scholar_preserves_source_pdf_url_even_when_doi_like(self):
        candidate = semantic_scholar_paper_to_candidate(
            {
                "paperId": "abc123",
                "title": "LLM Incident Response",
                "abstract": "Root cause analysis for cloud operations.",
                "year": 2026,
                "url": "https://www.semanticscholar.org/paper/abc123",
                "openAccessPdf": {"url": "https://doi.org/10.1145/3706598.3713581"},
                "externalIds": {"DOI": "10.1145/3706598.3713581"},
            }
        )

        self.assertEqual(candidate.pdf_url, "https://doi.org/10.1145/3706598.3713581")

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

    def test_curator_keyword_repetition_does_not_saturate_score(self):
        profile = {
            "interests": ["aiops", "root cause analysis", "anomaly detection"],
            "positive_signals": ["observability", "llm", "failure diagnosis", "cloud operations"],
            "negative_signals": [],
        }
        paper = evaluate_candidate(
            {
                "paper_id": 1,
                "title": (
                    "AIOps AIOps Root Cause Analysis Root Cause Analysis "
                    "Anomaly Detection Observability LLM Failure Diagnosis"
                ),
                "abstract": (
                    "AIOps root cause analysis anomaly detection observability llm "
                    "failure diagnosis cloud operations. " * 8
                ),
            },
            profile,
        )

        self.assertGreater(paper["score"], 0.0)
        self.assertLess(paper["score"], 95.0)

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

    def test_scout_refills_past_known_candidates_before_curation(self):
        source = RecordingSource([candidate(f"2601.refill{i}v1", f"Incident RCA {i}") for i in range(8)])
        for item in source.candidates[:5]:
            db.upsert_paper(self.connection, item.as_dict())
        self.connection.commit()

        result = ScoutAgent(source=source).run(
            self.connection,
            workflow_cycle_id=self.cycle_id,
            attempt_number=1,
            config=ScoutConfig(
                topics=["AIOps"],
                max_candidates=5,
                min_eligible_candidates=3,
                max_refill_fetch_rounds=3,
            ),
        )

        self.assertEqual(source.max_results_calls, [5, 10])
        self.assertEqual(result["eligible_count"], 3)
        self.assertEqual(result["stored_count"], 8)
        refill = json.loads(self.connection.execute(
            "SELECT diagnostics_json FROM scout_runs WHERE id = ?", (result["scout_run_id"],)
        ).fetchone()[0])
        self.assertEqual(refill["refill"]["stop_reason"], "minimum_eligible_reached")

    def test_degraded_arxiv_stops_internal_refill_after_one_pass(self):
        source = DegradedArxivSource(
            [candidate(f"2601.degraded{i}v1", f"Incident RCA {i}") for i in range(2)]
        )
        result = ScoutAgent(source=source).run(
            self.connection,
            workflow_cycle_id=self.cycle_id,
            attempt_number=1,
            config=ScoutConfig(
                topics=["AIOps"], max_candidates=5,
                min_eligible_candidates=3, max_refill_fetch_rounds=3,
            ),
        )

        self.assertEqual(source.max_results_calls, [5])
        self.assertTrue(result["source_degraded"])
        diagnostics = json.loads(self.connection.execute(
            "SELECT diagnostics_json FROM scout_runs WHERE id = ?", (result["scout_run_id"],)
        ).fetchone()[0])
        self.assertEqual(diagnostics["refill"]["stop_reason"], "source_degraded")

    def test_semantic_scholar_uses_one_conservative_fetch_round(self):
        source = RecordingSource([candidate(f"semantic-{index}", f"Semantic candidate {index}", source="semantic_scholar") for index in range(30)])
        source.name = "semantic_scholar"
        result = ScoutAgent(source=source).run(
            self.connection,
            workflow_cycle_id=self.cycle_id,
            attempt_number=1,
            config=ScoutConfig(
                topics=["AIOps"],
                max_candidates=10,
                min_eligible_candidates=10,
                max_refill_fetch_rounds=3,
            ),
        )

        self.assertEqual(source.max_results_calls, [10])
        self.assertEqual(source.topics_calls, [["AIOps"]])
        diagnostics = json.loads(self.connection.execute(
            "SELECT diagnostics_json FROM scout_runs WHERE id = ?", (result["scout_run_id"],)
        ).fetchone()[0])
        self.assertEqual(len(diagnostics["refill"]["rounds"]), 1)

    def test_prior_recommendation_is_excluded_on_later_scout_run(self):
        old_source = FakeSource([candidate("2601.openv1", "Incident RCA")])
        config = ScoutConfig(topics=["AIOps"], max_candidates=5)
        ScoutAgent(source=old_source).run(self.connection, workflow_cycle_id=self.cycle_id, attempt_number=1, config=config)
        old_candidates = db.eligible_candidates_for_cycle(self.connection, self.cycle_id)
        CuratorAgent().run(
            self.connection,
            workflow_cycle_id=self.cycle_id,
            candidates=old_candidates,
            profile_version=db.current_profile_version(self.connection),
            scout_attempt_count=1,
            config=CuratorConfig(max_recommendations=1, min_quality_score=1, max_scout_attempts=1),
        )
        next_cycle_id = db.create_workflow_cycle(self.connection, mode="test", max_scout_attempts=1)

        result = ScoutAgent(source=old_source).run(
            self.connection,
            workflow_cycle_id=next_cycle_id,
            attempt_number=1,
            config=config,
        )

        self.assertEqual(result["eligible_count"], 0)
        row = self.connection.execute(
            """
            SELECT excluded, exclusion_reason
            FROM scout_candidates
            WHERE scout_run_id = ?
            """,
            (result["scout_run_id"],),
        ).fetchone()
        self.assertEqual(row, (1, "already_recommended"))

    def test_same_cycle_recommendation_is_excluded_on_rescout(self):
        source = FakeSource([candidate("2601.samecyclev1", "Incident RCA")])
        config = ScoutConfig(topics=["AIOps"], max_candidates=5)
        ScoutAgent(source=source).run(self.connection, workflow_cycle_id=self.cycle_id, attempt_number=1, config=config)
        candidates = db.eligible_candidates_for_cycle(self.connection, self.cycle_id)
        CuratorAgent().run(
            self.connection,
            workflow_cycle_id=self.cycle_id,
            candidates=candidates,
            profile_version=db.current_profile_version(self.connection),
            scout_attempt_count=1,
            config=CuratorConfig(max_recommendations=1, min_quality_score=1, max_scout_attempts=3),
        )

        result = ScoutAgent(source=source).run(
            self.connection,
            workflow_cycle_id=self.cycle_id,
            attempt_number=2,
            config=config,
        )

        self.assertEqual(result["eligible_count"], 0)
        row = self.connection.execute(
            """
            SELECT excluded, exclusion_reason
            FROM scout_candidates
            WHERE scout_run_id = ?
            """,
            (result["scout_run_id"],),
        ).fetchone()
        self.assertEqual(row, (1, "already_recommended"))

    def test_non_arxiv_candidate_without_pdf_url_remains_curator_eligible(self):
        source = FakeSource(
            [
                ScoutCandidate(
                    source="semantic_scholar",
                    source_id="no-pdf-semantic",
                    title="Interesting paper without a PDF",
                    abstract="Interactive debugging for AI agents.",
                    authors=[],
                    published="2026-01-01",
                    updated=None,
                    url="https://www.semanticscholar.org/paper/no-pdf-semantic",
                    pdf_url=None,
                    categories=["Computer Science"],
                )
            ]
        )

        result = ScoutAgent(source=source).run(
            self.connection,
            workflow_cycle_id=self.cycle_id,
            attempt_number=1,
            config=ScoutConfig(topics=["debugging agents"], max_candidates=5),
        )

        self.assertEqual(result["stored_count"], 1)
        self.assertEqual(result["eligible_count"], 1)
        row = self.connection.execute(
            """
            SELECT excluded, exclusion_reason
            FROM scout_candidates
            WHERE scout_run_id = ?
            """,
            (result["scout_run_id"],),
        ).fetchone()
        self.assertEqual(row, (0, None))
        self.assertEqual(len(db.eligible_candidates_for_cycle(self.connection, self.cycle_id)), 1)

    def test_non_arxiv_candidate_with_doi_pdf_url_remains_curator_eligible(self):
        source = FakeSource(
            [
                ScoutCandidate(
                    source="semantic_scholar",
                    source_id="doi-as-pdf-semantic",
                    title="Interesting paper with DOI instead of PDF",
                    abstract="Interactive debugging for AI agents.",
                    authors=[],
                    published="2026-01-01",
                    updated=None,
                    url="https://www.semanticscholar.org/paper/doi-as-pdf-semantic",
                    pdf_url="https://doi.org/10.1145/3706598.3713581",
                    categories=["Computer Science"],
                )
            ]
        )

        result = ScoutAgent(source=source).run(
            self.connection,
            workflow_cycle_id=self.cycle_id,
            attempt_number=1,
            config=ScoutConfig(topics=["debugging agents"], max_candidates=5),
        )

        self.assertEqual(result["stored_count"], 1)
        self.assertEqual(result["eligible_count"], 1)
        row = self.connection.execute(
            """
            SELECT excluded, exclusion_reason
            FROM scout_candidates
            WHERE scout_run_id = ?
            """,
            (result["scout_run_id"],),
        ).fetchone()
        self.assertEqual(row, (0, None))
        self.assertEqual(len(db.eligible_candidates_for_cycle(self.connection, self.cycle_id)), 1)

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

    def test_pipeline_deepens_fetch_limit_on_rescout(self):
        db_path = Path(self.tmp.name) / "pipeline_deepens.db"
        db.init_db(db_path)
        source = RecordingSource([candidate(f"2601.deep{i}v1", f"Unrelated math {i}") for i in range(6)])

        with (
            patch("paper_agents.pipeline.create_scout_source", return_value=source),
            patch("paper_agents.pipeline.load_profile", return_value={"interests": ["AIOps"], "positive_signals": [], "negative_signals": []}),
        ):
            result = run_daily_pipeline(
                topics=["AIOps"],
                fetch_limit=2,
                keep_limit=3,
                max_scout_attempts=3,
                min_quality_score=100,
                db_path=db_path,
                mode="test",
                source_name="arxiv",
            )

        # Refill fetches deeper within each Scout attempt when earlier results
        # are already known, then pipeline-level rescout still deepens its base.
        self.assertEqual(source.max_results_calls, [2, 4, 8, 6, 12])
        self.assertEqual(len(result["scout_results"]), 3)
        self.assertTrue(all(scout_result["eligible_count"] > 0 for scout_result in result["scout_results"]))
        self.assertEqual(result["curator"]["recommendations"], [])

    def test_pipeline_suppresses_outer_rescout_for_degraded_arxiv(self):
        db_path = Path(self.tmp.name) / "pipeline_degraded.db"
        db.init_db(db_path)
        source = DegradedArxivSource([candidate("2601.degradedv1", "Unrelated math")])

        with (
            patch("paper_agents.pipeline.create_scout_source", return_value=source),
            patch("paper_agents.pipeline.load_profile", return_value={
                "interests": ["AIOps"], "positive_signals": [], "negative_signals": []
            }),
        ):
            result = run_daily_pipeline(
                topics=["AIOps"], fetch_limit=2, keep_limit=3,
                max_scout_attempts=3, min_quality_score=100,
                db_path=db_path, mode="test", source_name="arxiv",
            )

        self.assertEqual(source.max_results_calls, [2])
        self.assertEqual(len(result["scout_results"]), 1)
        self.assertFalse(result["curator"]["requested_rescout"])
        self.assertIn("reduced-coverage", result["curator"]["rescout_reason"])

    def test_pipeline_does_not_hold_write_lock_during_source_fetch(self):
        db_path = Path(self.tmp.name) / "pipeline_concurrency.db"
        db.init_db(db_path)

        class ConcurrentWriterSource(FakeSource):
            def fetch(self, topics, *, max_results, freshness_months):
                with db.connect_db(db_path) as concurrent_connection:
                    db.create_workflow_cycle(
                        concurrent_connection,
                        mode="concurrent-test",
                        max_scout_attempts=1,
                    )
                return super().fetch(
                    topics,
                    max_results=max_results,
                    freshness_months=freshness_months,
                )

        source = ConcurrentWriterSource([candidate("2601.concurrentv1", "AIOps observability")])
        with (
            patch("paper_agents.pipeline.create_scout_source", return_value=source),
            patch(
                "paper_agents.pipeline.load_profile",
                return_value={"interests": ["AIOps"], "positive_signals": [], "negative_signals": []},
            ),
        ):
            result = run_daily_pipeline(
                topics=["AIOps"],
                fetch_limit=1,
                keep_limit=1,
                max_scout_attempts=1,
                min_quality_score=100,
                db_path=db_path,
                mode="test",
                source_name="openalex",
            )

        self.assertEqual(result["scout_results"][0]["eligible_count"], 1)
        with db.connect_db(db_path) as connection:
            modes = {row[0] for row in connection.execute("SELECT mode FROM workflow_cycles")}
        self.assertIn("concurrent-test", modes)

    def test_connect_db_waits_for_short_lived_write_locks(self):
        with db.connect_db(self.db_path) as connection:
            busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]

        self.assertEqual(busy_timeout, db.DEFAULT_BUSY_TIMEOUT_MS)

    def test_scouting_guidance_is_versioned_and_only_latest_active_loads(self):
        first = db.create_scouting_guidance(self.connection, curator_run_id=None, guidance_text="old")
        second = db.create_scouting_guidance(self.connection, curator_run_id=None, guidance_text="new")
        active = db.active_scouting_guidance(self.connection)
        self.assertNotEqual(first, second)
        self.assertEqual(active["id"], second)
        inactive_count = self.connection.execute("SELECT COUNT(*) FROM scouting_guidance WHERE active = 0").fetchone()[0]
        self.assertEqual(inactive_count, 1)

    def test_scout_guidance_uses_profile_and_recent_structured_feedback(self):
        keep_paper_id, _ = db.upsert_paper(
            self.connection,
            {
                "source": "semantic_scholar",
                "source_id": "keep-guidance",
                "title": "RAG observability for production incident response",
                "abstract": "Root cause analysis with telemetry and context grounding.",
                "categories": ["Software engineering"],
            },
        )
        reject_paper_id, _ = db.upsert_paper(
            self.connection,
            {
                "source": "openalex",
                "source_id": "reject-guidance",
                "title": "Weak illustrative monitoring benchmark",
                "abstract": "No empirical production evidence.",
                "categories": ["Software engineering"],
            },
        )
        ingest_feedback_blob(
            self.connection,
            paper_id=keep_paper_id,
            recommendation_id=None,
            content="Decision: keep\nScore: 4.5\nRAG and context grounding are useful for incident response.",
            source="test",
        )
        ingest_feedback_blob(
            self.connection,
            paper_id=reject_paper_id,
            recommendation_id=None,
            content="Decision: reject\nScore: 1\nToo illustrative and weak evidence.",
            source="test",
        )

        rows = db.recent_structured_feedback_with_papers(self.connection, limit=10)
        guidance = build_scout_guidance(db.current_profile_version(self.connection), rows)

        self.assertIn("incident management", guidance.include_terms)
        self.assertIn("rag", guidance.boost_terms)
        self.assertIn("context grounding", guidance.boost_terms)
        self.assertIn("weak evidence", guidance.avoid_terms)
        self.assertIn("illustrative", guidance.avoid_terms)
        self.assertEqual(guidance.feedback_count, 2)
        self.assertEqual(guidance.profile_version_id, self.profile_id)

    def test_scout_agent_records_feedback_guidance_and_guided_topics(self):
        feedback_paper_id, _ = db.upsert_paper(
            self.connection,
            {
                "source": "semantic_scholar",
                "source_id": "guidance-feedback",
                "title": "RAG observability for production incidents",
                "abstract": "Context grounding for incident response.",
            },
        )
        ingest_feedback_blob(
            self.connection,
            paper_id=feedback_paper_id,
            recommendation_id=None,
            content="Decision: keep\nScore: 5\nMore RAG context grounding for incident response.",
            source="test",
        )
        source = FakeSource([candidate("2601.guidedv1", "Guided candidate")])

        result = ScoutAgent(source=source).run(
            self.connection,
            workflow_cycle_id=self.cycle_id,
            attempt_number=1,
            config=ScoutConfig(topics=["microservice diagnosis"], max_candidates=5),
        )

        row = self.connection.execute(
            "SELECT topics_json, guidance_id, diagnostics_json FROM scout_runs WHERE id = ?",
            (result["scout_run_id"],),
        ).fetchone()
        topics = json.loads(row[0])
        diagnostics = json.loads(row[2])
        guidance_row = self.connection.execute(
            "SELECT active, metadata_json FROM scouting_guidance WHERE id = ?",
            (row[1],),
        ).fetchone()
        self.assertIn("rag", topics)
        self.assertEqual(guidance_row[0], 0)
        self.assertEqual(json.loads(guidance_row[1])["source"], "feedback_profile")
        self.assertIn("scout_guidance", diagnostics)
        self.assertIn("rag", result["guidance"]["boost_terms"])

    def test_scout_agent_excludes_clear_feedback_avoid_matches(self):
        profile_version = db.current_profile_version(self.connection)
        db.create_profile_version(
            self.connection,
            profile=profile_version["profile"] | {"negative_signals": ["toy benchmark", "weak evidence"]},
            source_structured_feedback_id=None,
            change_summary="test negative signals",
        )
        source = FakeSource(
            [
                ScoutCandidate(
                    source="arxiv",
                    source_id="2601.badguidancev1",
                    title="Toy benchmark with weak evidence",
                    abstract="An illustrative paper with no production signal.",
                    authors=[],
                    published="2026-01-01",
                    updated=None,
                    url="https://example.test/bad",
                    pdf_url=None,
                    categories=["cs.SE"],
                ),
                candidate("2601.goodguidancev1", "Incident response observability"),
            ]
        )

        result = ScoutAgent(source=source).run(
            self.connection,
            workflow_cycle_id=self.cycle_id,
            attempt_number=1,
            config=ScoutConfig(topics=["AIOps"], max_candidates=5),
        )

        self.assertEqual(result["eligible_count"], 1)
        rows = self.connection.execute(
            """
            SELECT papers.title, scout_candidates.excluded, scout_candidates.exclusion_reason,
                   scout_candidates.source_diagnostics_json
            FROM scout_candidates
            JOIN papers ON papers.id = scout_candidates.paper_id
            ORDER BY scout_candidates.retrieval_order
            """
        ).fetchall()
        bad = next(row for row in rows if row[0] == "Toy benchmark with weak evidence")
        self.assertEqual(bad[1], 1)
        self.assertEqual(bad[2], "feedback_avoid_terms")
        diagnostics = json.loads(bad[3])
        self.assertEqual(diagnostics["feedback_guidance"]["feedback_avoid_hits"], ["toy benchmark", "weak evidence"])

    def test_topics_with_guidance_preserves_explicit_topic_and_bounds_expansion(self):
        guidance = build_scout_guidance(
            {"id": 9, "profile": {"interests": ["observability"], "positive_signals": [], "negative_signals": []}},
            [
                {
                    "decision": "keep",
                    "score": 5,
                    "title": "RAG context grounding for production incidents",
                    "abstract": "RAG and telemetry for incident response.",
                    "categories": [],
                    "observations": [],
                    "preference_signals": [],
                }
            ],
        )

        topics = topics_with_guidance(["microservice diagnosis"], guidance, max_topics=5)

        self.assertEqual(topics[0], "microservice diagnosis")
        self.assertLessEqual(len(topics), 5)
        self.assertIn("rag", topics)


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

    def test_reviewer_download_uses_semantic_reader_download_link_fallback(self):
        class FakeResponse:
            def __init__(self, url: str, data: bytes, content_type: str):
                self._url = url
                self._data = data
                self.headers = {"content-type": content_type}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return self._data

            def geturl(self):
                return self._url

        calls: list[str] = []

        def fake_urlopen(request, timeout):
            url = request.full_url
            calls.append(url)
            if url == "https://www.semanticscholar.org/reader/cddbfa8cb765894db98925730db8a9c22f4ec633":
                return FakeResponse(
                    url,
                    b'<a href="https://export.arxiv.org/pdf/2507.12472v1.pdf" download="">Download PDF</a>',
                    "text/html; charset=utf-8",
                )
            if url == "https://export.arxiv.org/pdf/2507.12472v1.pdf":
                return FakeResponse(url, b"%PDF-1.7\nexample", "application/pdf")
            raise AssertionError(f"unexpected URL: {url}")

        with patch("paper_agents.reviewer_agent.urllib.request.urlopen", fake_urlopen):
            path = download_pdf_for_recommendation(
                {
                    "paper_id": 240,
                    "source": "semantic_scholar",
                    "source_id": "cddbfa8cb765894db98925730db8a9c22f4ec633",
                    "title": "A Survey of AIOps in the Era of Large Language Models",
                    "pdf_url": "https://doi.org/10.1145/3746635",
                },
                Path(self.tmp.name) / "papers",
            )

        self.assertIsNotNone(path)
        self.assertEqual(path.read_bytes(), b"%PDF-1.7\nexample")
        self.assertEqual(
            calls,
            [
                "https://www.semanticscholar.org/reader/cddbfa8cb765894db98925730db8a9c22f4ec633",
                "https://export.arxiv.org/pdf/2507.12472v1.pdf",
            ],
        )

    def test_reviewer_backfill_uses_abstract_only_triage_when_pdf_is_unavailable(self):
        paper_id, _ = db.upsert_paper(
            self.connection,
            {
                "source": "semantic_scholar",
                "source_id": "abstract-only-paper",
                "title": "Abstract Only Paper",
                "url": "https://www.semanticscholar.org/paper/abstract-only-paper",
                "pdf_url": None,
                "published": "2026-09-04",
                "abstract": "This paper studies incident response automation for production operations.",
            },
        )
        recommendation = {
            "paper_id": paper_id,
            "recommendation_order": 1,
            "title": "Abstract Only Paper",
            "published": "2026-09-04",
            "source": "semantic_scholar",
            "source_id": "abstract-only-paper",
            "url": "https://www.semanticscholar.org/paper/abstract-only-paper",
            "pdf_url": None,
            "abstract": "This paper studies incident response automation for production operations.",
            "score": 47.0,
            "rationale": "Matched incident response.",
            "matched_signals": ["incident response", "production operations"],
        }
        fake_extraction = {
            "merged": {
                "paper_date": "2026-09-04",
                "research_problem": "Incident response automation",
                "why_it_matters": "It can reduce operational toil.",
                "approach": "Uses source abstract evidence.",
            },
            "merge_strategy": "synthesis",
            "chunk_count": 1,
        }

        with (
            patch("paper_agents.reviewer_agent.download_pdf_for_recommendation", return_value=None),
            patch("paper_agents.reviewer_agent.extract_paper", return_value=fake_extraction) as extract_mock,
            patch("paper_agents.reviewer_agent.DEFAULT_EXTRACTION_DIR", Path(self.tmp.name) / "extractions"),
        ):
            card = ReviewerAgent().review_recommendation(
                self.connection,
                recommendation,
                index=1,
                config=ReviewerConfig(pdf_dir=Path(self.tmp.name) / "papers", model="test-model"),
            )

        self.assertEqual(card["research_problem"], "Incident response automation")
        self.assertIsNone(card["error"])
        extract_mock.assert_called_once()
        artifact = self.connection.execute(
            "SELECT path, metadata_json FROM artifacts WHERE paper_id = ? AND artifact_type = 'triage_summary'",
            (paper_id,),
        ).fetchone()
        self.assertIsNotNone(artifact)
        self.assertTrue(Path(artifact[0]).name.endswith(".abstract.summary.json"))
        metadata = json.loads(artifact[1])
        self.assertTrue(metadata["abstract_only"])
        saved = json.loads(Path(artifact[0]).read_text(encoding="utf-8"))
        self.assertTrue(saved["abstract_only"])
        self.assertEqual(saved["merged"]["source_abstract"], recommendation["abstract"])

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

    def test_init_db_migrates_structured_feedback_score_to_real(self):
        legacy_db = Path(self.tmp.name) / "legacy-score.db"
        connection = sqlite3.connect(legacy_db)
        try:
            connection.executescript(
                """
                CREATE TABLE raw_feedback (
                    id INTEGER PRIMARY KEY,
                    paper_id INTEGER,
                    recommendation_id INTEGER,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    received_at TEXT NOT NULL DEFAULT (datetime('now')),
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE feedback_parse_attempts (
                    id INTEGER PRIMARY KEY,
                    raw_feedback_id INTEGER NOT NULL REFERENCES raw_feedback(id) ON DELETE CASCADE,
                    attempted_at TEXT NOT NULL DEFAULT (datetime('now')),
                    parser_name TEXT NOT NULL,
                    parser_version TEXT NOT NULL,
                    model TEXT,
                    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
                    error TEXT,
                    output_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE structured_feedback (
                    id INTEGER PRIMARY KEY,
                    parse_attempt_id INTEGER NOT NULL UNIQUE REFERENCES feedback_parse_attempts(id) ON DELETE CASCADE,
                    paper_id INTEGER,
                    decision TEXT,
                    score INTEGER,
                    observations_json TEXT NOT NULL DEFAULT '[]',
                    preference_signals_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    CHECK (score IS NULL OR (score >= 1 AND score <= 5))
                );
                INSERT INTO raw_feedback (id, content, content_hash) VALUES (1, 'Score: 4', 'hash');
                INSERT INTO feedback_parse_attempts (id, raw_feedback_id, parser_name, parser_version, status)
                VALUES (1, 1, 'feedback-agent', 'deterministic-v1', 'succeeded');
                INSERT INTO structured_feedback (id, parse_attempt_id, decision, score, observations_json, preference_signals_json)
                VALUES (1, 1, 'keep', 4, '[]', '[]');
                """
            )
            connection.commit()
        finally:
            connection.close()

        db.init_db(legacy_db)

        migrated = sqlite3.connect(legacy_db)
        try:
            score_column = next(column for column in migrated.execute("PRAGMA table_info(structured_feedback)") if column[1] == "score")
            score = migrated.execute("SELECT score FROM structured_feedback WHERE id = 1").fetchone()[0]
        finally:
            migrated.close()
        self.assertEqual(score_column[2].upper(), "REAL")
        self.assertEqual(score, 4.0)

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

    def test_feedback_blob_ingestion_accepts_decimal_score(self):
        paper_id, recommendation_id = self._seed_review_recommendation()
        content = "Decision: keep\nScore: 4.5\nUseful and practical.\n"

        output = ingest_feedback_blob(
            self.connection,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            content=content,
            source="test",
            status=None,
        )

        structured = self.connection.execute(
            "SELECT decision, score FROM structured_feedback WHERE id = ?",
            (output["structured_feedback_id"],),
        ).fetchone()
        self.assertEqual(structured, ("keep", 4.5))

    def test_feedback_blob_ingestion_ignores_out_of_range_decimal_score(self):
        paper_id, recommendation_id = self._seed_review_recommendation()

        output = ingest_feedback_blob(
            self.connection,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            content="Decision: maybe\nScore: 5.5\nInteresting but too broad.",
            source="test",
            status=None,
        )

        score = self.connection.execute(
            "SELECT score FROM structured_feedback WHERE id = ?",
            (output["structured_feedback_id"],),
        ).fetchone()[0]
        self.assertIsNone(score)

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
            with patch("paper_agents.feedback.run_gemini") as run:
                run.return_value = '{"profile":{"interests":[],"positive_signals":[],"negative_signals":[],"notes":""},"change_summary":"ok"}'
                call_gemini_json(payload)

        self.assertEqual(run.call_args.args[1], 240)
        self.assertTrue(run.call_args.kwargs["stream_json"])
        self.assertEqual(run.call_args.args[0][1:3], ["--output-format", "stream-json"])

    def test_call_gemini_json_timeout_error_includes_timeout(self):
        payload = {"current_profile": {}, "structured_feedback": []}
        with patch.dict("os.environ", {"PAPER_AGENT_GEMINI_TIMEOUT_SECONDS": "240"}):
            with patch("paper_agents.feedback.run_gemini", side_effect=RuntimeError("Gemini CLI timed out after 240 seconds")):
                with self.assertRaisesRegex(RuntimeError, "timed out after 240 seconds"):
                    call_gemini_json(payload)

    def test_feedback_profile_apply_falls_back_to_flash_lite_on_quota_error(self):
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
            calls.append(model)
            if model is None:
                raise RuntimeError("Gemini CLI failed: 429 quota exhausted")
            return {
                "profile": {
                    "interests": ["fallback profile"],
                    "positive_signals": ["quota recovery"],
                    "negative_signals": [],
                    "notes": "Recovered with Flash-Lite.",
                },
                "change_summary": "Recovered with fallback model.",
            }

        with patch("paper_agents.feedback.call_gemini_json", side_effect=provider):
            output = apply_feedback_to_profile(self.connection, dry_run=True)

        self.assertEqual(calls, [None, "gemini-3.1-flash-lite"])
        self.assertEqual(output["status"], "dry_run")
        self.assertEqual(output["model"], "gemini-3.1-flash-lite")
        self.assertEqual(output["structured_feedback_ids"], [feedback["structured_feedback_id"]])
        attempts = self.connection.execute(
            "SELECT model, dry_run, status, error FROM feedback_profile_apply_attempts ORDER BY id"
        ).fetchall()
        self.assertEqual(attempts[0], (None, 1, "failed", "Gemini CLI failed: 429 quota exhausted"))
        self.assertEqual(attempts[1], ("gemini-3.1-flash-lite", 1, "succeeded", None))

    def test_feedback_profile_apply_does_not_fallback_for_non_quota_error(self):
        paper_id, recommendation_id = self._seed_review_recommendation()
        ingest_feedback_blob(
            self.connection,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            content="Decision: keep\nScore: 5\nVery applied.",
            source="test",
        )

        with patch("paper_agents.feedback.call_gemini_json", side_effect=RuntimeError("Provider did not return valid JSON.")) as provider:
            output = apply_feedback_to_profile(self.connection, dry_run=True)

        self.assertEqual(provider.call_count, 1)
        self.assertEqual(output["status"], "failed")
        self.assertIsNone(output["model"])
        self.assertEqual(output["error"], "Provider did not return valid JSON.")

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

    def test_render_pulled_date_marks_naive_database_timestamp_as_utc(self):
        rendered = web.render_pulled_date({"pulled_at": "2026-09-08 01:43:02"})

        self.assertEqual(
            rendered,
            '<time class="pulled-date" data-pulled-at="2026-09-08T01:43:02Z" '
            'datetime="2026-09-08T01:43:02Z">Pulled 2026-09-08</time>',
        )

    def test_review_queue_renders_simplified_review_workflow(self):
        self._seed_review_recommendation()
        self.connection.commit()

        html = web.render_review_queue(self.db_path)

        self.assertIn('<form method="get" action="/" class="queue-controls">', html)
        self.assertIn('<select name="filter"', html)
        self.assertIn('<select name="source"', html)
        self.assertIn('<select name="sort"', html)
        self.assertNotIn('<select name="view"', html)
        self.assertNotIn('class="secondary">Apply</button>', html)
        self.assertIn('<span class="visually-hidden">Queue</span>', html)
        self.assertIn('<span class="visually-hidden">Source</span>', html)
        self.assertIn('<span class="visually-hidden">Sort</span>', html)
        self.assertIn('role="group" aria-label="View density"', html)
        self.assertIn('name="view" value="full" class="view-option current" aria-label="Full view" title="Full view"', html)
        self.assertIn('name="view" value="compact" class="view-option" aria-label="Condensed view" title="Condensed view"', html)
        self.assertNotIn('<span>Full</span>', html)
        self.assertNotIn('<span>Condensed</span>', html)
        self.assertIn("All sources", html)
        self.assertIn("arXiv", html)
        self.assertIn('href="/topics"', html)
        self.assertIn('href="/health"', html)
        self.assertIn('class="source-badge source-badge-arxiv"', html)
        self.assertIn('class="action-rail"', html)
        self.assertIn('<h1 class="brand-title">', html)
        self.assertIn('<a class="brand-home" href="/">', html)
        self.assertIn('<picture class="logo-frame">', html)
        self.assertIn('srcset="/assets/logo_dark.png?v=', html)
        self.assertIn('src="/assets/logo_light.png?v=', html)
        self.assertIn('<span class="brand-name">Project Paper</span>', html)
        self.assertIn('<span class="page-title">Review Queue</span>', html)
        self.assertIn('<div class="header-controls">', html)
        self.assertIn('<nav class="primary-nav" aria-label="Primary">', html)
        self.assertIn('<option value="score">Highest score</option>', html)
        self.assertIn('<option value="latest" selected>Newest</option>', html)
        self.assertIn('<option value="all" selected>All papers</option>', html)
        self.assertIn('<option value="has_feedback">Scored</option>', html)
        self.assertIn('<option value="needs_review">Needs review</option>', html)
        self.assertNotIn('<option value="not_interested"', html)
        self.assertIn('<span>Match Score</span><strong>72.5</strong>', html)
        self.assertIn('<div class="paper-meta">', html)
        self.assertIn("Pulled ", html)
        self.assertRegex(html, r'data-pulled-at="\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"')
        self.assertRegex(html, r'datetime="\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"')
        self.assertIn('pulledAt.getFullYear()', html)
        self.assertIn('pulledAt.getMonth() + 1', html)
        self.assertIn('pulledAt.getDate()', html)
        self.assertLess(html.index('<div class="paper-meta">'), html.index("Pulled "))
        self.assertLess(html.index('aria-label="Copy paper URL">Copy</button>'), html.index("Pulled "))
        self.assertLess(html.index("Pulled "), html.index('<section class="match-rationale">'))
        self.assertIn('data-copy-value="https://example.test/2607.reviewv1"', html)
        self.assertIn('class="title-link"', html)
        self.assertIn('class="metadata-action pdf-action"', html)
        self.assertIn(">Open PDF</a>", html)
        self.assertIn('aria-label="Copy paper URL">Copy</button>', html)
        self.assertLess(html.index(">Open PDF</a>"), html.index('aria-label="Copy paper URL">Copy</button>'))
        self.assertNotIn("Artifacts</summary>", html)
        self.assertNotIn(">Open summary</a>", html)
        self.assertNotIn(">Open paper</a>", html)
        self.assertNotIn(">Not interested</button>", html)
        self.assertIn('<span class="signals-label">Signals</span>', html)
        self.assertIn('aria-label="Copy Paper Discussion prompt">Copy discussion prompt</button>', html)
        self.assertIn("I want to discuss this paper for my Project Paper workflow.", html)
        self.assertIn("Decision: keep|maybe|reject", html)
        self.assertIn("Score: 1-5, where 5 is highest", html)
        self.assertIn("<summary>Add feedback</summary>", html)
        self.assertIn('<textarea name="notes" placeholder="Paste final feedback blob from Paper Discussion here">', html)
        self.assertIn("Why this matches you", html)
        self.assertIn("Strong match.", html)
        self.assertNotIn("No ChatGPT review yet", html)
        self.assertNotIn("Copy prompt/link", html)
        self.assertNotIn(">Read later</button>", html)
        self.assertNotIn(">Interested</button>", html)
        self.assertNotIn(">Reviewed</button>", html)
        self.assertNotIn("<span>score</span>", html)
        self.assertNotIn(">Notes<textarea", html)

    def test_review_queue_collapses_keyword_echo_rationale(self):
        card = {"ranking_reason": "Matched incident, automation.", "matched_keywords": ["incident", "automation"]}

        self.assertEqual(web.display_rationale(card), "Matched your current profile signals.")
        rendered = web.render_match_rationale(card, '<span class="tag">incident</span>')
        self.assertIn('<span class="tag">incident</span>', rendered)
        self.assertNotIn("<p>Matched your current profile signals.</p>", rendered)

    def test_review_queue_preserves_specific_rationale(self):
        card = {"ranking_reason": "Strong match.", "matched_keywords": ["incident", "automation"]}

        self.assertEqual(web.display_rationale(card), "Strong match.")

    def test_review_queue_discussion_prompt_includes_existing_card_context(self):
        card = {
            "title": "Prompt Paper",
            "source_label": "Semantic Scholar",
            "published": "2026-08-29",
            "url": "https://example.test/prompt-paper",
            "pdf_url": "https://example.test/prompt-paper.pdf",
            "score": 87.25,
            "user_score": 4.5,
            "ranking_reason": "Strong practical SRE match.",
            "matched_keywords": ["aiops", "incident response"],
            "artifacts": {},
            "summary": {
                "research_problem": "Reducing incident triage time.",
                "why_it_matters": "Production engineers need faster context.",
                "approach": "Retrieval-grounded root cause analysis.",
            },
        }

        prompt = web.build_discussion_prompt(card)

        self.assertIn("Paper:\nPrompt Paper", prompt)
        self.assertIn("Source: Semantic Scholar", prompt)
        self.assertIn("Date: 2026-08-29", prompt)
        self.assertIn("Link: https://example.test/prompt-paper", prompt)
        self.assertIn("PDF: https://example.test/prompt-paper.pdf", prompt)
        self.assertIn("Match score: 87.2", prompt)
        self.assertIn("User score: 4.5/5", prompt)
        self.assertIn("Why this matches me: Strong practical SRE match.", prompt)
        self.assertIn("Signals: aiops, incident response", prompt)
        self.assertIn("Problem: Reducing incident triage time.", prompt)
        self.assertIn("Decision: keep|maybe|reject", prompt)

    def test_review_queue_renders_profile_apply_queued_banner(self):
        html = web.render_review_queue(self.db_path, saved=True, profile_apply_queued=True)

        self.assertIn("Feedback saved.", html)
        self.assertIn("Profile update queued.", html)

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

        html = web.render_review_queue(self.db_path, filter_value="has_feedback")

        self.assertIn('<div class="user-score"><span>Your score</span><strong>2/5</strong></div>', html)
        self.assertIn("<summary>View/edit feedback</summary>", html)
        self.assertIn('class="match-score score-secondary"', html)
        self.assertIn('<span>Match Score</span><strong>72.5</strong>', html)
        self.assertLess(html.index('<span>Match Score</span><strong>72.5</strong>'), html.index('<div class="user-score"><span>Your score</span><strong>2/5</strong></div>'))

    def test_review_queue_renders_decimal_user_feedback_score(self):
        paper_id, recommendation_id = self._seed_review_recommendation()
        ingest_feedback_blob(
            self.connection,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            content="Decision: keep\nScore: 4.5\nStrong fit.",
            source="test",
            status="interested",
        )
        self.connection.commit()

        html = web.render_review_queue(self.db_path, filter_value="has_feedback")

        self.assertIn('<div class="user-score"><span>Your score</span><strong>4.5/5</strong></div>', html)

    def test_review_queue_needs_review_excludes_structured_feedback(self):
        paper_id, recommendation_id = self._seed_review_recommendation()
        ingest_feedback_blob(
            self.connection,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            content="Decision: keep\nScore: 4\nAlready scored.",
            source="test",
            status="reviewed",
        )
        self.connection.commit()

        needs_review_html = web.render_review_queue(self.db_path, filter_value="needs_review")
        scored_html = web.render_review_queue(self.db_path, filter_value="has_feedback")

        self.assertNotIn("Dense Review Paper", needs_review_html)
        self.assertIn("Dense Review Paper", scored_html)
        self.assertIn('<div class="user-score"><span>Your score</span><strong>4/5</strong></div>', scored_html)

    def test_review_queue_has_feedback_filter_includes_structured_feedback_without_reviewed_status(self):
        recommended_paper_id, _ = self._seed_review_recommendation()
        feedback_paper_id, _ = db.upsert_paper(
            self.connection,
            {
                "source": "semantic_scholar",
                "source_id": "feedback-only",
                "title": "Feedback Only Paper",
                "url": "https://example.test/feedback-only",
                "published": "2026-08-20",
                "abstract": "AIOps root cause analysis.",
            },
        )
        ingest_feedback_blob(
            self.connection,
            paper_id=feedback_paper_id,
            recommendation_id=None,
            content="Decision: keep\nScore: 4.5\nStrong feedback-only fit.",
            source="test",
            status="interested",
        )
        self.connection.commit()

        default_html = web.render_review_queue(self.db_path)
        feedback_html = web.render_review_queue(self.db_path, filter_value="has_feedback")

        self.assertIn("Dense Review Paper", default_html)
        self.assertNotIn("Feedback Only Paper", default_html)
        self.assertIn('<option value="all">All papers</option><option value="has_feedback" selected>Scored</option>', feedback_html)
        self.assertIn("Feedback Only Paper", feedback_html)
        self.assertIn("Semantic Scholar", feedback_html)
        self.assertIn('<div class="user-score"><span>Your score</span><strong>4.5/5</strong></div>', feedback_html)
        self.assertIn("Decision: keep", feedback_html)
        self.assertIn("Feedback:", feedback_html)
        self.assertNotEqual(recommended_paper_id, feedback_paper_id)

    def test_review_queue_has_feedback_filter_includes_raw_feedback_without_structured_row(self):
        feedback_paper_id, _ = db.upsert_paper(
            self.connection,
            {
                "source": "openalex",
                "source_id": "raw-feedback-only",
                "title": "Raw Feedback Only Paper",
                "url": "https://example.test/raw-feedback-only",
                "published": "2026-08-20",
                "abstract": "Production operations feedback.",
            },
        )
        db.create_raw_feedback(self.connection, paper_id=feedback_paper_id, content="Raw feedback that did not parse.")
        self.connection.commit()

        html = web.render_review_queue(self.db_path, filter_value="has_feedback")

        self.assertIn("Raw Feedback Only Paper", html)
        self.assertIn("Feedback:", html)
        self.assertIn("Raw feedback that did not parse.</textarea>", html)

    def test_review_queue_newest_sort_uses_pulled_date(self):
        older_paper_id, _ = self._seed_review_recommendation(source_id="2607.olderv1")
        newer_paper_id, _ = self._seed_review_recommendation(source_id="2607.newerv1")
        self.connection.execute(
            "UPDATE papers SET first_discovered_at = ? WHERE id = ?",
            ("2026-07-30 05:00:00", older_paper_id),
        )
        self.connection.execute(
            "UPDATE papers SET first_discovered_at = ? WHERE id = ?",
            ("2026-09-02 05:00:00", newer_paper_id),
        )
        self.connection.commit()

        cards = web.load_review_cards(self.db_path, filter_value="all", source_value="all", sort_value="latest")

        self.assertGreater(len(cards), 1)
        self.assertEqual(cards[0]["id"], newer_paper_id)
        self.assertEqual(cards[0]["pulled_at"], "2026-09-02 05:00:00")
        self.assertEqual(cards[1]["id"], older_paper_id)

    def test_review_queue_scored_filter_includes_lightweight_score_note(self):
        paper_id, _ = self._seed_review_recommendation()
        self.connection.execute(
            "INSERT INTO feedback (paper_id, status, notes) VALUES (?, ?, ?)",
            (paper_id, "reviewed", "Decision: keep\nScore: 4.5\nSaved before structured ingest."),
        )
        self.connection.commit()

        html = web.render_review_queue(self.db_path, filter_value="has_feedback")

        self.assertIn("Dense Review Paper", html)
        self.assertIn('<div class="user-score"><span>Your score</span><strong>4.5/5</strong></div>', html)

    def test_review_queue_legacy_reviewed_filter_falls_back_to_all(self):
        paper_id, _ = self._seed_review_recommendation()
        self.connection.execute(
            "INSERT INTO feedback (paper_id, status, notes) VALUES (?, ?, ?)",
            (paper_id, "reviewed", "Marked reviewed."),
        )
        self.connection.commit()

        html = web.render_review_queue(self.db_path, filter_value="reviewed")

        self.assertNotIn('<option value="reviewed"', html)
        self.assertIn("All papers | All sources", html)
        self.assertIn("Dense Review Paper", html)


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

    def test_review_queue_shows_recommended_non_arxiv_without_pdf_or_triage(self):
        paper_id, _ = db.upsert_paper(
            self.connection,
            {
                "source": "semantic_scholar",
                "source_id": "semantic-no-pdf",
                "title": "Semantic Paper Without PDF",
                "url": "https://www.semanticscholar.org/paper/semantic-no-pdf",
                "published": "2026-08-20",
                "abstract": "Debugging agent workflows.",
                "pdf_url": None,
            },
        )
        curator_run_id = db.create_curator_run(
            self.connection,
            workflow_cycle_id=self.cycle_id,
            profile_version_id=self.profile_id,
            scout_attempt_count=1,
            max_scout_attempts=1,
            min_quality_score=1,
            max_recommendations=1,
            model="test",
        )
        db.insert_curator_evaluation(
            self.connection,
            curator_run_id=curator_run_id,
            paper_id=paper_id,
            scout_candidate_id=None,
            score=80,
            rationale="Strong match.",
            matched_signals=["debugging"],
            quality_threshold_met=True,
        )
        db.insert_recommendation(
            self.connection,
            curator_run_id=curator_run_id,
            paper_id=paper_id,
            recommendation_order=1,
            rationale="Strong match.",
        )
        self.connection.commit()

        default_html = web.render_review_queue(self.db_path)

        self.assertIn("Semantic Paper Without PDF", default_html)

    def test_review_queue_does_not_render_open_pdf_for_non_arxiv_doi_url(self):
        paper_id, _ = db.upsert_paper(
            self.connection,
            {
                "source": "semantic_scholar",
                "source_id": "semantic-doi-as-pdf",
                "title": "Semantic Paper With DOI Pretending To Be PDF",
                "url": "https://www.semanticscholar.org/paper/semantic-doi-as-pdf",
                "published": "2026-08-20",
                "abstract": "Debugging agent workflows.",
                "pdf_url": "https://doi.org/10.1145/3706598.3713581",
            },
        )
        curator_run_id = db.create_curator_run(
            self.connection,
            workflow_cycle_id=self.cycle_id,
            profile_version_id=self.profile_id,
            scout_attempt_count=1,
            max_scout_attempts=1,
            min_quality_score=1,
            max_recommendations=1,
            model="test",
        )
        db.insert_curator_evaluation(
            self.connection,
            curator_run_id=curator_run_id,
            paper_id=paper_id,
            scout_candidate_id=None,
            score=80,
            rationale="Strong match.",
            matched_signals=["debugging"],
            quality_threshold_met=True,
        )
        db.insert_recommendation(
            self.connection,
            curator_run_id=curator_run_id,
            paper_id=paper_id,
            recommendation_order=1,
            rationale="Strong match.",
        )
        self.connection.commit()

        default_html = web.render_review_queue(self.db_path)

        self.assertIn("Semantic Paper With DOI Pretending To Be PDF", default_html)
        self.assertNotIn(
            'class="metadata-action pdf-action" href="https://doi.org/10.1145/3706598.3713581"',
            default_html,
        )

    def test_review_queue_summary_formats_structured_fields_and_truncates_abstract(self):
        html = web.render_summary(
            {
                "research_problem": "Deployment safety",
                "why_it_matters": None,
                "approach": [
                    {
                        "name": "Tiered rollouts",
                        "description": "Changes are tested before broad production rollout.",
                    },
                    {
                        "name": "Health checks",
                        "description": "Automated checks gate each rollout phase.",
                    },
                ],
            },
            compact=False,
        )

        self.assertIn("Tiered rollouts: Changes are tested before broad production rollout.", html)
        self.assertIn("Health checks: Automated checks gate each rollout phase.", html)
        self.assertNotIn("[{", html)
        self.assertNotIn("&#x27;name&#x27;", html)

        json_string_html = web.render_summary(
            {
                "research_problem": "Deployment safety",
                "approach": (
                    '[{"name": "Tiered rollouts", '
                    '"description": "Changes are tested before broad production rollout."}]'
                ),
            },
            compact=False,
        )
        self.assertIn("Tiered rollouts: Changes are tested before broad production rollout.", json_string_html)
        self.assertNotIn("[{", json_string_html)

        nested_json_html = web.render_summary(
            {
                "research_problem": "Agent benchmarks",
                "approach": (
                    '{"reasoning-process": {"methodology": ["Two large-scale datasets are released"], '
                    '"evaluation": ["Localized the fault occurrence", "Identified the root cause"]}}'
                ),
            },
            compact=False,
        )
        self.assertIn("Reasoning Process:", nested_json_html)
        self.assertIn("Methodology: Two large-scale datasets are released", nested_json_html)
        self.assertIn("Evaluation: Localized the fault occurrence; Identified the root cause", nested_json_html)
        self.assertNotIn("&quot;reasoning-process&quot;", nested_json_html)
        self.assertNotIn("{", nested_json_html)

        long_abstract = " ".join(["observability"] * 200)
        abstract_html = web.render_summary({"source_abstract": long_abstract}, compact=False)
        self.assertIn("Source Abstract", abstract_html)
        self.assertLess(len(abstract_html), len(long_abstract))
        self.assertIn("...", abstract_html)

        abstract_only_html = web.render_match_rationale(
            {
                "ranking_reason": "Matched incident response.",
                "matched_keywords": ["incident response"],
                "summary": {"abstract_only": True},
            },
            '<span class="tag">incident response</span>',
        )
        self.assertIn("Abstract-only triage", abstract_only_html)
        self.assertIn("Full PDF was not downloaded.", abstract_only_html)
        self.assertIn("Why this matches you", abstract_only_html)

        formula_html = web.render_summary(
            {
                "research_problem": "Deployment safety",
                "approach": "RLCR (y, q, y * ) = 1y=y* - (q - 1y=y*) 2 | {z } | {z } correction",
            },
            compact=False,
        )
        self.assertIn("<h3>Approach</h3><p>Not extracted yet.</p>", formula_html)
        self.assertNotIn("RLCR", formula_html)

    def test_topics_page_renders_editable_topic_manager(self):
        config_path = Path(self.tmp.name) / "topics.yaml"
        save_topic_config(
            [
                TopicEntry(
                    id="datalake-operations",
                    label="Datalake operations",
                    query="datalake operations reliability observability production engineering",
                    sources=["arxiv", "semantic_scholar", "openalex"],
                )
            ],
            config_path,
        )

        html = web.render_topics_page(config_path=config_path)

        self.assertIn("Topic Agent", html)
        self.assertIn('name="request_text"', html)
        self.assertIn("Ask TopicAgent", html)
        self.assertNotIn("<summary>Advanced</summary>", html)
        self.assertIn('href="/topics?edit=datalake-operations"', html)
        self.assertIn('class="topic-row topic-read-row"', html)
        self.assertIn('class="topic-source topic-list-details"', html)
        self.assertIn("1 configured | expand to search, toggle, or edit", html)
        self.assertIn('title="datalake operations reliability observability production engineering"', html)
        self.assertNotIn('class="topic-source topic-editor-panel"', html)
        self.assertNotIn("Topic changes apply to future scheduled runs.", html)
        self.assertNotIn('class="topic-note"', html)
        self.assertIn("Datalake operations", html)
        self.assertLess(html.index("Source schedule inventory"), html.index("All Topics"))
        self.assertIn('class="topic-inventory-details" open', html)
        self.assertIn('<a class="brand-home" href="/">', html)

        saved_html = web.render_topics_page(config_path=config_path, saved=True)
        self.assertIn("Saved. Changes apply to future scheduled runs.", saved_html)

    def test_topics_page_shows_single_selected_editor_panel(self):
        config_path = Path(self.tmp.name) / "topics.yaml"
        save_topic_config(
            [
                TopicEntry("datalake-operations", "Datalake operations", "datalake query", ["arxiv"]),
                TopicEntry("incident-response", "Incident response", "incident query", ["openalex"]),
            ],
            config_path,
        )

        html = web.render_topics_page(config_path=config_path, edit_id="datalake-operations")

        self.assertIn('class="topic-source topic-editor-panel"', html)
        self.assertIn('class="topic-source topic-list-details" open', html)
        self.assertIn('<input type="hidden" name="topic_id" value="datalake-operations">', html)
        self.assertIn('href="/topics">Cancel</a>', html)
        self.assertIn('href="/topics?edit=incident-response"', html)

    def test_topics_post_adds_fast_path_topic(self):
        config_path = Path(self.tmp.name) / "topics.yaml"
        save_topic_config([], config_path)

        web.save_topics_form(
            {
                "action": ["add"],
                "topic_text": ["datalake operations"],
            },
            config_path=config_path,
        )

        loaded = load_topic_config(config_path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].label, "Datalake operations")
        self.assertEqual(loaded[0].query, "datalake operations reliability observability production engineering")
        self.assertEqual(loaded[0].sources, ["arxiv", "semantic_scholar", "openalex"])
        self.assertEqual(loaded[0].cadence, "daily")
        self.assertEqual(loaded[0].priority, "normal")
        self.assertTrue(loaded[0].enabled)

    def test_topic_agent_create_new_proposal_from_mocked_ollama(self):
        def provider(url, model, prompt, timeout):
            self.assertIn("datalake reliability", prompt)
            return {
                "action": "create_new",
                "matched_topic_id": None,
                "label": "Datalake reliability",
                "query": "datalake reliability observability production engineering",
                "sources": ["arxiv", "openalex"],
                "cadence": "daily",
                "priority": "normal",
                "enabled": True,
                "rationale": "Relevant to operations reliability.",
                "source_rationale": "arXiv and OpenAlex cover systems research.",
            }

        proposal = suggest_topic_proposal("datalake reliability", [], provider=provider, model="test-qwen")

        self.assertEqual(proposal.action, "create_new")
        self.assertEqual(proposal.label, "Datalake reliability")
        self.assertEqual(proposal.sources, ["arxiv", "openalex"])
        self.assertEqual(proposal.provider, "ollama")
        self.assertEqual(proposal.model, "test-qwen")

    def test_topic_agent_prompt_includes_conversation_turns(self):
        def provider(url, model, prompt, timeout):
            self.assertIn("data lake or lakehouse", prompt)
            self.assertIn("lakehouse reliability", prompt)
            self.assertIn("Infer the user's intent semantically", prompt)
            return {
                "action": "create_new",
                "label": "Lakehouse reliability",
                "query": "lakehouse reliability observability production engineering",
                "sources": ["openalex"],
                "cadence": "daily",
                "priority": "normal",
                "enabled": True,
            }

        proposal = suggest_topic_proposal(
            "lakehouse reliability",
            [],
            conversation=[
                {"role": "user", "content": "datalake"},
                {"role": "agent", "content": "Do you mean data lake or lakehouse operations?"},
            ],
            provider=provider,
            model="test-qwen",
        )

        self.assertEqual(proposal.label, "Lakehouse reliability")

    def test_topic_agent_llm_can_remove_without_remove_keyword(self):
        topics = [
            TopicEntry(
                "datalake-operations",
                "Datalake operations",
                "datalake operations reliability observability",
                ["arxiv", "openalex"],
                enabled=True,
            )
        ]

        def provider(url, model, prompt, timeout):
            self.assertIn("Infer the user's intent semantically", prompt)
            self.assertNotIn("If the user asks to remove, delete, or drop", prompt)
            return {
                "action": "remove_existing",
                "matched_topic_id": "datalake-operations",
                "label": "Datalake operations",
                "query": "datalake operations reliability observability",
                "sources": ["arxiv", "openalex"],
                "cadence": "daily",
                "priority": "normal",
                "enabled": True,
                "rationale": "The user no longer wants this topic managed.",
                "source_rationale": "The topic is being removed from all sources.",
            }

        proposal = suggest_topic_proposal("I do not care about datalake operations anymore", topics, provider=provider)

        self.assertEqual(proposal.action, "remove_existing")
        self.assertEqual(proposal.matched_topic_id, "datalake-operations")

    def test_topic_agent_invalid_json_falls_back_to_deterministic_proposal(self):
        def provider(url, model, prompt, timeout):
            raise ValueError("bad json")

        proposal = suggest_topic_proposal("datalake operations", [], provider=provider, model="test-qwen")

        self.assertEqual(proposal.action, "create_new")
        self.assertEqual(proposal.label, "Datalake operations")
        self.assertEqual(proposal.provider, "deterministic_fallback")
        self.assertEqual(proposal.sources, ["arxiv", "semantic_scholar", "openalex"])

    def test_topic_agent_duplicate_becomes_update_proposal(self):
        topics = [TopicEntry("datalake-operations", "Datalake operations", "datalake query", ["arxiv"])]

        proposal = suggest_topic_proposal(
            "datalake operations",
            topics,
            provider=lambda url, model, prompt, timeout: {"not": "valid"},
        )

        self.assertEqual(proposal.action, "update_existing")
        self.assertEqual(proposal.matched_topic_id, "datalake-operations")
        self.assertEqual(proposal.label, "Datalake operations")

    def test_topic_agent_remove_request_removes_existing_topic(self):
        topics = [
            TopicEntry(
                "datalake-operations",
                "Datalake operations",
                "datalake operations reliability observability",
                ["arxiv", "openalex"],
                enabled=True,
            )
        ]

        proposal = suggest_topic_proposal(
            "remove datalake operations",
            topics,
            provider=lambda url, model, prompt, timeout: {"not": "valid"},
        )

        self.assertEqual(proposal.action, "remove_existing")
        self.assertEqual(proposal.matched_topic_id, "datalake-operations")
        self.assertIn("removing this topic from the topic config", proposal.rationale)

    def test_topic_agent_delete_request_removes_existing_topic(self):
        topics = [
            TopicEntry(
                "datalake-operations",
                "Datalake operations",
                "datalake operations reliability observability",
                ["arxiv", "openalex"],
                enabled=True,
            )
        ]

        proposal = suggest_topic_proposal(
            "delete datalake operations",
            topics,
            provider=lambda url, model, prompt, timeout: {"not": "valid"},
        )

        self.assertEqual(proposal.action, "remove_existing")
        self.assertEqual(proposal.matched_topic_id, "datalake-operations")
        self.assertIn("removing this topic from the topic config", proposal.rationale)

    def test_topic_agent_disable_request_disables_existing_topic(self):
        topics = [
            TopicEntry(
                "datalake-operations",
                "Datalake operations",
                "datalake operations reliability observability",
                ["arxiv", "openalex"],
                enabled=True,
            )
        ]

        proposal = suggest_topic_proposal(
            "disable datalake operations",
            topics,
            provider=lambda url, model, prompt, timeout: {"not": "valid"},
        )

        self.assertEqual(proposal.action, "update_existing")
        self.assertEqual(proposal.matched_topic_id, "datalake-operations")
        self.assertFalse(proposal.enabled)

    def test_topic_agent_fallback_asks_when_change_intent_is_ambiguous(self):
        topics = [
            TopicEntry(
                "datalake-operations",
                "Datalake operations",
                "datalake operations reliability observability",
                ["arxiv", "openalex"],
                enabled=True,
            )
        ]

        proposal = suggest_topic_proposal(
            "I do not care about datalake operations anymore",
            topics,
            provider=lambda url, model, prompt, timeout: {"not": "valid"},
        )

        self.assertEqual(proposal.action, "ask_clarifying_question")
        self.assertIn("remove Datalake operations", proposal.question)
        self.assertIn("disable it", proposal.question)

    def test_topic_agent_validates_update_matched_topic(self):
        topics = [TopicEntry("datalake-operations", "Datalake operations", "datalake query", ["arxiv"])]

        proposal = validate_topic_proposal(
            {
                "action": "update_existing",
                "matched_topic_id": "datalake-operations",
                "label": "Datalake operations",
                "query": "datalake reliability observability",
                "sources": ["openalex", "semantic_scholar"],
                "cadence": "weekly",
                "priority": "high",
                "enabled": True,
            },
            topics,
        )

        self.assertEqual(proposal.action, "update_existing")
        self.assertEqual(proposal.sources, ["semantic_scholar", "openalex"])

    def test_topic_agent_apply_proposal_writes_config(self):
        config_path = Path(self.tmp.name) / "topics.yaml"
        topics = [TopicEntry("datalake-operations", "Datalake operations", "datalake query", ["arxiv"])]
        save_topic_config(topics, config_path)

        proposal = TopicProposal(
            action="update_existing",
            matched_topic_id="datalake-operations",
            label="Datalake operations",
            query="datalake reliability observability",
            sources=["openalex"],
            cadence="weekly",
            priority="high",
            enabled=True,
        )
        web.apply_topic_proposal_form(
            {"proposal_json": [topic_proposal_to_json(proposal)]},
            config_path=config_path,
        )

        loaded = load_topic_config(config_path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].query, "datalake reliability observability")
        self.assertEqual(loaded[0].sources, ["openalex"])
        self.assertEqual(loaded[0].cadence, "weekly")
        self.assertEqual(loaded[0].priority, "high")

    def test_topic_agent_apply_disable_proposal_disables_topic(self):
        topics = [TopicEntry("datalake-operations", "Datalake operations", "datalake query", ["arxiv"], enabled=True)]
        proposal = TopicProposal(
            action="update_existing",
            matched_topic_id="datalake-operations",
            label="Datalake operations",
            query="datalake query",
            sources=["arxiv"],
            cadence="daily",
            priority="normal",
            enabled=False,
        )

        updated = apply_topic_proposal(proposal, topics)

        self.assertEqual(len(updated), 1)
        self.assertFalse(updated[0].enabled)

    def test_topic_agent_apply_remove_proposal_deletes_topic(self):
        topics = [TopicEntry("datalake-operations", "Datalake operations", "datalake query", ["arxiv"], enabled=True)]
        proposal = TopicProposal(
            action="remove_existing",
            matched_topic_id="datalake-operations",
            label="Datalake operations",
            query="datalake query",
            sources=["arxiv"],
            cadence="daily",
            priority="normal",
            enabled=True,
        )

        updated = apply_topic_proposal(proposal, topics)

        self.assertEqual(updated, [])

    def test_topic_agent_apply_update_prevents_duplicate_collision(self):
        topics = [
            TopicEntry("datalake-operations", "Datalake operations", "datalake query", ["arxiv"]),
            TopicEntry("incident-response", "Incident response", "incident query", ["openalex"]),
        ]
        proposal = TopicProposal(
            action="update_existing",
            matched_topic_id="incident-response",
            label="Datalake operations",
            query="incident query",
            sources=["openalex"],
            cadence="daily",
            priority="normal",
            enabled=True,
        )

        with self.assertRaises(DuplicateTopicError):
            apply_topic_proposal(proposal, topics)

    def test_topics_page_renders_topic_agent_proposal(self):
        proposal = TopicProposal(
            action="create_new",
            label="Datalake reliability",
            query="datalake reliability observability",
            sources=["arxiv", "openalex"],
            cadence="daily",
            priority="normal",
            enabled=True,
            rationale="Good fit.",
            source_rationale="Use systems sources.",
        )

        html = web.render_topics_page(proposal=proposal, request_text="datalake reliability")

        self.assertIn("What do you want Project Paper to scout?", html)
        self.assertIn("TopicAgent Proposal", html)
        self.assertIn("Datalake reliability", html)
        self.assertIn('name="proposal_json"', html)
        self.assertIn("Apply and save", html)
        self.assertNotIn('name="topic_text"', html)
        self.assertLess(html.index("What do you want Project Paper to scout?"), html.index("TopicAgent Proposal"))
        self.assertLess(html.index("TopicAgent Proposal"), html.index("Source schedule inventory"))
        self.assertNotIn('class="topic-source topic-proposal-panel"', html)
        self.assertIn('name="conversation_json"', html)
        self.assertIn("New pending", html)
        self.assertIn("showing pending TopicAgent preview", html)
        self.assertIn('class="topic-row topic-read-row topic-preview-row"', html)

    def test_topic_agent_form_allows_explicit_source_selection(self):
        html = web.render_topics_page()

        self.assertIn('name="sources" value="arxiv" checked', html)
        self.assertIn('name="sources" value="semantic_scholar" checked', html)
        self.assertIn('name="sources" value="openalex" checked', html)

    def test_topic_agent_form_overrides_proposal_sources(self):
        config_path = Path(self.tmp.name) / "topics.yaml"
        save_topic_config([], config_path)
        proposal = TopicProposal(
            action="create_new",
            label="Cloud incident response",
            query="llm cloud incident response",
            sources=["arxiv", "semantic_scholar", "openalex"],
        )
        with patch("paper_agents.web.suggest_topic_proposal", return_value=proposal):
            result = web.propose_topic_form(
                {"request_text": ["LLM cloud incident response"], "sources": ["semantic_scholar"]},
                config_path=config_path,
            )

        self.assertEqual(result.sources, ["semantic_scholar"])
        self.assertIn("Sources selected", result.source_rationale)

    def test_topic_agent_form_requires_a_source_selection(self):
        config_path = Path(self.tmp.name) / "topics.yaml"
        save_topic_config([], config_path)

        with self.assertRaisesRegex(ValueError, "Select at least one source"):
            web.propose_topic_form({"request_text": ["LLM cloud incident response"]}, config_path=config_path)

    def test_topics_page_previews_update_proposal_in_topic_list(self):
        config_path = Path(self.tmp.name) / "topics.yaml"
        save_topic_config(
            [TopicEntry("datalake-operations", "Datalake operations", "datalake query", ["arxiv"], enabled=True)],
            config_path,
        )
        proposal = TopicProposal(
            action="update_existing",
            matched_topic_id="datalake-operations",
            label="Datalake operations",
            query="datalake query",
            sources=["arxiv"],
            cadence="daily",
            priority="normal",
            enabled=False,
        )

        html = web.render_topics_page(proposal=proposal, config_path=config_path)

        self.assertIn("Update pending", html)
        self.assertIn("Apply to save", html)
        self.assertIn('<span class="topic-status status-disabled">Off</span>', html)
        self.assertIn('class="topic-source topic-list-details" open', html)

    def test_topics_page_previews_remove_proposal_in_topic_list(self):
        config_path = Path(self.tmp.name) / "topics.yaml"
        save_topic_config(
            [TopicEntry("datalake-operations", "Datalake operations", "datalake query", ["arxiv"], enabled=True)],
            config_path,
        )
        proposal = TopicProposal(
            action="remove_existing",
            matched_topic_id="datalake-operations",
            label="Datalake operations",
            query="datalake query",
            sources=["arxiv"],
            cadence="daily",
            priority="normal",
            enabled=True,
        )

        html = web.render_topics_page(proposal=proposal, config_path=config_path)

        self.assertIn("Remove pending", html)
        self.assertIn("Apply to remove", html)
        self.assertIn("Remove topic", html)
        self.assertIn("Will remove", html)

    def test_topics_page_renders_topic_agent_conversation(self):
        proposal = TopicProposal(
            action="ask_clarifying_question",
            question="Do you mean lakehouse reliability or data pipeline incidents?",
            rationale="The request was broad.",
        )

        html = web.render_topics_page(
            proposal=proposal,
            conversation=[
                {"role": "user", "content": "datalake"},
                {"role": "agent", "content": "Do you mean lakehouse reliability or data pipeline incidents?"},
            ],
        )

        self.assertIn("TopicAgent conversation", html)
        self.assertIn("Reply to TopicAgent", html)
        self.assertIn("Do you mean lakehouse reliability", html)
        self.assertIn('name="conversation_json"', html)

    def test_topic_agent_conversation_form_continues_after_question(self):
        def provider(url, model, prompt, timeout):
            self.assertIn("data pipeline incidents", prompt)
            return {
                "action": "create_new",
                "label": "Data pipeline incidents",
                "query": "data pipeline incidents reliability observability production engineering",
                "sources": ["arxiv", "openalex"],
                "cadence": "daily",
                "priority": "normal",
                "enabled": True,
            }

        conversation = [
            {"role": "user", "content": "datalake"},
            {"role": "agent", "content": "Do you mean lakehouse reliability or data pipeline incidents?"},
        ]
        proposal = suggest_topic_proposal(
            "data pipeline incidents",
            [],
            conversation=conversation,
            provider=provider,
            model="test-qwen",
        )
        updated = web.append_topic_agent_turns(conversation, "data pipeline incidents", proposal)

        self.assertEqual(proposal.label, "Data pipeline incidents")
        self.assertEqual(updated[-2]["content"], "data pipeline incidents")
        self.assertIn("I prepared a topic proposal", updated[-1]["content"])

    def test_topics_post_rejects_duplicate_add(self):
        config_path = Path(self.tmp.name) / "topics.yaml"
        save_topic_config(
            [TopicEntry("datalake-operations", "Datalake operations", "datalake query", ["arxiv"])],
            config_path,
        )

        with self.assertRaisesRegex(DuplicateTopicError, "Topic already exists: Datalake operations") as captured:
            web.save_topics_form(
                {
                    "action": ["add"],
                    "topic_text": ["  datalake   OPERATIONS  "],
                    "sources": ["arxiv"],
                    "cadence": ["daily"],
                    "priority": ["normal"],
                    "enabled": ["1"],
                },
                config_path=config_path,
            )

        loaded = load_topic_config(config_path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(captured.exception.topic.id, "datalake-operations")

        html = web.render_topics_page(config_path=config_path, duplicate=True, edit_id=captured.exception.topic.id)
        self.assertIn("Topic already exists. Editing existing topic: Datalake operations.", html)
        self.assertIn('class="topic-source topic-editor-panel" id="topic-editor"', html)
        self.assertIn('name="label" value="Datalake operations" required autofocus', html)

    def test_topics_post_rejects_duplicate_edit(self):
        config_path = Path(self.tmp.name) / "topics.yaml"
        save_topic_config(
            [
                TopicEntry("datalake-operations", "Datalake operations", "datalake query", ["arxiv"]),
                TopicEntry("incident-response", "Incident response", "incident query", ["openalex"]),
            ],
            config_path,
        )

        with self.assertRaisesRegex(ValueError, "Topic already exists: Datalake operations"):
            web.save_topics_form(
                {
                    "action": ["update"],
                    "topic_id": ["incident-response"],
                    "label": ["Incident response"],
                    "query": [" DATALAKE   query "],
                    "sources": ["openalex"],
                    "cadence": ["daily"],
                    "priority": ["normal"],
                    "enabled": ["1"],
                },
                config_path=config_path,
            )

        loaded = load_topic_config(config_path)
        self.assertEqual(loaded[1].query, "incident query")

    def test_topics_post_toggles_enabled_from_read_mode(self):
        config_path = Path(self.tmp.name) / "topics.yaml"
        save_topic_config(
            [TopicEntry("datalake-operations", "Datalake operations", "datalake query", ["arxiv"], enabled=True)],
            config_path,
        )

        web.save_topics_form(
            {
                "action": ["toggle"],
                "topic_id": ["datalake-operations"],
                "enabled": ["0"],
            },
            config_path=config_path,
        )

        loaded = load_topic_config(config_path)
        self.assertFalse(loaded[0].enabled)

    def test_cleanup_legacy_feedback_statuses_preserves_not_interested(self):
        paper_id, _ = self._seed_review_recommendation()
        for status in ["read_later", "interested", "reviewed", "not_interested"]:
            self.connection.execute(
                "INSERT INTO feedback (paper_id, status, notes) VALUES (?, ?, ?)",
                (paper_id, status, f"{status} note"),
            )
        self.connection.commit()

        dry_run = db.cleanup_legacy_feedback_statuses(self.db_path)
        self.assertEqual(dry_run["rows_matched"], 3)
        self.assertEqual(dry_run["rows_deleted"], 0)

        applied = db.cleanup_legacy_feedback_statuses(self.db_path, dry_run=False)
        self.assertEqual(applied["rows_deleted"], 3)
        rows = self.connection.execute("SELECT status, notes FROM feedback ORDER BY id").fetchall()
        self.assertEqual(rows, [("not_interested", "not_interested note")])

    def test_review_queue_quick_status_controls_are_status_only(self):
        self._seed_review_recommendation()
        self.connection.commit()

        html = web.render_review_queue(self.db_path)

        self.assertIn('name="action" value="feedback"', html)
        self.assertNotIn('data-quick-status="1">Not interested</button>', html)
        self.assertNotIn('data-quick-status="1">Read later</button>', html)
        self.assertIn('<span class="submit-state" aria-live="polite"></span>', html)

    def test_review_queue_status_only_save_does_not_ingest_existing_notes(self):
        paper_id, recommendation_id = self._seed_review_recommendation()
        self.connection.commit()
        calls = []

        result = web.save_feedback(
            self.db_path,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            status="not_interested",
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

    def test_review_queue_feedback_save_defaults_lightweight_status_to_reviewed(self):
        paper_id, recommendation_id = self._seed_review_recommendation()
        self.connection.commit()

        result = web.save_feedback(
            self.db_path,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            status=None,
            notes="Decision: keep\nScore: 4.5\nGood fit.",
            feedback_content="Decision: keep\nScore: 4.5\nGood fit.\n",
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

        lightweight = self.connection.execute("SELECT status, notes FROM feedback WHERE paper_id = ?", (paper_id,)).fetchone()
        structured = self.connection.execute("SELECT decision, score FROM structured_feedback WHERE paper_id = ?", (paper_id,)).fetchone()
        self.assertTrue(result["feedback_ingested"])
        self.assertEqual(lightweight, ("reviewed", "Decision: keep\nScore: 4.5\nGood fit."))
        self.assertEqual(structured, ("keep", 4.5))

    def test_review_queue_feedback_save_can_queue_profile_apply(self):
        paper_id, recommendation_id = self._seed_review_recommendation()
        self.connection.commit()
        provider_started = threading.Event()

        def provider(payload, model):
            provider_started.set()
            return {
                "profile": {
                    "interests": ["background-applied"],
                    "positive_signals": ["good fit"],
                    "negative_signals": [],
                    "notes": "Applied from background worker.",
                },
                "change_summary": "Background-applied UI feedback.",
            }

        result = web.save_feedback(
            self.db_path,
            paper_id=paper_id,
            recommendation_id=recommendation_id,
            status="interested",
            notes="Decision: keep\nScore: 4.5\nGood fit.",
            feedback_content="Decision: keep\nScore: 4.5\nGood fit.\n",
            source="review_queue_ui",
            profile_provider_fn=provider,
            profile_apply_mode="background",
        )

        self.assertTrue(result["feedback_saved"])
        self.assertTrue(result["feedback_ingested"])
        self.assertTrue(result["profile_apply_queued"])
        self.assertIsNone(result["profile_apply"])
        self.assertEqual(
            self.connection.execute("SELECT status, notes FROM feedback WHERE paper_id = ?", (paper_id,)).fetchone(),
            ("interested", "Decision: keep\nScore: 4.5\nGood fit."),
        )
        self.assertTrue(provider_started.wait(timeout=2))
        deadline = time.time() + 2
        while time.time() < deadline:
            current = db.current_profile_version(self.connection)
            if current["profile"].get("interests") == ["background-applied"]:
                break
            time.sleep(0.05)
        current = db.current_profile_version(self.connection)
        self.assertEqual(current["profile"]["interests"], ["background-applied"])
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
        self.assertEqual(
            self.connection.execute("SELECT status, notes FROM feedback WHERE paper_id = ?", (paper_id,)).fetchone(),
            ("interested", "Decision: keep\nScore: 4\nGood fit."),
        )
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
            status="not_interested",
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

    def test_health_summary_does_not_warn_for_unbackfillable_missing_triage_summary(self):
        paper_id, _ = db.upsert_paper(
            self.connection,
            {
                "source": "semantic_scholar",
                "source_id": "semantic-doi-only",
                "title": "Semantic DOI Only Paper",
                "url": "https://www.semanticscholar.org/paper/semantic-doi-only",
                "pdf_url": "https://doi.org/10.1145/example",
                "published": "2026-08-20",
                "abstract": "Interactive debugging for agent systems.",
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
            score=44.5,
            rationale="Matched debugging.",
            matched_signals=["debugging"],
            quality_threshold_met=True,
        )
        db.insert_recommendation(
            self.connection,
            curator_run_id=curator_run_id,
            paper_id=paper_id,
            recommendation_order=1,
            rationale="Matched debugging.",
        )
        db.update_workflow_state(self.connection, self.cycle_id, "awaiting_manual_discussion")
        self.connection.commit()

        summary = db.health_summary(self.db_path, days=21)

        self.assertEqual(summary["artifact_health"]["missing_triage_summary_count"], 1)
        self.assertEqual(summary["artifact_health"]["backfillable_missing_triage_summary_count"], 0)
        messages = [warning["message"] for warning in summary["warnings"]]
        self.assertFalse(any("without triage summaries" in message for message in messages))

    def test_health_summary_warns_on_zero_eligible_scout_run(self):
        self._seed_scout_candidate(source="openalex", excluded=True, exclusion_reason="already_seen")
        self.connection.commit()

        summary = db.health_summary(self.db_path, days=21, source="openalex")

        self.assertEqual(summary["top"]["latest_zero_eligible_source"], "openalex")
        self.assertTrue(any("0 eligible" in warning["message"] for warning in summary["warnings"]))
        self.assertTrue(
            any(
                isinstance(warning.get("scout_run_id"), int)
                for warning in summary["warnings"]
            )
        )
        self.assertEqual(summary["source_breakdown"]["exclusion_reasons"][0]["reason"], "already_seen")

    def test_health_summary_warns_on_low_candidate_scout_run(self):
        scout_run_id, _, _ = self._seed_scout_candidate(source="arxiv", excluded=False, source_id="2607.lowv1")
        paper_id, is_new = db.upsert_paper(
            self.connection,
            {
                "source": "arxiv",
                "source_id": "2607.lowv2",
                "title": "Second low-volume arxiv health paper",
                "url": "https://example.test/2607.lowv2",
                "published": "2026-08-01",
                "abstract": "Microservice diagnosis for production engineering.",
            },
        )
        db.insert_scout_candidate(
            self.connection,
            scout_run_id=scout_run_id,
            paper_id=paper_id,
            retrieval_order=2,
            is_new=is_new,
            excluded=False,
            exclusion_reason=None,
            source_query="microservice diagnosis",
        )
        self.connection.commit()

        summary = db.health_summary(self.db_path, days=21, source="arxiv")

        self.assertTrue(
            any(
                "returned only 2 candidates" in warning["message"]
                and isinstance(warning.get("scout_run_id"), int)
                for warning in summary["warnings"]
            )
        )

    def test_health_summary_warns_for_latest_zero_eligible_run_per_source(self):
        self._seed_scout_candidate(source="openalex", excluded=True, source_id="W-zero", exclusion_reason="already_seen")
        self._seed_scout_candidate(source="semantic_scholar", excluded=False, source_id="S-new")
        self.connection.commit()

        summary = db.health_summary(self.db_path, days=21)

        self.assertEqual(summary["latest_scout_run"]["source"], "semantic_scholar")
        self.assertTrue(
            any(
                "openalex Scout run at " in warning["message"]
                and "had 0 eligible candidates" in warning["message"]
                and isinstance(warning.get("scout_run_id"), int)
                for warning in summary["warnings"]
            )
        )

    def test_health_omits_redundant_two_cycle_warning(self):
        self._seed_scout_candidate(source="openalex", excluded=True, exclusion_reason="history")
        db.create_workflow_cycle(self.connection, mode="test", max_scout_attempts=1)
        self.connection.commit()
        summary = db.health_summary(self.db_path)
        self.assertEqual(len(summary["recent_cycle_recommendations"]), 2)
        self.assertTrue(all(row["recommendation_count"] == 0 for row in summary["recent_cycle_recommendations"]))
        messages = [warning["message"] for warning in summary["warnings"]]
        self.assertFalse(any("two recent workflow cycles" in message for message in messages))
        self.assertTrue(any("openalex" in message and "0 eligible" in message for message in messages))
        self.assertNotIn("two recent workflow cycles", web.render_health_page(self.db_path))

    def test_health_summary_does_not_duplicate_failed_cycle_warning(self):
        db.update_workflow_state(self.connection, self.cycle_id, "failed")
        self.connection.commit()

        summary = db.health_summary(self.db_path, days=21)

        self.assertFalse(
            any("Latest workflow cycle is failed" in warning["message"] for warning in summary["warnings"])
        )

    def test_health_summary_warns_when_eligible_source_produces_no_recommendations(self):
        self._seed_scout_candidate(source="openalex", excluded=False, source_id="W-eligible")
        self.connection.commit()

        summary = db.health_summary(self.db_path, days=21, source="openalex")

        self.assertTrue(
            any(
                "1 eligible candidates and produced 0 recommendations" in warning["message"]
                and isinstance(warning.get("scout_run_id"), int)
                for warning in summary["warnings"]
            )
        )

    def test_health_summary_warns_when_source_has_underfilled_eligible_pool(self):
        self._seed_scout_candidate(source="arxiv", excluded=False, source_id="2607.underfilled")
        self.connection.commit()

        summary = db.health_summary(self.db_path, days=21, source="arxiv")

        self.assertTrue(
            any(
                "had only 1 eligible candidates and produced 0 recommendations; target pool: 10 eligible candidates" in warning["message"]
                and "arxiv Scout run at " in warning["message"]
                and isinstance(warning.get("scout_run_id"), int)
                for warning in summary["warnings"]
            )
        )

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
        messages = [warning["message"] for warning in summary["profile_maintenance"]]
        self.assertFalse(any("profile" in w["message"] for w in summary["warnings"]))
        html = web.render_health_page(self.db_path)
        self.assertIn("Profile maintenance / Needs attention", html)
        self.assertIn("current pending backlog, all dates", html)
        self.assertTrue(any("Gemini/profile apply failed" in message for message in messages))
        self.assertTrue(any("Structured feedback is waiting" in message for message in messages))

    def test_health_summary_clears_profile_apply_failure_after_success(self):
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
        profile_version_id = db.create_profile_version(
            self.connection,
            {"interests": ["operations"]},
            source_structured_feedback_id=ingest["structured_feedback_id"],
            change_summary="Applied feedback.",
        )
        db.create_feedback_profile_applications(
            self.connection,
            [ingest["structured_feedback_id"]],
            profile_version_id,
        )
        db.create_feedback_profile_apply_attempt(
            self.connection,
            provider="gemini",
            model="gemini-3.1-flash-lite",
            structured_feedback_ids=[ingest["structured_feedback_id"]],
            dry_run=False,
            status="succeeded",
            profile_version_id=profile_version_id,
        )
        self.connection.commit()

        summary = db.health_summary(self.db_path, days=21)

        self.assertEqual(summary["feedback_profile"]["unapplied_structured_feedback_count"], 0)
        self.assertEqual(summary["feedback_profile"]["recent_apply_failures_count"], 0)
        messages = [warning["message"] for warning in summary["profile_maintenance"]]
        self.assertFalse(any("Gemini/profile apply failed" in message for message in messages))
        self.assertFalse(any("Structured feedback is waiting" in message for message in messages))

    def test_health_scout_warnings_exclude_manual_and_stale_history(self):
        for source in ("arxiv", "openalex", "semantic_scholar", "manual_backfill", "manual"):
            self._seed_scout_candidate(source=source, excluded=True, source_id=source)
        self.connection.commit()
        summary = db.health_summary(self.db_path, days=90)
        messages = " ".join(w["message"] for w in summary["warnings"])
        for source in ("arxiv", "openalex", "semantic_scholar"):
            self.assertIn(f"{source} Scout run at ", messages)
        self.assertNotIn("manual", messages)
        self.assertIn("latest 3 scheduled Scout runs in the last 7 days", messages)
        self.assertEqual(len(summary["latest_scout_runs_by_source"]), 5)
        self.connection.execute("UPDATE scout_runs SET started_at = datetime('now', '-8 days')")
        self.connection.commit()
        summary = db.health_summary(self.db_path, days=90)
        self.assertFalse(any("Scout" in w["message"] for w in summary["warnings"]))
        manual = db.health_summary(self.db_path, source="manual_backfill")
        self.assertFalse(any("Scout" in w["message"] for w in manual["warnings"]))

    def test_health_scout_warning_recency_boundary(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        for source in ("arxiv", "openalex", "semantic_scholar"):
            self.assertTrue(db._is_recent_scheduled_scout({"source": source, "started_at": (now - timedelta(days=6, hours=23)).isoformat()}))
            self.assertFalse(db._is_recent_scheduled_scout({"source": source, "started_at": (now - timedelta(days=7, seconds=1)).isoformat()}))
        self.assertFalse(db._is_recent_scheduled_scout({"source": "arxiv", "started_at": "invalid"}))
        self.assertFalse(db._is_recent_scheduled_scout({"source": "arxiv", "started_at": (now + timedelta(days=1)).isoformat()}))
        self.assertEqual(web.render_profile_maintenance([]), "")
        self.assertIn("last 7 days", web.render_warnings([{"level": "warning", "message": "test"}]))

    def test_health_page_omits_empty_warning_banner(self):
        self.assertEqual(web.render_warnings([]), "")

    def test_health_page_renders_dashboard_controls(self):
        self._seed_scout_candidate(source="openalex", excluded=True, exclusion_reason="history")
        self.connection.commit()

        html = web.render_health_page(self.db_path, days=21, source_value="openalex")

        self.assertIn("Project Paper Health", html)
        self.assertIn('<form method="get" action="/health"', html)
        self.assertIn('href="/">Review Queue</a>', html)
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
        self.assertIn('href="/topics">Topics</a>', html)

    def test_topics_page_renders_source_topic_inventory(self):
        html = web.render_topics_page()

        self.assertIn("Project Paper Scout Topics", html)
        self.assertIn("Next scheduled topics (", html)
        self.assertIn("Configured topics (", html)
        self.assertNotIn("Topic changes apply to future scheduled runs.", html)
        self.assertIn("Source schedule inventory", html)
        self.assertIn("Topic Agent", html)
        self.assertIn('href="/">Review Queue</a>', html)
        self.assertIn('href="/health">Health</a>', html)
        self.assertIn("Daily default", html)
        self.assertIn("Rotating source job", html)
        self.assertIn("Source cron/manual job", html)
        self.assertIn("AIOps", html)
        self.assertIn("AIOps observability incident response", html)
        self.assertIn("AIOps root cause analysis", html)

    def test_topic_inventory_exposes_three_sources(self):
        inventory = scout_topic_inventory()

        self.assertEqual([item["source"] for item in inventory], ["arxiv", "openalex", "semantic_scholar"])
        openalex = next(item for item in inventory if item["source"] == "openalex")
        self.assertEqual([topic.query for topic in openalex["topics"]], OPENALEX_ROTATING_TOPICS)
        self.assertIn(openalex["active_topics"][0], OPENALEX_ROTATING_TOPICS)

    def _seed_review_recommendation(self, *, source_id: str = "2607.reviewv1") -> tuple[int, int]:
        paper_id, _ = db.upsert_paper(
            self.connection,
            {
                "source": "arxiv",
                "source_id": source_id,
                "title": "Dense Review Paper",
                "url": f"https://example.test/{source_id}",
                "pdf_url": f"https://example.test/{source_id}.pdf",
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

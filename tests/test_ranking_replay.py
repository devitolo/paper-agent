"""Opt-in private snapshot gates.

Run with PAPER_AGENT_QA_DB pointing to a collected snapshot (never the live DB):
python3 -m unittest discover -s tests -p test_ranking_replay.py -v
The snapshot is opened read-only. No paper text or feedback is committed here.
Relative-order gates are QA proposals for architect review, not numeric targets.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from paper_agents.curator_agent import evaluate_candidate
from paper_agents import db
from paper_agents.curator_rescore import rescore_recommendations


@unittest.skipUnless(os.environ.get("PAPER_AGENT_QA_DB"), "Set PAPER_AGENT_QA_DB to a private QA snapshot")
class ProductionRankingReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(os.environ["PAPER_AGENT_QA_DB"]).resolve()
        cls.snapshot_path = path
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            profile = connection.execute(
                "SELECT id, profile_json FROM profile_versions WHERE active=1 ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if profile is None:
                raise AssertionError("Snapshot must contain an active profile")
            cls.profile_id = profile["id"]
            cls.profile = json.loads(profile["profile_json"])
            cls.papers = [dict(row) for row in connection.execute("SELECT * FROM papers")]
            # Match the production Curator call: provenance affects V2 scoring.
            # Keep text-only V1 baselines in the private report, not in this gate.
            for paper in cls.papers:
                paper["evidence"] = db.paper_evidence_context(connection, paper["id"])
            cls.scored = [dict(row) for row in connection.execute("""
                SELECT p.*, sf.score AS user_score, sf.decision
                FROM papers p JOIN structured_feedback sf ON sf.paper_id=p.id
                WHERE sf.id=(SELECT MAX(f.id) FROM structured_feedback f WHERE f.paper_id=p.id)
                  AND sf.score IS NOT NULL
                ORDER BY p.id
            """)]
        cls.replay = {p["id"]: evaluate_candidate(p, cls.profile) for p in cls.papers}

    def test_snapshot_has_enough_feedback_for_relative_order_checks(self):
        self.assertGreaterEqual(len(self.scored), 10)
        self.assertTrue(any(p["decision"] == "reject" for p in self.scored))
        self.assertTrue(any(p["user_score"] >= 4 for p in self.scored))

    def test_active_profile_changes_at_least_one_curator_score(self):
        changed = [p["id"] for p in self.papers
                   if self.replay[p["id"]]["score"] != evaluate_candidate(p, {})["score"]]
        self.assertTrue(changed, f"Profile {self.profile_id} changes 0/{len(self.papers)} scores")

    def test_high_rated_papers_outrank_rejected_papers_on_average(self):
        high = [self.replay[p["id"]]["score"] for p in self.scored if p["user_score"] >= 4]
        rejected = [self.replay[p["id"]]["score"] for p in self.scored if p["decision"] == "reject"]
        self.assertTrue(high and rejected, "Need both high-rated and rejected feedback")
        self.assertGreater(sum(high) / len(high), sum(rejected) / len(rejected),
                           "QA proposed gate: highly rated papers should rank above rejects as a group")

    def test_cli_preview_filters_source_and_does_not_change_snapshot(self):
        before = hashlib.sha256(self.snapshot_path.read_bytes()).digest()
        result = subprocess.run(
            [sys.executable, "-m", "paper_agents.cli", "curator-rescore",
             "--db", str(self.snapshot_path), "--date", "2026-09-05",
             "--source", "semantic_scholar"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout[result.stdout.index("{"):])
        self.assertFalse(payload["applied"])
        self.assertEqual(payload["date_utc"], "2026-09-05")
        self.assertGreater(payload["recommendation_count"], 0)
        with closing(sqlite3.connect(self.snapshot_path.as_uri() + "?mode=ro", uri=True)) as c:
            expected = {row[0] for row in c.execute("""
                SELECT r.id FROM recommendations r
                WHERE date(r.created_at)='2026-09-05' AND EXISTS (
                    SELECT 1 FROM paper_sources ps
                    WHERE ps.paper_id=r.paper_id AND ps.source='semantic_scholar')
            """)}
        self.assertEqual({row["recommendation_id"] for row in payload["results"]}, expected)
        self.assertEqual(hashlib.sha256(self.snapshot_path.read_bytes()).digest(), before)

    def test_second_rescore_failure_rolls_back_first_update_and_audit_on_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "disposable.db"
            with closing(sqlite3.connect(self.snapshot_path.as_uri() + "?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(path)) as copy:
                    source.backup(copy)
                    before = list(copy.iterdump())
            calls = []

            def fail_second(candidate, profile):
                calls.append(candidate["paper_id"])
                if len(calls) == 2:
                    raise RuntimeError("QA injected second-row failure")
                return evaluate_candidate(candidate, profile)

            with patch("paper_agents.curator_rescore.evaluate_candidate", side_effect=fail_second):
                with self.assertRaisesRegex(RuntimeError, "second-row failure"):
                    rescore_recommendations(path, "2026-09-05", apply=True)
            self.assertEqual(len(calls), 2)
            with closing(sqlite3.connect(path)) as copy:
                self.assertEqual(list(copy.iterdump()), before,
                                 "Rollback must preserve all rows, including feedback and audit history")

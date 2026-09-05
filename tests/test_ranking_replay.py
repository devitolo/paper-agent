"""Opt-in private snapshot gates.

Run with PAPER_AGENT_QA_DB pointing to a collected snapshot (never the live DB):
python3 -m unittest discover -s tests -p test_ranking_replay.py -v
The snapshot is opened read-only. No paper text or feedback is committed here.
Relative-order gates are QA proposals for architect review, not numeric targets.
"""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import unittest

from paper_agents.curator_agent import evaluate_candidate
from paper_agents import db


@unittest.skipUnless(os.environ.get("PAPER_AGENT_QA_DB"), "Set PAPER_AGENT_QA_DB to a private QA snapshot")
class ProductionRankingReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(os.environ["PAPER_AGENT_QA_DB"]).resolve()
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

from __future__ import annotations

import copy
import json
import unittest

from paper_agents.metadata_relevance import build_prompt, parse_judgment, run_experiment


class MetadataRelevanceTests(unittest.TestCase):
    def fixture(self):
        return {"profile": {"interests": ["AIOps"], "positive_signals": ["production"],
                            "negative_signals": ["toy studies"], "notes": "PRIVATE LABEL HISTORY"},
                "candidates": [{"id": "1", "title": "AIOps", "abstract": "Production operations",
                                "decision": "keep", "user_score": 4,
                                "review": "PRIVATE REVIEW", "discovery_query": "ai operations"}]}

    def envelope(self, score=80):
        return {"response": json.dumps({"status": "complete", "score": score,
            "rationale": "The metadata directly matches production AIOps interests.",
            "query_match": "strong", "negative_preference_applicability": "does_not_apply"})}

    def test_prompt_excludes_labels_reviews_scores_and_profile_notes(self):
        fixture = self.fixture()
        prompt = build_prompt(fixture["candidates"][0], fixture["profile"])
        self.assertNotIn("PRIVATE", prompt)
        self.assertNotIn('"decision"', prompt)
        self.assertNotIn('"user_score"', prompt)
        self.assertIn("ai operations", prompt)

    def test_run_attaches_labels_only_after_judgment_and_ranks_both_methods(self):
        fixture = self.fixture()
        seen = []
        report = run_experiment(fixture, provider=lambda u, m, p, t: seen.append(p) or self.envelope())
        self.assertEqual(report["calls"], 1)
        self.assertEqual(report["results"][0]["qwen"]["score"], 80)
        self.assertEqual(report["results"][0]["qwen_rank"], 1)
        self.assertEqual(report["results"][0]["keyword_rank"], 1)
        self.assertEqual(report["results"][0]["human_label"]["decision"], "keep")
        self.assertNotIn("keep", seen[0])

    def test_invalid_provider_output_is_unknown_and_does_not_abort(self):
        report = run_experiment(self.fixture(), provider=lambda *args: {"response": "{}"})
        self.assertEqual(report["results"][0]["qwen"]["status"], "unknown")
        self.assertIsNone(report["results"][0]["qwen_rank"])

    def test_boolean_and_out_of_range_scores_are_rejected(self):
        for score in (True, -1, 101):
            with self.subTest(score=score), self.assertRaises(ValueError):
                parse_judgment(self.envelope(score))

    def test_label_changes_do_not_change_prompt(self):
        fixture = self.fixture()
        before = build_prompt(fixture["candidates"][0], fixture["profile"])
        changed = copy.deepcopy(fixture)
        changed["candidates"][0].update(decision="reject", user_score=1, review="OTHER")
        self.assertEqual(before, build_prompt(changed["candidates"][0], changed["profile"]))


if __name__ == "__main__":
    unittest.main()

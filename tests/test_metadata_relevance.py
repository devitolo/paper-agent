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

    def test_targeted_feasibility_run_calls_only_requested_paper(self):
        fixture = self.fixture()
        other = copy.deepcopy(fixture["candidates"][0])
        other["id"] = "618"
        fixture["candidates"].append(other)
        prompts = []

        report = run_experiment(
            fixture, target_ids={"618"}, timeout=120,
            provider=lambda u, m, p, t: prompts.append((p, t)) or self.envelope(),
        )

        self.assertEqual([row["id"] for row in report["results"]], ["618"])
        self.assertEqual(report["calls"], 1)
        self.assertEqual(prompts[0][1], 120)

    def test_unknown_target_is_rejected_before_provider_call(self):
        with self.assertRaisesRegex(ValueError, "target paper id"):
            run_experiment(self.fixture(), target_ids={"missing"},
                           provider=lambda *args: self.fail("provider called"))

    def test_extended_timeout_requires_one_target_paper(self):
        with self.assertRaisesRegex(ValueError, "exactly one target paper"):
            run_experiment(self.fixture(), timeout=120,
                           provider=lambda *args: self.fail("provider called"))

        fixture = self.fixture()
        other = copy.deepcopy(fixture["candidates"][0])
        other["id"] = "618"
        fixture["candidates"].append(other)
        with self.assertRaisesRegex(ValueError, "exactly one target paper"):
            run_experiment(fixture, target_ids={"1", "618"}, timeout=120,
                           provider=lambda *args: self.fail("provider called"))


if __name__ == "__main__":
    unittest.main()

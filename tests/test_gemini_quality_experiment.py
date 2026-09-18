import json
import unittest

from paper_agents.gemini_quality_experiment import assess, build_prompt, normalize
from paper_agents.two_axis_ranking import QUALITY_DIMENSIONS


def response(score=3):
    return {"status": "complete", "dimensions": {
        name: {"status": "complete", "score": score, "rationale": "Supported by metadata."}
        for name in QUALITY_DIMENSIONS}, "overall_rationale": "Concrete contribution."}


class GeminiQualityExperimentTests(unittest.TestCase):
    corpus = {"candidates": [
        {"id": "28", "title": "TELLER", "abstract": "Method and evaluation.",
         "decision": "keep", "user_score": 4.5},
        {"id": "357", "title": "Future SRE", "abstract": "Broad review.",
         "decision": "reject", "user_score": 1},
    ]}

    def test_prompt_is_label_isolated_and_quality_only(self):
        prompt = build_prompt({**self.corpus["candidates"][0], "review": "PRIVATE"})
        self.assertNotIn("PRIVATE", prompt)
        self.assertNotIn("user_score", prompt)
        self.assertIn("Do not judge topical relevance", prompt)

    def test_anchor_assessment_attaches_labels_after_calls(self):
        prompts = []
        report = assess(self.corpus, paper_ids={"28", "357"},
                        provider=lambda p, m, t: prompts.append(p) or response())
        self.assertEqual(report["calls"], 2)
        self.assertTrue(all("decision" not in prompt for prompt in prompts))
        self.assertEqual(report["results"][0]["human_label"]["decision"], "keep")

    def test_quoted_boolean_and_out_of_range_scores_are_rejected(self):
        for invalid in ("3", True, -1, 5):
            with self.subTest(invalid=invalid):
                value = response()
                value["dimensions"]["evidence"]["score"] = invalid
                with self.assertRaises(ValueError):
                    normalize(value)

    def test_provider_failure_is_sanitized(self):
        report = assess(self.corpus, paper_ids={"28"},
                        provider=lambda *args: (_ for _ in ()).throw(RuntimeError("PRIVATE")))
        self.assertNotIn("PRIVATE", json.dumps(report))


if __name__ == "__main__":
    unittest.main()

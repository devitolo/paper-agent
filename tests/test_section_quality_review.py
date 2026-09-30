from __future__ import annotations

import unittest

from paper_agents.section_quality_review import build_review_bundle, public_bundle, validate_decision


class SectionQualityReviewTests(unittest.TestCase):
    def setUp(self):
        self.baseline = {"results": [{"paper_id": 1, "title": "Paper", "new_fields": {"research_problem": "current", "why_it_matters": "current why", "approach": "current approach"}, "original_signals": {"research_problem": "down", "why_it_matters": "up", "approach": "down"}}]}
        def candidate(prefix):
            fields = {}
            sources = {}
            for field in ("research_problem", "why_it_matters", "approach"):
                fields[field] = f"{prefix} {field}"
                fields[f"{field}_evidence"] = f"evidence {field}"
                sources[field] = "source_abstract"
            return {"paper_id": 1, "minilm_qwen": fields, "source_types": sources}
        self.abstract = {"results": [candidate("abstract")]}
        self.field = {"results": [candidate("field")]}

    def test_bundle_is_blinded_and_has_three_tasks(self):
        bundle = build_review_bundle(self.baseline, self.abstract, self.field)
        public = public_bundle(bundle)
        self.assertEqual(len(public["tasks"]), 3)
        self.assertNotIn("method_mapping", public)
        self.assertEqual({c["label"] for c in public["tasks"][0]["candidates"]}, {"A", "B"})
        self.assertTrue(all("method" not in c for c in public["tasks"][0]["candidates"]))

    def test_decision_requires_three_usefulness_labels(self):
        bundle = build_review_bundle(self.baseline, self.abstract, self.field)
        task_id = bundle["tasks"][0]["task_id"]
        ratings = {"current_useful": "yes", "A_useful": "partial", "B_useful": "no"}
        self.assertEqual(validate_decision(bundle, {"task_id": task_id, "ratings": ratings}), (task_id, ratings))
        with self.assertRaisesRegex(ValueError, "incomplete"):
            validate_decision(bundle, {"task_id": task_id, "ratings": {}})

    def test_old_support_labels_are_not_accepted(self):
        bundle = build_review_bundle(self.baseline, self.abstract, self.field)
        task_id = bundle["tasks"][0]["task_id"]
        ratings = {"current_useful": "yes", "A_useful": "partial", "A_supported": "yes", "B_useful": "no", "B_supported": "unclear"}
        with self.assertRaisesRegex(ValueError, "incomplete"):
            validate_decision(bundle, {"task_id": task_id, "ratings": ratings})


if __name__ == "__main__":
    unittest.main()

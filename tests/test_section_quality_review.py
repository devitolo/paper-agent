from __future__ import annotations

import unittest

from paper_agents.section_quality_review import (
    REVIEW_VERSION,
    build_review_bundle,
    public_bundle,
    render_page,
    validate_decision,
)


class SectionQualityReviewTests(unittest.TestCase):
    def setUp(self):
        self.baseline = {"results": [{"paper_id": 1, "title": "Paper", "new_fields": {
            "research_problem": "Current text explains a difficult production monitoring problem.",
            "why_it_matters": "Current text explains why the operational problem matters.",
            "approach": "Current text describes the method used by the authors.",
        }, "original_signals": {"research_problem": "down", "why_it_matters": "up", "approach": "down"}}]}
        def candidate(prefix):
            fields = {}
            sources = {}
            for field in ("research_problem", "why_it_matters", "approach"):
                fields[field] = f"{prefix} replacement clearly explains the paper section in plain language"
                fields[f"{field}_evidence"] = f"evidence {field}"
                sources[field] = "source_abstract"
            return {"paper_id": 1, "minilm_qwen": fields, "source_types": sources}
        self.abstract = {"results": [candidate("abstract")]}
        self.field = {"results": [candidate("field")]}

    def test_bundle_is_pairwise_blinded_and_does_not_include_prior_signal(self):
        bundle = build_review_bundle(self.baseline, self.abstract, self.field, task_limit=3)
        public = public_bundle(bundle)
        self.assertEqual(len(public["tasks"]), 3)
        self.assertEqual(bundle["review_version"], REVIEW_VERSION)
        self.assertNotIn("method_mapping", public)
        for task in public["tasks"]:
            self.assertEqual(set(task), {"task_id", "paper_id", "title", "field", "current", "replacement"})
            self.assertNotEqual(task["current"], task["replacement"])
            self.assertNotIn("prior_signal", task)

    def test_decision_is_one_pairwise_choice(self):
        bundle = build_review_bundle(self.baseline, self.abstract, self.field)
        task_id = bundle["tasks"][0]["task_id"]
        self.assertEqual(
            validate_decision(bundle, {"task_id": task_id, "decision": "replacement"}),
            (task_id, "replacement"),
        )
        with self.assertRaisesRegex(ValueError, "invalid decision"):
            validate_decision(bundle, {"task_id": task_id, "decision": "yes"})

    def test_old_three_way_ratings_are_not_accepted(self):
        bundle = build_review_bundle(self.baseline, self.abstract, self.field)
        task_id = bundle["tasks"][0]["task_id"]
        ratings = {"current_useful": "yes", "A_useful": "partial", "B_useful": "no"}
        with self.assertRaisesRegex(ValueError, "invalid decision"):
            validate_decision(bundle, {"task_id": task_id, "ratings": ratings})

    def test_near_identical_and_unreadable_comparisons_are_filtered(self):
        same = self.field["results"][0]["minilm_qwen"]
        for field in ("research_problem", "why_it_matters", "approach"):
            same[field] = self.baseline["results"][0]["new_fields"][field]
        bundle = build_review_bundle(self.baseline, self.abstract, self.field)
        self.assertTrue(all(task["replacement"] != task["current"] for task in bundle["tasks"]))

    def test_page_has_two_versions_and_one_choice(self):
        page = render_page()
        self.assertIn("Current production", page)
        self.assertIn("Proposed replacement", page)
        for label in ("Current better", "Replacement better", "Neither useful", "Too similar"):
            self.assertIn(f"'{label}'", page)
        for old_label in (
            "Current is better", "Replacement is better", "Neither is useful",
            "Too similar to judge",
        ):
            self.assertNotIn(old_label, page)
        self.assertNotIn("Candidate A", page)
        self.assertNotIn("Prior signal", page)

    def test_preferred_tasks_bound_the_review(self):
        bundle = build_review_bundle(
            self.baseline, self.abstract, self.field,
            task_limit=1, preferred_task_ids=("1:approach", "1:research_problem"),
        )
        self.assertEqual([task["task_id"] for task in bundle["tasks"]], ["1:approach"])


if __name__ == "__main__":
    unittest.main()

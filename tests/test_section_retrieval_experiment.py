from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from paper_agents.section_retrieval_experiment import (
    build_qwen_retrieval_prompt,
    parse_qwen_retrieval_output,
    prepare_paper_source,
    rank_passages,
    run_paper,
    split_source_passages,
)


class FakeScorer:
    def score_pair(self, query: str, text: str) -> float:
        terms = set(query.casefold().split())
        return float(sum(word in text.casefold() for word in terms))


class SectionRetrievalExperimentTests(unittest.TestCase):
    def test_abstract_mode_uses_abstract_even_when_baseline_hash_is_full_text(self):
        paper = {"paper_id": 4, "abstract": "A focused abstract about incident recovery."}
        prepared = prepare_paper_source(
            Path("/unused"),
            paper,
            {"source_sha256": "full-text-hash"},
            source_mode="abstract",
        )
        self.assertEqual(prepared["context_type"], "source_abstract")
        self.assertEqual(prepared["source_mode"], "abstract")
        self.assertEqual(
            prepared["source_sha256"],
            hashlib.sha256(paper["abstract"].encode()).hexdigest(),
        )

    def test_passages_overlap_and_preserve_source_words(self):
        source = (
            "Operators inspect logs manually during incidents. "
            "This delays recovery for critical services. "
            "The authors train a classifier over incident telemetry."
        )
        passages = split_source_passages(source, max_chars=110)
        self.assertGreaterEqual(len(passages), 2)
        self.assertIn("Operators inspect logs manually during incidents.", passages[0])
        self.assertTrue(all(passage in source or " ".join(passage.split()) in " ".join(source.split()) for passage in passages))

    def test_ranking_is_field_specific_and_bounded(self):
        passages = ["A concrete failure delays recovery.", "The authors train a classifier.", "Operators face outages."]
        ranked = rank_passages(passages, scorer=FakeScorer(), top_k=2)
        self.assertEqual(set(ranked), {"research_problem", "why_it_matters", "approach"})
        self.assertTrue(all(len(rows) == 2 for rows in ranked.values()))

    def test_qwen_evidence_is_copied_from_selected_passage(self):
        ranked = {
            field: [{"passage_id": "p1", "score": 1.0, "text": f"Exact {field} evidence."}]
            for field in ("research_problem", "why_it_matters", "approach")
        }
        response = {
            "research_problem": "Operators cannot isolate failures.",
            "research_problem_passage_id": "p1",
            "why_it_matters": "Failure isolation delays recovery.",
            "why_it_matters_passage_id": "p1",
            "approach": "The authors rank telemetry signals.",
            "approach_passage_id": "p1",
        }
        output, flags = parse_qwen_retrieval_output(json.dumps(response), ranked)
        self.assertEqual(output["approach_evidence"], "Exact approach evidence.")
        self.assertEqual(flags["approach"], [])

    def test_runner_produces_baseline_minilm_and_hybrid_versions(self):
        abstract = (
            "Operators inspect logs manually during incidents, delaying recovery. "
            "The authors train a classifier over incident telemetry to rank likely failures."
        )
        paper = {
            "paper_id": 1,
            "title": "Incident Analysis",
            "abstract": abstract,
            "artifact": {"metadata": {"abstract_only": True}},
            "frozen_sources": [],
        }
        baseline = {
            "source_sha256": hashlib.sha256(abstract.encode()).hexdigest(),
            "new_fields": {"research_problem": "old"},
            "original_signals": {"research_problem": "down"},
        }

        def generate(url, model, prompt, timeout):
            self.assertIn("RETRIEVED PASSAGES", prompt)
            ids = {}
            for field in ("research_problem", "why_it_matters", "approach"):
                ids[field] = "p1"
            return {"response": json.dumps({
                "research_problem": "Operators inspect incident logs manually.",
                "research_problem_passage_id": ids["research_problem"],
                "why_it_matters": "Manual inspection delays recovery.",
                "why_it_matters_passage_id": ids["why_it_matters"],
                "approach": "The authors train a classifier over telemetry.",
                "approach_passage_id": ids["approach"],
            })}

        with tempfile.TemporaryDirectory() as tempdir:
            result = run_paper(
                Path(tempdir), paper, baseline, scorer=FakeScorer(), model="test",
                ollama_url="unused", timeout=1, generate_fn=generate,
            )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["qwen_baseline"]["research_problem"], "old")
        self.assertEqual(result["minilm_qwen_flags"]["research_problem"], [])
        self.assertIn("evidence", result["minilm_only"]["approach"])


if __name__ == "__main__":
    unittest.main()

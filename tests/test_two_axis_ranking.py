import copy
import unittest

from paper_agents.two_axis_ranking import replay_two_axis


class TwoAxisRankingTests(unittest.TestCase):
    def fixture(self):
        corpus = {
            "profile": {"interests": ["observability"], "positive_signals": [], "negative_signals": []},
            "candidates": [
                {"id": "strong", "title": "Observability method", "abstract": "observability", "decision": "keep", "user_score": 4},
                {"id": "buzz", "title": "Observability future", "abstract": "observability", "decision": "reject", "user_score": 1},
            ],
        }
        labels = {
            "version": 1, "scale": {"minimum": 0, "maximum": 4}, "disclosure": "development",
            "papers": [
                {"id": "strong", "relevance": 4, "novelty": 4, "technical_depth": 4,
                 "evidence": 4, "baseline_quality": 4, "operational_realism": 4},
                {"id": "buzz", "relevance": 4, "novelty": 0, "technical_depth": 0,
                 "evidence": 0, "baseline_quality": 0, "operational_realism": 0},
            ],
        }
        return corpus, labels

    def test_quality_axis_breaks_equal_topic_score_tie(self):
        corpus, labels = self.fixture()
        report = replay_two_axis(corpus, labels, top_k=1)
        self.assertEqual(report["baseline"]["high_ranked_rejections"], ["buzz"])
        self.assertEqual(report["two_axis"]["order"], ["strong", "buzz"])
        self.assertEqual(report["two_axis"]["high_ranked_rejections"], [])

    def test_replay_does_not_mutate_inputs(self):
        corpus, labels = self.fixture()
        before = copy.deepcopy((corpus, labels))
        replay_two_axis(corpus, labels, top_k=2)
        self.assertEqual((corpus, labels), before)

    def test_requires_exact_label_coverage_and_bounded_integer_ratings(self):
        corpus, labels = self.fixture()
        labels["papers"].pop()
        with self.assertRaisesRegex(ValueError, "cover"):
            replay_two_axis(corpus, labels, top_k=2)
        corpus, labels = self.fixture()
        labels["papers"][0]["evidence"] = 4.0
        with self.assertRaisesRegex(ValueError, "evidence"):
            replay_two_axis(corpus, labels, top_k=2)


if __name__ == "__main__":
    unittest.main()

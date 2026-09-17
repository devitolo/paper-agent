"""Independent harness checks; injected responses only, never a model call."""
import copy
import json
import unittest
import runpy
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from paper_agents.metadata_relevance import allowed_profile, build_prompt, run_experiment, call_qwen


def fixture(count=1):
    return {"profile": {"interests": ["AIOps"], "positive_signals": [],
                        "negative_signals": [], "notes": "PRIVATE_HISTORY"},
            "candidates": [{"id": str(i), "title": "AIOps operations",
                            "abstract": "Production incident management.",
                            "decision": "keep", "user_score": 4}
                           for i in range(count)]}


def envelope():
    return {"response": json.dumps({"status": "complete", "score": 80,
        "rationale": "Matches operations interests.", "query_match": "unknown",
        "negative_preference_applicability": "unknown"}),
        "prompt_eval_count": 123, "eval_count": 34}


class MetadataRelevanceIndependentTests(unittest.TestCase):
    def test_single_target_rejects_ambiguous_duplicate_ids_before_provider(self):
        data = fixture(2)
        data["candidates"][1]["id"] = data["candidates"][0]["id"]
        with self.assertRaisesRegex(ValueError, "paper ids must be unique"):
            run_experiment(data, target_ids={"0"}, timeout=120,
                           provider=lambda *args: self.fail("provider called"))

    def test_reduced_prompt_caps_preserve_label_isolation(self):
        data = fixture()
        data["profile"] = {
            "interests": [f"interest-{index}-" + "x" * 400 for index in range(15)],
            "positive_signals": ["positive"] * 15,
            "negative_signals": ["negative"] * 15,
            "notes": "PRIVATE_HISTORY",
        }
        data["candidates"][0]["abstract"] = "a" * 5000 + "PRIVATE_TAIL"
        profile = allowed_profile(data["profile"])
        self.assertEqual(len(profile["interests"]), 12)
        self.assertTrue(all(len(value) <= 300 for values in profile.values() for value in values))
        prompt = build_prompt(data["candidates"][0], data["profile"])
        self.assertNotIn("PRIVATE_HISTORY", prompt)
        self.assertNotIn("PRIVATE_TAIL", prompt)

    def test_local_request_uses_reduced_output_budget_without_live_call(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self, limit):
                return json.dumps(envelope()).encode()
        with patch("paper_agents.metadata_relevance.urllib.request.build_opener") as factory:
            factory.return_value.open.return_value = Response()
            call_qwen("http://127.0.0.1:11434/api/generate", "fixture-model", "prompt", 1)
            request = factory.return_value.open.call_args.args[0]
            body = json.loads(request.data)
            self.assertEqual(body["options"]["num_predict"], 250)

    def test_provider_body_read_cannot_exceed_total_call_deadline(self):
        class SlowResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self, limit):
                time.sleep(0.08)
                return json.dumps(envelope()).encode()
        with patch("paper_agents.metadata_relevance.urllib.request.build_opener") as factory:
            factory.return_value.open.return_value = SlowResponse()
            with self.assertRaises(TimeoutError):
                call_qwen("http://127.0.0.1:11434/api/generate", "fixture-model", "fixture-prompt", 0.02)

    def test_fractional_remaining_deadline_is_not_rounded_up(self):
        time_now, allowed = [0.0], []
        def provider(url, model, prompt, timeout):
            allowed.append(timeout)
            time_now[0] += 0.8
            return envelope()
        with patch("paper_agents.metadata_relevance.time.monotonic", side_effect=lambda: time_now[0]):
            run_experiment(fixture(2), provider=provider, deadline_seconds=1)
        self.assertTrue(len(allowed) == 1 or allowed[1] <= 0.2 + 1e-9)

    def test_cli_checkpoint_does_not_overwrite_input_via_temporary_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "fixture.json", root / "report.json"
            source.write_text(json.dumps(fixture()))
            before = source.read_bytes()
            output.with_suffix(".json.tmp").symlink_to(source)
            def fake_run(data, **kwargs):
                result = {"status": "complete", "results": [], "calls": 0}
                kwargs["checkpoint"](result)
                return result
            script = Path(__file__).resolve().parents[1] / "scripts" / "compare_metadata_relevance.py"
            with patch.object(sys, "argv", [str(script), str(source), "--output", str(output)]), patch(
                "paper_agents.metadata_relevance.run_experiment", side_effect=fake_run
            ):
                try:
                    runpy.run_path(str(script), run_name="__main__")
                except (SystemExit, FileExistsError):
                    pass
            self.assertEqual(source.read_bytes(), before,
                             "checkpoint scratch aliases must not overwrite original corpus")

    def test_cli_forwards_single_paper_feasibility_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "fixture.json", root / "report.json"
            source.write_text(json.dumps(fixture()))
            captured = {}
            def fake_run(data, **kwargs):
                captured.update(kwargs)
                return {"status": "complete", "results": [], "calls": 0}
            script = Path(__file__).resolve().parents[1] / "scripts" / "compare_metadata_relevance.py"
            argv = [str(script), str(source), "--output", str(output),
                    "--paper-id", "0", "--timeout", "120"]
            with patch.object(sys, "argv", argv), patch(
                "paper_agents.metadata_relevance.run_experiment", side_effect=fake_run
            ):
                runpy.run_path(str(script), run_name="__main__")
            self.assertEqual(captured["target_ids"], {"0"})
            self.assertEqual(captured["timeout"], 120)

    def test_fifteen_paper_end_to_end_preserves_inputs_and_isolates_labels(self):
        data = fixture(15)
        before = copy.deepcopy(data)
        prompts, checkpoints = [], []
        def provider(url, model, prompt, timeout):
            prompts.append(prompt)
            self.assertLessEqual(timeout, 45)
            return envelope()
        report = run_experiment(data, provider=provider, checkpoint=checkpoints.append)
        self.assertEqual(data, before)
        self.assertEqual(report["calls"], 15)
        self.assertEqual(len(report["results"]), 15)
        self.assertEqual(len(checkpoints), 15)
        self.assertEqual(sorted(row["qwen_rank"] for row in report["results"]), list(range(1, 16)))
        for prompt in prompts:
            for forbidden in ("PRIVATE_HISTORY", '"decision"', '"user_score"'):
                self.assertNotIn(forbidden, prompt)

    def test_missing_abstract_stays_unknown_even_if_provider_returns_score(self):
        data = fixture()
        data["candidates"][0]["abstract"] = ""
        report = run_experiment(data, provider=lambda *args: envelope())
        self.assertEqual(report["results"][0]["qwen"]["status"], "unknown")
        self.assertIsNone(report["results"][0]["qwen"]["score"])

    def test_provider_failure_is_sanitized_and_not_retried(self):
        calls = []
        def provider(*args):
            calls.append(args)
            raise RuntimeError("PRIVATE_SECRET /private/paper token=secret")
        report = run_experiment(fixture(), provider=provider)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(report["results"][0]["qwen_rank"])
        self.assertNotIn("PRIVATE_SECRET", json.dumps(report))

    def test_call_timeout_is_clamped_to_remaining_overall_deadline(self):
        time_now = [0.0]
        allowed = []
        def provider(url, model, prompt, timeout):
            allowed.append(timeout)
            time_now[0] += 40
            return envelope()
        with patch("paper_agents.metadata_relevance.time.monotonic", side_effect=lambda: time_now[0]):
            run_experiment(fixture(2), provider=provider, deadline_seconds=50)
        self.assertEqual(len(allowed), 2)
        self.assertLessEqual(allowed[1], 10)

    def test_usage_returned_by_provider_is_preserved_in_report(self):
        report = run_experiment(fixture(), provider=lambda *args: envelope())
        serialized = json.dumps(report)
        self.assertIn('"prompt_eval_count": 123', serialized)
        self.assertIn('"eval_count": 34', serialized)

    def test_invalid_scores_and_unknowns_never_receive_a_rank(self):
        for score in (True, -1, 101, float("nan"), float("inf"), "80", None):
            with self.subTest(score=score):
                result = envelope()
                value = json.loads(result["response"])
                value["score"] = score
                result["response"] = json.dumps(value)
                report = run_experiment(fixture(), provider=lambda *args: result)
                self.assertIsNone(report["results"][0]["qwen_rank"])

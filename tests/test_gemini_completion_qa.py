"""Independent failure acceptance tests; no live Gemini or provider calls."""
import os
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from paper_agents import db
from paper_agents.feedback import apply_feedback_to_profile, ingest_feedback_blob
from paper_agents.gemini_process import _CompletionStream, run_gemini


FIXTURE = r'''
import json, sys, time
mode = sys.argv[1]
answer = json.dumps({"profile": {"interests": ["PRIVATE_PROFILE"]},
                     "change_summary": "PRIVATE_REVIEW"})
if mode.startswith("framed_"):
    def emit(event):
        print(json.dumps(event), flush=True)
    emit({"type": "init", "session_id": "qa", "model": "fixture"})
    emit({"type": "message", "role": "assistant", "delta": True, "content": answer})
    if mode == "framed_missing_result":
        time.sleep(60)
    emit({"type": "result", "status": "success"})
    if mode == "framed_error_after":
        time.sleep(.1)
        emit({"type": "error", "severity": "error", "message": "PRIVATE_REVIEW"})
    if mode == "framed_nonzero":
        time.sleep(.1)
        sys.exit(2)
    if mode == "framed_complete_hang":
        time.sleep(60)
    sys.exit(0)
if mode == "truncated":
    print(answer[:-5], flush=True)
elif mode == "malformed":
    print("PRIVATE_PROFILE {broken JSON PRIVATE_REVIEW}", flush=True)
else:
    print(answer, flush=True)
    print("provider failed PRIVATE_REVIEW", file=sys.stderr, flush=True)
    if mode == "nonzero":
        sys.exit(2)
    time.sleep(60)
'''


@unittest.skipUnless(os.name == "posix", "POSIX Gemini lifecycle acceptance")
class GeminiCompletionQATests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.script = root / "provider_fixture.py"
        self.script.write_text(FIXTURE)
        path = root / "state.db"
        db.init_db(path)
        self.connection = db.connect_db(path)
        self.addCleanup(self.connection.close)
        self.original = db.create_profile_version(self.connection, {"interests": ["original"]})
        paper_id, _ = db.upsert_paper(self.connection, {
            "source": "test", "source_id": "completion-qa", "title": "QA fixture",
        })
        for score in (4, 5):
            ingest_feedback_blob(self.connection, paper_id=paper_id, recommendation_id=None,
                                 content=f"Decision: keep\nScore: {score}\nUseful implementation.",
                                 source="test")
        self.pending = db.unapplied_structured_feedback(self.connection)
        self.assertEqual(len(self.pending), 2)
        self.connection.commit()

    def assert_failed_without_applying(self, mode):
        command = [sys.executable, str(self.script), mode]
        started = time.monotonic()
        with patch("paper_agents.feedback.run_gemini",
                   side_effect=lambda _command, _timeout, **kwargs: run_gemini(
                       command, 1, **(kwargs if mode.startswith("framed_") else {}))):
            result = apply_feedback_to_profile(self.connection, model="explicit-fixture", dry_run=False)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(db.current_profile_version(self.connection)["id"], self.original)
        self.assertEqual(db.unapplied_structured_feedback(self.connection), self.pending)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM profile_versions").fetchone()[0], 1)
        attempts = self.connection.execute("SELECT status, error FROM feedback_profile_apply_attempts").fetchall()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0][0], "failed")
        self.assertLess(len(attempts[0][1]), 500)
        self.assertNotIn("PRIVATE", attempts[0][1])
        self.assertNotIn("PRIVATE", str(result))
        return result

    def test_nonzero_exit_with_valid_profile_json_does_not_apply_feedback(self):
        self.assert_failed_without_applying("nonzero")

    def test_truncated_profile_json_does_not_apply_feedback(self):
        self.assert_failed_without_applying("truncated")

    def test_malformed_output_does_not_apply_feedback_or_leak_private_text(self):
        self.assert_failed_without_applying("malformed")

    def test_valid_profile_json_before_timeout_does_not_apply_feedback(self):
        result = self.assert_failed_without_applying("hang")
        self.assertIn("timed out", result["error"])

    def test_framed_answer_without_terminal_result_does_not_apply_feedback(self):
        result = self.assert_failed_without_applying("framed_missing_result")
        self.assertIn("timed out", result["error"])

    def test_terminal_success_followed_by_error_does_not_apply_feedback(self):
        self.assert_failed_without_applying("framed_error_after")

    def test_terminal_success_followed_by_nonzero_exit_does_not_apply_feedback(self):
        self.assert_failed_without_applying("framed_nonzero")

    def test_terminal_success_then_teardown_hang_applies_both_pending_rows_once(self):
        command = [sys.executable, str(self.script), "framed_complete_hang"]
        with patch("paper_agents.feedback.run_gemini",
                   side_effect=lambda _command, _timeout, **kwargs: run_gemini(command, 2, **kwargs)):
            result = apply_feedback_to_profile(self.connection, model="explicit-fixture", dry_run=False)
        self.assertEqual(result["status"], "applied")
        self.assertNotEqual(db.current_profile_version(self.connection)["id"], self.original)
        self.assertEqual(db.unapplied_structured_feedback(self.connection), [])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM profile_versions").fetchone()[0], 2)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM feedback_profile_applications").fetchone()[0], 2)
        self.assertEqual(self.connection.execute("SELECT status FROM feedback_profile_apply_attempts").fetchall(), [("succeeded",)])

    def test_only_error_payload_not_stats_triggers_quota_classification(self):
        cases = [
            ({"type": "result", "status": "error", "error": {"message": "failed"},
              "stats": {"total_tokens": 429}}, False),
            ({"type": "result", "status": "error", "error": {"message": "429 PRIVATE_REVIEW"}}, True),
            ({"type": "error", "severity": "error", "message": "PRIVATE_REVIEW failed",
              "unused": "429 quota"}, False),
        ]
        for event, quota in cases:
            with self.subTest(event=event), self.assertRaises(RuntimeError) as caught:
                _CompletionStream().feed(json.dumps(event).encode())
            self.assertEqual("provider_category=quota" in str(caught.exception), quota)
            self.assertNotIn("PRIVATE", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

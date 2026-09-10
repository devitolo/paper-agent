from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_agents import db, manual_scout as manual, pipeline, web
from paper_agents.topics import TopicEntry, save_topic_config


class ManualScoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "paper_agent.db"
        self.config = self.root / "topics.yaml"
        db.init_db(self.path)
        save_topic_config([], self.config)
        self.packaged = patch.dict(os.environ, {"PAPER_AGENT_PACKAGED": "1"})
        self.packaged.start()
        self.addCleanup(self.packaged.stop)

    def topics(self):
        save_topic_config([
            TopicEntry("a", "A", "explicit arxiv research", ["arxiv"], cadence="manual"),
            TopicEntry("b", "B", "not selected", ["openalex"]),
            TopicEntry("c", "C", "disabled", ["arxiv"], enabled=False),
        ], self.config)

    def test_no_topic_disables_action_and_rejects_start(self):
        panel = web.render_manual_scout_panel(self.path, self.config)
        self.assertIn('type="submit" disabled', panel)
        self.assertIn("enabled arXiv topic", panel)
        with patch.object(manual.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(ValueError, "enabled arXiv"):
                manual.start(self.path, self.config)
            spawn.assert_not_called()

    def test_unavailable_inference_disables_action_without_hiding_saved_papers(self):
        self.topics()
        unavailable = {"status": "unavailable", "message": "Local Qwen inference is unavailable."}
        with patch.object(manual, "inference_readiness", return_value=unavailable):
            snapshot = web.manual_scout_snapshot(self.path, self.config)
            panel = web.render_manual_scout_panel(self.path, self.config)

        self.assertFalse(snapshot["can_run"])
        self.assertEqual(snapshot["readiness"], unavailable)
        self.assertIn('type="submit" disabled', panel)
        self.assertIn("Local Qwen: Unavailable", panel)
        self.assertIn('href="/">Refresh papers</a>', panel)

    def test_recent_inference_readiness_enables_action(self):
        self.topics()
        ready = {"status": "ready", "message": "Local Qwen inference is ready."}
        with patch.object(manual, "inference_readiness", return_value=ready):
            snapshot = web.manual_scout_snapshot(self.path, self.config)

        self.assertTrue(snapshot["can_run"])
        self.assertEqual(snapshot["readiness"], ready)

    def test_recent_readiness_cache_does_not_probe_on_status_refresh(self):
        manual._write_readiness(self.path, {"status": "ready", "message": "Local Qwen inference is ready."})
        with patch("paper_agents.package_runtime.check_model") as check:
            readiness = manual.inference_readiness(self.path)

        self.assertEqual(readiness["status"], "ready")
        check.assert_not_called()
        now = 1_000
        for age, expected in ((0, "ready"), (manual.READINESS_TTL - 1, "ready"),
                              (manual.READINESS_TTL, "preparing"), (-1, "preparing")):
            with self.subTest(age=age):
                (self.root / "model-readiness.json").write_text(json.dumps({
                    "status": "ready", "message": "Local Qwen inference is ready.", "checked_at": now - age,
                }), encoding="utf-8")
                with patch.object(manual.time, "time", return_value=now):
                    self.assertEqual(manual.inference_readiness(self.path, schedule=False)["status"], expected)

    def test_native_mode_does_not_add_panel_or_pipeline_lock(self):
        called = []
        @manual.exclusive_pipeline
        def fake_pipeline(**kwargs):
            called.append(kwargs)
            return {"curator": {"recommendations": []}}
        with patch.dict(os.environ, {"PAPER_AGENT_PACKAGED": "0"}):
            self.assertEqual(web.render_manual_scout_panel(self.path, self.config), "")
            with patch.object(manual, "acquire") as acquire:
                fake_pipeline(db_path=self.path)
                acquire.assert_not_called()
        self.assertEqual(len(called), 1)

    def test_duplicate_clicks_and_cli_share_lock(self):
        self.topics()
        finish = threading.Event()
        class Process:
            def wait(self, timeout=None):
                finish.wait(5)
                return 0
        with patch.object(manual, "inference_readiness", return_value={"status": "ready", "message": "ready"}), \
             patch.object(manual.subprocess, "Popen", return_value=Process()) as spawn:
            queued = manual.start(self.path, self.config)
            try:
                self.assertEqual(queued["status"], "queued")
                self.assertTrue(manual.status(self.path)["busy"])
                with self.assertRaises(manual.ScoutBusy):
                    manual.start(self.path, self.config)
                with self.assertRaises(manual.ScoutBusy):
                    pipeline.run_daily_pipeline(db_path=self.path, topics=[])
                spawn.assert_called_once()
                self.assertEqual(queued["topics"], ["explicit arxiv research"])
            finally:
                finish.set()
                for _ in range(100):
                    if not manual.status(self.path)["busy"]:
                        break
                    time.sleep(.01)
        self.assertEqual(manual.status(self.path)["status"], "interrupted")

    def test_separate_process_cannot_acquire_same_data_lock(self):
        handle = manual.acquire(self.path)
        try:
            result = subprocess.run([sys.executable, "-c",
                "from pathlib import Path; from paper_agents.manual_scout import acquire; import sys; acquire(Path(sys.argv[1]))",
                str(self.path)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("already running", result.stderr)
        finally:
            handle.close()

    def run_worker(self, result=None, error=None):
        handle = manual.acquire(self.path)
        manual.write_status(self.path, {"status": "queued", "run_id": "saved-id", "topics": ["explicit arxiv research"],
                                        "message": "Queued for test."})
        @manual.exclusive_pipeline
        def fake_pipeline(**kwargs):
            self.worker_args = kwargs
            if error:
                raise error
            return result
        try:
            with patch("paper_agents.package_runtime.check_model", return_value={"inference": "ready"}) as readiness, \
                 patch.object(pipeline, "run_daily_pipeline", fake_pipeline), \
                 patch.dict(os.environ, {"PAPER_AGENT_OLLAMA_URL": "http://ollama:11434"}):
                code = manual.worker(self.path, handle.fileno())
                readiness.assert_called_once_with(timeout=120)
                return code
        finally:
            handle.close()

    def test_worker_full_pipeline_explicit_arxiv_and_persisted_success(self):
        code = self.run_worker({"workflow_cycle_id": 7, "curator": {"recommendations": [{"id": 1}]}})
        self.assertEqual(code, 0)
        self.assertEqual(self.worker_args["topics"], ["explicit arxiv research"])
        self.assertEqual(self.worker_args["source_name"], "arxiv")
        self.assertEqual(self.worker_args["ollama_url"], "http://ollama:11434/api/generate")
        self.assertEqual(self.worker_args["model"], "qwen2.5:1.5b-instruct")
        self.assertEqual(self.worker_args["max_scout_attempts"], 1)
        self.assertEqual(self.worker_args["workers"], 1)
        saved = json.loads((self.root / "scout-status.json").read_text())
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["workflow_cycle_id"], 7)
        self.assertEqual(manual.status(self.path)["run_id"], "saved-id")
        self.assertTrue(web.manual_scout_snapshot(self.path, self.config)["status"] == "completed")

    def test_empty_source_failure_and_exception_are_distinct(self):
        self.run_worker({"curator": {"recommendations": []}, "scout_results": []})
        self.assertEqual(manual.status(self.path)["status"], "empty")
        self.run_worker({"cycle": {"state": "failed"}, "scout_results": [{"errors": ["timeout"]}]})
        self.assertEqual(manual.status(self.path)["status"], "failed")
        with patch("traceback.print_exc"):
            self.assertEqual(self.run_worker(error=RuntimeError("provider failed")), 1)
        self.assertEqual(manual.status(self.path)["status"], "failed")

    def test_model_failure_does_not_start_pipeline(self):
        handle = manual.acquire(self.path)
        manual.write_status(self.path, {"status": "queued", "run_id": "model-failure", "topics": ["research"],
                                        "message": "Queued for test."})
        try:
            with patch("paper_agents.package_runtime.check_model", side_effect=RuntimeError("offline")), \
                 patch.object(pipeline, "run_daily_pipeline") as run, patch("traceback.print_exc"):
                self.assertEqual(manual.worker(self.path, handle.fileno()), 1)
                run.assert_not_called()
        finally:
            handle.close()
        self.assertEqual(manual.status(self.path)["status"], "failed")
        self.assertIn("Existing papers are safe", manual.status(self.path)["message"])

    def test_restart_marks_abandoned_work_interrupted_without_relaunch(self):
        manual.write_status(self.path, {"status": "running", "run_id": "old", "topics": ["research"], "message": "running"})
        with patch.object(manual.subprocess, "Popen") as spawn:
            state = manual.status(self.path)
            spawn.assert_not_called()
        self.assertEqual(state["status"], "interrupted")
        self.assertEqual(state["run_id"], "old")
        self.assertEqual(json.loads((self.root / "scout-status.json").read_text())["status"], "interrupted")

    def test_module_worker_reuses_inherited_lock_in_real_pipeline(self):
        handle = manual.acquire(self.path)
        manual.write_status(self.path, {"status": "queued", "run_id": "worker", "topics": [], "message": "queued"})
        code = """
import sys
import paper_agents.package_runtime as runtime
runtime.check_model = lambda timeout: {'inference': 'ready'}
from paper_agents.manual_scout import worker_main
raise SystemExit(worker_main(__import__('pathlib').Path(sys.argv[1]), int(sys.argv[2])))
"""
        try:
            result = subprocess.run([sys.executable, "-c", code, str(self.path), str(handle.fileno())],
                pass_fds=(handle.fileno(),), capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            handle.close()
        self.assertEqual(manual.status(self.path)["status"], "empty")

    def test_supervisor_deadline_stops_worker_and_releases_lock(self):
        handle = manual.acquire(self.path)
        log = (self.root / "test.log").open("w")
        state = {"status": "running", "run_id": "deadline", "topics": ["research"], "message": "running"}
        manual.write_status(self.path, state)
        from unittest.mock import Mock
        process = Mock(pid=12345)
        process.wait.side_effect = [subprocess.TimeoutExpired("worker", 1800), -9]
        with patch.object(manual.os, "killpg") as kill:
            manual._supervise(process, handle, self.path, state, log)
            kill.assert_called_once()
        self.assertEqual(manual.status(self.path)["status"], "failed")
        self.assertIn("30-minute", manual.status(self.path)["message"])
        self.assertFalse(manual.status(self.path)["busy"])

    def test_supervisor_classifies_unfinalized_worker_exit(self):
        from unittest.mock import Mock
        for code, expected in ((0, "interrupted"), (1, "failed")):
            handle = manual.acquire(self.path)
            log = (self.root / f"test-{code}.log").open("w")
            state = {"status": "running", "run_id": f"worker-{code}", "topics": ["research"], "message": "running"}
            manual.write_status(self.path, state)
            process = Mock()
            process.wait.return_value = code
            manual._supervise(process, handle, self.path, state, log)
            self.assertEqual(manual.status(self.path)["status"], expected)

    def test_malformed_valid_status_json_degrades_to_visible_failed_state(self):
        invalid_statuses = [None, [], {}, 0, True, {"status": "completed"},
                            {"status": [], "message": "bad"}, {"status": {}, "message": "bad"},
                            {"status": 1, "message": "bad"}, {"status": False, "message": "bad"}]
        for value in invalid_statuses:
            with self.subTest(status=value):
                (self.root / "scout-status.json").write_text(json.dumps(value), encoding="utf-8")
                state = manual.status(self.path)
                self.assertEqual(state["status"], "failed")
                self.assertIn("incomplete or invalid", state["message"])

        invalid_readiness = [None, [], {}, 0, True, {"status": "ready"},
                             {"status": [], "message": "bad", "checked_at": 1},
                             {"status": {}, "message": "bad", "checked_at": 1},
                             {"status": 1, "message": "bad", "checked_at": 1},
                             {"status": False, "message": "bad", "checked_at": 1}]
        for value in invalid_readiness:
            with self.subTest(readiness=value):
                (self.root / "model-readiness.json").write_text(json.dumps(value), encoding="utf-8")
                self.assertIsNone(manual._read_readiness(self.path))
                self.assertEqual(manual.inference_readiness(self.path, schedule=False)["status"], "preparing")

        (self.root / "scout-status.json").write_text('{"status": "completed"}', encoding="utf-8")

        state = manual.status(self.path)
        snapshot = web.manual_scout_snapshot(self.path, self.config)
        panel = web.render_manual_scout_panel(self.path, self.config)

        self.assertEqual(state["status"], "failed")
        self.assertIn("incomplete or invalid", state["message"])
        self.assertEqual(snapshot["status"], "failed")
        self.assertIn("Failed: Scout status is incomplete or invalid", panel)


if __name__ == "__main__":
    unittest.main()

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from paper_agents.db import init_db
from paper_agents import web
from paper_agents.sync_exchange import MAX_BODY_BYTES, signature


class ServingHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "data/paper_agent.db"
        self.db.parent.mkdir()
        init_db(self.db)
        (self.root / "config").mkdir()
        (self.root / "config/topics.yaml").write_text("topics: []")
        (self.root / "config/industry.json").write_text("{}")
        (self.root / "data/profile.json").write_text('{"interests":[],"positive_signals":[],"negative_signals":[],"feedback_history":[]}')
        self.env = patch.dict(os.environ, {"PAPER_AGENT_SERVING_ONLY": "1", "PAPER_AGENT_SYNC_SHARED_SECRET": "s" * 32})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(self.db))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, path, method="GET", body=None, signed=False, extra=None):
        data = body or b""
        headers = extra or {}
        if signed:
            ts = str(time.time())
            headers.update({"X-Paper-Sync-Timestamp": ts, "X-Paper-Sync-Signature": signature(method, path, ts, data, headers.get("X-Paper-Sync-Idempotency", ""))})
        return urllib.request.urlopen(urllib.request.Request(self.url + path, data=body, method=method, headers=headers), timeout=3)

    def test_input_auth_uses_request_body_and_rejects_before_snapshot(self):
        with patch.object(web, "build_input_snapshot", return_value={"schema": 1, "revision": "r"}) as build:
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.request("/sync/input")
            self.assertEqual(failure.exception.code, 401)
            build.assert_not_called()
            with self.request("/sync/input", signed=True) as response:
                self.assertEqual(json.load(response)["revision"], "r")
            build.assert_called_once()

    def test_signed_deploy_probes_never_export_or_apply(self):
        before = self.db.read_bytes()
        with patch.object(web, "build_input_snapshot", side_effect=AssertionError("snapshot mutation")), patch.object(web, "receive_result_batch", side_effect=AssertionError("result mutation")):
            for path, method, body in (("/sync/input", "GET", None), ("/sync/pipeline-results", "POST", b"{}")):
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    self.request(path, method, body, extra={"X-Paper-Sync-Probe":"1"})
                self.assertEqual(failure.exception.code, 401)
                failure.exception.close()
                with self.request(path, method, body, signed=True, extra={"X-Paper-Sync-Probe":"1"}) as response:
                    self.assertEqual(json.load(response), {"schema":1,"status":"ready","route":path,"read_only":True})
        self.assertEqual(before, self.db.read_bytes())

    def test_installed_controller_probe_runs_against_actual_http_handler(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('probe_controller',Path(__file__).resolve().parents[2]/'scripts/aws_deploy.py')
        controller=importlib.util.module_from_spec(spec);spec.loader.exec_module(controller)
        before=self.db.read_bytes()
        with patch.object(web,"build_input_snapshot",side_effect=AssertionError("snapshot mutation")), patch.object(web,"receive_result_batch",side_effect=AssertionError("result mutation")):
            code=controller.SYNC_HEALTH_PROBE.replace('http://127.0.0.1:8000',self.url)
            exec(compile(code,'<container-health-probe>','exec'),{})
        self.assertEqual(before,self.db.read_bytes())

    def test_pipeline_results_route_requires_a_valid_batch(self):
        before=self.db.read_bytes()
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.request("/sync/pipeline-results","POST",b"{}",signed=True)
        self.assertEqual(failure.exception.code,422)
        failure.exception.close()
        self.assertEqual(before,self.db.read_bytes())

    def test_pipeline_probe_rejects_nonempty_payload(self):
        with patch.object(web, "receive_result_batch", side_effect=AssertionError("result mutation")):
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.request("/sync/pipeline-results", "POST", b'{"results":[]}', signed=True, extra={"X-Paper-Sync-Probe":"1"})
            self.assertEqual(failure.exception.code, 400)
            failure.exception.close()

    def test_model_routes_disabled_and_ready_is_model_independent(self):
        with patch.object(web, "manual_scout_snapshot", side_effect=AssertionError("model invoked")):
            with self.request("/ready") as response:
                self.assertTrue(json.load(response)["ready"])
            for path, method in (("/scout/status", "GET"), ("/scout/run", "POST")):
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    self.request(path, method, b"" if method == "POST" else None)
                self.assertEqual(failure.exception.code, 404)

    def test_oversized_and_malformed_lengths_rejected_before_body_read(self):
        for length, expected in ((str(MAX_BODY_BYTES + 1), 413), ("-1", 400), ("nope", 400)):
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.request("/sync/results", "POST", b"", extra={"Content-Length": length})
            self.assertEqual(failure.exception.code, expected)

    def test_profile_route_is_authenticated_and_reachable(self):
        with patch("paper_agents.sync_exchange.receive_profile_proposal", return_value={"status": "accepted"}) as receive:
            with self.request("/sync/profile-proposals", "POST", b"{}", signed=True, extra={"X-Paper-Sync-Idempotency": "p"}) as response:
                self.assertEqual(json.load(response)["status"], "accepted")
            receive.assert_called_once()

    def test_topics_direct_edit_allowed_but_agent_disabled(self):
        with patch.object(web, "save_topics_form") as save:
            with self.request("/topics", "POST", b"action=save") as response:
                self.assertEqual(response.status, 200)
            save.assert_called_once()
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.request("/topics", "POST", b"action=propose")
        self.assertEqual(failure.exception.code, 503)

    def test_backup_stream_download_and_restore(self):
        from paper_agents import sync_backup
        original = sync_backup.create_backup
        def create(db_path, config_dir, destination, root=None):
            return original(self.db, self.root / "config", destination, root=self.root)
        with patch.object(sync_backup, "create_backup", side_effect=create):
            downloaded = sync_backup.download_backup(self.url, self.root / "received", allow_http_loopback=True)
        self.assertTrue(sync_backup.validate_backup(downloaded))
        restored = sync_backup.restore_backup(downloaded, self.root / "restored")
        self.assertTrue(sync_backup.validate_backup(restored))
        self.assertEqual((restored / "config/topics.yaml").read_text(), "topics: []")


if __name__ == "__main__":
    unittest.main()

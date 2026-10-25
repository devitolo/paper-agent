import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import host_resource_collector as collector


class HostResourceCollectorTests(unittest.TestCase):
    def test_summary_preserves_counts_and_window_statistics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = []
            for index, value in enumerate((10, 20, 30, 40, 50, 60)):
                rows.append(json.dumps({"schema": collector.SCHEMA, "observed_at": f"2026-10-24T00:{index:02d}:00+00:00", "cpu": {"utilization_percent": value}, "memory": {"available_bytes": 1000 - value}}))
            (root / "samples.jsonl").write_text("\n".join(rows) + "\n")
            result = collector.summarize(root)
        self.assertEqual(result["sample_count"], 6)
        self.assertEqual(result["coverage_seconds"], 300)
        self.assertEqual(result["cpu"]["average_percent"], 35)
        self.assertEqual(result["cpu"]["peak_sustained_5m_percent"], 40)
        self.assertEqual(result["cpu"]["max_one_minute_percent"], 60)
        self.assertEqual(result["memory"]["available_min_bytes"], 940)

    def test_missing_cpu_and_memory_are_not_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "samples.jsonl").write_text(json.dumps({"observed_at": "2026-10-24T00:00:00+00:00", "cpu": {}, "memory": {}}) + "\n")
            result = collector.summarize(root)
        self.assertIsNone(result["cpu"]["average_percent"])
        self.assertIsNone(result["memory"]["available_average_bytes"])

    def test_sample_reports_swap_used_and_explicit_nonprod_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(collector, "read_meminfo", return_value={"MemAvailable": 10, "MemTotal": 20, "SwapTotal": 30, "SwapFree": 7}), patch.object(collector, "read_cpu", return_value=(100, 40)), patch.object(collector, "containers", return_value={"state": "unconfigured"}), patch.object(collector, "temperature", return_value={"state": "unavailable"}):
                result = collector.sample(root)
        self.assertEqual(result["environment"], "mini-nonprod")
        self.assertEqual(result["scope"], "host")
        self.assertEqual(result["memory"]["swap_used_bytes"], 23)

    def test_container_identity_is_sanitized(self):
        payload = {"State": {"Status": "running", "Health": {"Status": "healthy"}, "RestartCount": 0, "OOMKilled": False}, "Config": {"Image": "ghcr.io/example@sha256:" + "a" * 64, "Labels": {"org.opencontainers.image.revision": "b" * 40}}}
        with patch.dict(collector.os.environ, {"PROJECT_PAPER_CONTAINER_NAMES": "paper-mini-nonprod-app-1"}), patch.object(collector.subprocess, "check_output", return_value=json.dumps(payload)):
            result = collector.containers()
        self.assertEqual(result["items"][0]["image"], payload["Config"]["Image"])
        self.assertEqual(result["items"][0]["revision"], "b" * 40)

    def test_summary_marks_missing_intervals_and_uses_aligned_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = []
            for minute in (0, 1, 3, 4):
                rows.append(json.dumps({"schema": collector.SCHEMA, "observed_at": f"2026-10-24T12:{minute:02d}:00+00:00", "cpu": {"utilization_percent": 10}, "memory": {"available_bytes": 100}}))
            (root / "samples.jsonl").write_text("\n".join(rows) + "\n")
            result = collector.summarize(root, reference=collector.datetime(2026, 10, 24, 12, 15, tzinfo=collector.timezone.utc))
        self.assertEqual(result["window"]["start_at"], "2026-10-24T12:00:00+00:00")
        self.assertEqual(result["window"]["end_at"], "2026-10-24T12:15:00+00:00")
        self.assertEqual(result["window"]["sample_gaps"], 1)
        self.assertFalse(result["window"]["complete"])

    def test_collect_publishes_atomic_summary_without_database_access(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(collector, "now", return_value=collector.datetime(2026, 10, 24, 12, 15, tzinfo=collector.timezone.utc)):
            root = Path(directory)
            collector.collect(root)
            status = json.loads((root / "status.json").read_text())
            self.assertEqual(status["schema"], collector.SCHEMA)
            self.assertEqual(status["sample_count"], 1)
            self.assertTrue((root / "summaries.jsonl").exists())


if __name__ == "__main__":
    unittest.main()

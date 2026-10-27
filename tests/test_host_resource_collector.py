import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import host_resource_collector as collector


class HostResourceCollectorTests(unittest.TestCase):
    def test_disk_sampled_at_all_quarter_hours_and_delayed_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            for minute in (0, 15, 30, 45, 46):
                stamp = collector.datetime(2026, 10, 26, 12, minute, tzinfo=collector.timezone.utc)
                with patch.object(collector, "now", return_value=stamp), patch.object(collector, "read_cpu", return_value=None), patch.object(collector, "filesystem", return_value={"used_percent": 55}) as disk:
                    result = collector.sample(Path(directory))
                disk.assert_called_once()
                self.assertEqual(result["disk"]["used_percent"], 55)
                self.assertEqual(result["observed_at"], stamp.isoformat())

    def test_disk_value_and_timestamp_stay_paired_across_window_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = "2026-10-26T12:00:00+00:00"
            new = "2026-10-26T12:15:06+00:00"
            (root / "samples.jsonl").write_text(json.dumps({"observed_at": old, "disk": {"used_percent": 90}}) + "\n")
            (root / "last-observations.json").write_text(json.dumps({"disk": {"observed_at": new, "value": {"used_percent": 55}}}))
            result = collector.summarize(root, reference=collector.datetime.fromisoformat(new))
            self.assertEqual(result["latest"]["disk"]["used_percent"], 55)
            self.assertEqual(result["latest"]["observation_times"]["disk"], new)
            (root / "samples.jsonl").unlink()
            result = collector.summarize(root, reference=collector.datetime.fromisoformat("2026-10-26T14:00:00+00:00"))
            self.assertEqual(result["latest"]["observation_times"]["disk"], new)

    def test_summary_preserves_counts_and_window_statistics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = []
            for index, value in enumerate((10, 20, 30, 40, 50, 60)):
                rows.append(json.dumps({"schema": collector.SCHEMA, "observed_at": f"2026-10-24T12:{index:02d}:00+00:00", "cpu": {"utilization_percent": value}, "memory": {"available_bytes": 1000 - value}}))
            (root / "samples.jsonl").write_text("\n".join(rows) + "\n")
            result = collector.summarize(root, reference=collector.datetime(2026, 10, 24, 12, 15, tzinfo=collector.timezone.utc))
        self.assertEqual(result["sample_count"], 6)
        self.assertEqual(result["coverage_seconds"], 900)
        self.assertEqual(result["cpu"]["average_percent"], 35)
        self.assertIsNone(result["cpu"]["peak_sustained_5m_percent"])
        self.assertEqual(result["cpu"]["max_observed_percent"], 60)
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
            for stamp in ("12:00", "12:15", "12:45", "13:00"):
                rows.append(json.dumps({"schema": collector.SCHEMA, "observed_at": f"2026-10-24T{stamp}:00+00:00", "cpu": {"utilization_percent": 10}, "memory": {"available_bytes": 100}}))
            (root / "samples.jsonl").write_text("\n".join(rows) + "\n")
            result = collector.summarize(root, reference=collector.datetime(2026, 10, 24, 12, 15, tzinfo=collector.timezone.utc))
        self.assertEqual(result["window"]["start_at"], "2026-10-24T12:00:00+00:00")
        self.assertEqual(result["window"]["end_at"], "2026-10-24T12:15:00+00:00")
        self.assertEqual(result["window"]["sample_gaps"], 0)
        self.assertTrue(result["window"]["complete"])

    def test_sustained_peak_never_spans_a_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [json.dumps({"schema": collector.SCHEMA, "observed_at": f"2026-10-24T12:{minute:02d}:00+00:00", "cpu": {"utilization_percent": 100}, "memory": {"available_bytes": 100}}) for minute in (0, 1, 2, 7, 8, 9, 13, 14)]
            (root / "samples.jsonl").write_text("\n".join(rows) + "\n")
            result = collector.summarize(root, reference=collector.datetime(2026, 10, 24, 12, 15, tzinfo=collector.timezone.utc))
        self.assertIsNone(result["cpu"]["peak_sustained_5m_percent"])

    def test_aws_delayed_collection_refreshes_existing_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            export = Path(directory) / "export"
            export.mkdir()
            (export / "status.json").write_text('{"old":true}')
            stamp = collector.datetime(2026, 10, 26, 12, 46, tzinfo=collector.timezone.utc)
            with patch.dict(collector.os.environ, {"PROJECT_PAPER_RESOURCE_ENVIRONMENT": "aws-production", "PROJECT_PAPER_RESOURCE_EXPORT_DIR": str(export)}), patch.object(collector, "now", return_value=stamp), patch.object(collector, "filesystem", return_value={"used_percent": 55}):
                collector.collect(root)
            status = json.loads((export / "status.json").read_text())
            self.assertEqual(status["latest"]["disk"]["used_percent"], 55)
            self.assertEqual(status["latest"]["observation_times"]["disk"], stamp.isoformat())
            self.assertEqual(status["observed_at"], stamp.isoformat())

    def test_collect_publishes_atomic_summary_without_database_access(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(collector, "now", return_value=collector.datetime(2026, 10, 24, 12, 15, tzinfo=collector.timezone.utc)):
            root = Path(directory)
            collector.collect(root)
            status = json.loads((root / "status.json").read_text())
            self.assertEqual(status["schema"], collector.SCHEMA)
            self.assertEqual(status["sample_count"], 0)
            self.assertTrue((root / "summaries.jsonl").exists())


if __name__ == "__main__":
    unittest.main()

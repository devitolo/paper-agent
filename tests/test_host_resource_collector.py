import json
from pathlib import Path
import tempfile
import unittest
from datetime import timedelta
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

    def test_corrupt_numeric_intervals_cannot_crash_or_create_coverage(self):
        for seconds, value in ((1e308, 50), (10**1000, 50), (900, 10**1000)):
            with self.subTest(kind=(type(seconds).__name__, type(value).__name__)):
                rows = [{"observed_at": "2026-10-26T14:15:00+00:00", "cpu": {"utilization_percent": value, "interval_seconds": seconds}}]
                result = self.summarize_rows(rows, collector.datetime.fromisoformat(rows[0]["observed_at"]))
                self.assertIsNone(result["cpu"]["average_percent"])
                self.assertEqual(result["coverage_seconds"], 0)
                self.assertFalse(result["window"]["complete"])

    def test_many_fractional_retries_have_exact_microsecond_coverage(self):
        start = collector.datetime(2026, 10, 26, 14, tzinfo=collector.timezone.utc)
        for count in (10, 15, 30):
            with self.subTest(count=count):
                stamps = [start + timedelta(seconds=900 * index / count + (6.1 if index % 2 == 0 else 6.2)) for index in range(count + 1)]
                rows = [{"observed_at": right.isoformat(), "cpu": {"utilization_percent": 50, "interval_seconds": (right-left).total_seconds()}} for left, right in zip(stamps, stamps[1:])]
                rows.insert(0, {"observed_at": stamps[0].isoformat(), "cpu": {"utilization_percent": 50, "interval_seconds": 900}})
                result = self.summarize_rows(rows, stamps[-1])
                self.assertEqual(result["coverage_seconds"], 900)
                self.assertTrue(result["window"]["complete"])
                self.assertEqual(result["window"]["sample_gaps"], 0)

    def test_fractional_adjacent_intervals_do_not_report_rounding_as_a_gap(self):
        rows = [
            {"observed_at": "2026-10-26T17:45:06.100000+00:00", "cpu": {"utilization_percent": 20, "interval_seconds": 900}},
            {"observed_at": "2026-10-26T18:00:06.200000+00:00", "cpu": {"utilization_percent": 50, "interval_seconds": 900.1}},
        ]
        result = self.summarize_rows(rows, collector.datetime.fromisoformat(rows[-1]["observed_at"]))
        self.assertEqual(result["window"]["sample_gaps"], 0)
        self.assertTrue(result["window"]["complete"])
        self.assertEqual(result["cpu"]["average_percent"], 49.8)

    def test_rapid_retries_preserve_distinct_intervals_in_one_minute(self):
        rows = [
            {"observed_at": "2026-10-26T14:14:10+00:00", "cpu": {"utilization_percent": 10, "interval_seconds": 850}},
            {"observed_at": "2026-10-26T14:14:50+00:00", "cpu": {"utilization_percent": 40, "interval_seconds": 40}},
            {"observed_at": "2026-10-26T14:15:00+00:00", "cpu": {"utilization_percent": 70, "interval_seconds": 10}},
        ]
        result = self.summarize_rows(rows, collector.datetime.fromisoformat(rows[-1]["observed_at"]))
        self.assertEqual(result["sample_count"], 3)
        self.assertEqual(result["coverage_seconds"], 900)
        self.assertEqual(result["cpu"]["average_percent"], 12)
        self.assertTrue(result["window"]["complete"])

    def summarize_rows(self, rows, reference):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "samples.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
            return collector.summarize(root, reference=reference)

    def test_late_intervals_weight_only_their_overlap_with_declared_window(self):
        rows = [
            {"observed_at": "2026-10-26T14:00:06+00:00", "cpu": {"utilization_percent": 80, "interval_seconds": 900}},
            {"observed_at": "2026-10-26T14:15:06+00:00", "cpu": {"utilization_percent": 20, "interval_seconds": 900}},
        ]
        result = self.summarize_rows(rows, collector.datetime.fromisoformat(rows[-1]["observed_at"]))
        self.assertEqual(result["cpu"]["average_percent"], 20.4)
        self.assertEqual(result["coverage_seconds"], 900)
        self.assertTrue(result["window"]["complete"])
        self.assertEqual(result["window"]["sample_gaps"], 0)

    def test_jitter_does_not_create_a_gap_in_contiguous_cpu_intervals(self):
        rows = [
            {"observed_at": "2026-10-26T14:00:06.835892+00:00", "cpu": {"utilization_percent": 80, "interval_seconds": 900}},
            {"observed_at": "2026-10-26T14:15:07.271252+00:00", "cpu": {"utilization_percent": 20, "interval_seconds": 900.43536}},
        ]
        result = self.summarize_rows(rows, collector.datetime.fromisoformat(rows[-1]["observed_at"]))
        self.assertEqual(result["window"]["sample_gaps"], 0)
        self.assertEqual(result["coverage_seconds"], 900)
        self.assertTrue(result["window"]["complete"])

    def test_summary_preserves_counts_and_window_statistics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = []
            for index, value in enumerate((10, 20, 30, 40, 50, 60)):
                rows.append(json.dumps({"schema": collector.SCHEMA, "observed_at": f"2026-10-24T12:{index * 2:02d}:00+00:00", "cpu": {"utilization_percent": value, "interval_seconds": 120}, "memory": {"available_bytes": 1000 - value}}))
            (root / "samples.jsonl").write_text("\n".join(rows) + "\n")
            result = collector.summarize(root, reference=collector.datetime(2026, 10, 24, 12, 15, tzinfo=collector.timezone.utc))
        self.assertEqual(result["sample_count"], 5)
        self.assertEqual(result["coverage_seconds"], 600)
        self.assertEqual(result["cpu"]["average_percent"], 40)
        self.assertIsNone(result["cpu"]["peak_sustained_5m_percent"])
        self.assertEqual(result["cpu"]["max_observed_percent"], 60)
        self.assertIsNone(result["cpu"]["max_one_minute_percent"])
        self.assertEqual(result["memory"]["available_max_bytes"], 980)

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
            for minute in (0, 15, 45, 60):
                rows.append(json.dumps({"schema": collector.SCHEMA, "observed_at": f"2026-10-24T{12 + minute // 60:02d}:{minute % 60:02d}:00+00:00", "cpu": {"utilization_percent": 10}, "memory": {"available_bytes": 100}}))
            (root / "samples.jsonl").write_text("\n".join(rows) + "\n")
            result = collector.summarize(root, reference=collector.datetime(2026, 10, 24, 12, 15, tzinfo=collector.timezone.utc))
        self.assertEqual(result["window"]["start_at"], "2026-10-24T12:00:00+00:00")
        self.assertEqual(result["window"]["end_at"], "2026-10-24T12:15:00+00:00")
        self.assertEqual(result["window"]["sample_gaps"], 0)
        self.assertFalse(result["window"]["complete"])

    def test_sustained_peak_never_spans_a_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [json.dumps({"schema": collector.SCHEMA, "observed_at": f"2026-10-24T12:{minute:02d}:00+00:00", "cpu": {"utilization_percent": 100}, "memory": {"available_bytes": 100}}) for minute in (0, 1, 2, 7, 8, 9, 13, 14)]
            (root / "samples.jsonl").write_text("\n".join(rows) + "\n")
            result = collector.summarize(root, reference=collector.datetime(2026, 10, 24, 12, 15, tzinfo=collector.timezone.utc))
        self.assertIsNone(result["cpu"]["peak_sustained_5m_percent"])

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

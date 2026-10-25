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

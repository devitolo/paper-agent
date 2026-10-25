import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from paper_agents.resource_status import SCHEMA, load_resource_status


class ResourceStatusTests(unittest.TestCase):
    def test_missing_is_unavailable_and_old_observation_is_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            self.assertEqual(load_resource_status(path)["state"], "unavailable")
            path.write_text(json.dumps({"schema": SCHEMA, "observed_at": "2026-10-24T00:00:00+00:00", "sample_count": 15}))
            result = load_resource_status(path, now=datetime(2026, 10, 24, 1, 0, tzinfo=timezone.utc))
        self.assertEqual(result["state"], "stale")
        self.assertEqual(result["sample_count"], 15)


if __name__ == "__main__":
    unittest.main()

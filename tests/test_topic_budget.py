import sqlite3
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from paper_agents.topic_budget import finish, reserve, status


class TopicBudgetTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")

    def test_reservation_is_serialized_and_consumed(self):
        now = datetime(2026, 11, 1, 7, 0, tzinfo=ZoneInfo("UTC"))
        first = reserve(self.db, limit=1, now=now)
        second = reserve(self.db, limit=1, now=now)
        self.assertTrue(first["reserved"])
        self.assertFalse(second["reserved"])
        finish(self.db, day=first["day"], succeeded=False)
        self.assertEqual(status(self.db, limit=1, now=now)["remaining"], 0)

    def test_reset_uses_pacific_calendar_day(self):
        before = datetime(2026, 3, 8, 7, 59, tzinfo=ZoneInfo("UTC"))
        after = datetime(2026, 3, 8, 8, 1, tzinfo=ZoneInfo("UTC"))
        self.assertNotEqual(status(self.db, now=before)["day"], status(self.db, now=after)["day"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from delivery.be.freshness import collect_freshness


class DeliveryFreshnessTest(unittest.TestCase):
    def test_collect_freshness_returns_site_dates_and_exclusive_buckets(self):
        measured_at = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
        rows = [
            {"site_id": "current", "site_name": "Current", "sheet": "API",
             "documents": 3, "last_collected_at": measured_at - timedelta(days=7)},
            {"site_id": "month", "site_name": "Month", "sheet": "HTML",
             "documents": 2, "last_collected_at": measured_at - timedelta(days=8)},
            {"site_id": "quarter", "site_name": "Quarter", "sheet": None,
             "documents": 1, "last_collected_at": measured_at - timedelta(days=31)},
            {"site_id": "old", "site_name": "Old", "sheet": None,
             "documents": 1, "last_collected_at": measured_at - timedelta(days=91)},
            {"site_id": "empty", "site_name": "Empty", "sheet": None,
             "documents": 0, "last_collected_at": None},
        ]
        conn = Mock()
        conn.execute.return_value.fetchall.return_value = rows

        result = collect_freshness(conn, measured_at=measured_at)

        self.assertEqual(result["summary"], {
            "total_sites": 5,
            "with_documents": 4,
            "never_collected": 1,
            "distribution": {
                "within_7_days": 1,
                "8_to_30_days": 1,
                "31_to_90_days": 1,
                "over_90_days": 1,
                "never": 1,
            },
        })
        self.assertEqual(result["sites"][1]["age_days"], 8)
        self.assertEqual(result["sites"][4]["freshness_bucket"], "never")
        self.assertEqual(result["measured_at"], measured_at)
        sql = conn.execute.call_args.args[0]
        self.assertIn("LEFT JOIN documents", sql)
        self.assertNotIn("INSERT", sql.upper())
        self.assertNotIn("UPDATE", sql.upper())

    def test_future_timestamp_is_treated_as_current(self):
        measured_at = datetime(2026, 8, 12, tzinfo=timezone.utc)
        conn = Mock()
        conn.execute.return_value.fetchall.return_value = [{
            "site_id": "future", "site_name": "Future", "sheet": None,
            "documents": 1, "last_collected_at": measured_at + timedelta(hours=1),
        }]

        result = collect_freshness(conn, measured_at=measured_at)

        self.assertEqual(result["sites"][0]["age_days"], 0)
        self.assertEqual(result["sites"][0]["freshness_bucket"], "within_7_days")


if __name__ == "__main__":
    unittest.main()

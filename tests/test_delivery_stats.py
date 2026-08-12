from __future__ import annotations

import unittest
from unittest.mock import Mock

from delivery.be.stats import collect_stats


class DeliveryStatsTest(unittest.TestCase):
    def test_collect_stats_serializes_rows_and_marks_blob_unmeasured(self):
        conn = Mock()
        conn.execute.side_effect = [
            Mock(fetchone=lambda: {"sites": 2, "documents": 3, "pdf_downloaded": 2,
                                   "text_extracted": 1, "pdf_bytes": 42,
                                   "latest_collected_at": None}),
            Mock(fetchall=lambda: [{"key": "Korea", "sites": 1, "documents": 3}]),
            Mock(fetchall=lambda: [{"key": "site-a", "documents": 3}]),
            Mock(fetchone=lambda: {"orphan_documents": 0, "missing_pdf_metadata": 1}),
        ]
        result = collect_stats(conn)
        self.assertEqual(result["overview"]["documents"], 3)
        self.assertEqual(result["by_sheet"][0]["key"], "Korea")
        self.assertEqual(result["integrity"]["blob_check"]["status"], "not_measured")
        self.assertIsNotNone(result["measured_at"].tzinfo)


if __name__ == "__main__":
    unittest.main()

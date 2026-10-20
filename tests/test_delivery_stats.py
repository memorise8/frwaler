from __future__ import annotations

import unittest
from unittest.mock import Mock

from delivery.be.stats import collect_stats


class DeliveryStatsTest(unittest.TestCase):
    def test_collect_stats_serializes_rows_and_marks_blob_unmeasured(self):
        conn = Mock()
        conn.execute.side_effect = [
            Mock(fetchone=lambda: {"relation": None}),  # document_counts absent: live fallback
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
        self.assertEqual(result["source"], "live")

    def test_collect_stats_reads_counts_table_when_refreshed(self):
        conn = Mock()
        conn.execute.side_effect = [
            Mock(fetchone=lambda: {"relation": "document_counts"}),
            Mock(fetchone=lambda: {"present": True}),
            Mock(fetchone=lambda: {"sites": 2, "documents": 4_200_000, "pdf_downloaded": 2,
                                   "text_extracted": 1, "pdf_bytes": 42,
                                   "latest_collected_at": None}),
            Mock(fetchall=lambda: [{"key": "Korea", "sites": 1, "documents": 4_200_000}]),
            Mock(fetchall=lambda: [{"key": "scienceon-api", "documents": 3_400_000}]),
            Mock(fetchone=lambda: {"orphan_documents": 0, "missing_pdf_metadata": 1,
                                   "counts_refreshed_at": "2026-10-20T00:00:00Z"}),
        ]
        result = collect_stats(conn)
        self.assertEqual(result["source"], "counts")
        self.assertEqual(result["overview"]["documents"], 4_200_000)
        self.assertEqual(result["by_site"][0]["key"], "scienceon-api")
        self.assertEqual(result["integrity"], {"orphan_documents": 0, "missing_pdf_metadata": 1,
                                               "blob_check": {"status": "not_measured"}})
        self.assertEqual(result["counts_refreshed_at"], "2026-10-20T00:00:00Z")
        sql = " ".join(call.args[0] for call in conn.execute.call_args_list)
        self.assertNotIn("count(d.seq_id)", sql)


if __name__ == "__main__":
    unittest.main()

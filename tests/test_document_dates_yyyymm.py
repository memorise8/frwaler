import sqlite3
import unittest

from crawler import db_libertree
from crawler.document_dates import normalize_date, normalize_document_dates


class CompactYearMonthTests(unittest.TestCase):
    def check(self, raw, value, status, precision):
        self.assertEqual(normalize_date(raw), {
            "value": value, "raw": raw, "status": status, "precision": precision})

    def test_valid_yyyymm_is_month_precision_without_invented_day(self):
        for raw in ("194912", "195001", "199912", "202601"):
            self.check(raw, None, "partial", "month")

    def test_invalid_month_or_year_stays_unrecognized(self):
        for raw in ("194900", "194913", "202699", "000001", "19491", "1949120", "199912x"):
            self.check(raw, None, "unrecognized", None)

    def test_existing_shapes_are_unchanged(self):
        self.check("1949", None, "partial", "year")
        self.check("19491201", "1949-12-01", "normalized", "day")
        self.check("2026-01", None, "partial", "month")
        self.check("", None, "missing", None)

    def test_document_columns_keep_raw_and_leave_date_null(self):
        doc = normalize_document_dates({"site_id": "scienceon-api", "published_date": "194912"})
        self.assertIsNone(doc["published_date"])
        self.assertEqual(
            (doc["published_date_raw"], doc["published_date_status"], doc["published_date_precision"]),
            ("194912", "partial", "month"))

    def test_saved_row_records_month_precision(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        db_libertree.init_db(conn)
        conn.execute("INSERT INTO sites(site_id,site_name,site_url) VALUES('s','s','https://example.invalid')")
        conn.commit()
        db_libertree.insert_document(conn, {
            "site_id": "s", "post_number": "1", "meta_url": "https://example.invalid/1",
            "title": "t", "published_date": "194912"})
        row = conn.execute("SELECT * FROM documents").fetchone()
        self.assertIsNone(row["published_date"])
        self.assertEqual((row["published_date_raw"], row["published_date_status"],
                          row["published_date_precision"]), ("194912", "partial", "month"))


if __name__ == "__main__":
    unittest.main()

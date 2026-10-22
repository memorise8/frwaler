from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from delivery.be.catalogue import _facets_from_counts, _fallback_facets, _hydrate  # noqa: E402
from delivery.be.document_detail import collect_document_detail  # noqa: E402
from delivery.be.freshness import collect_freshness  # noqa: E402
from delivery.be.site_names import display_site_name  # noqa: E402


class DisplaySiteNameTest(unittest.TestCase):
    def test_strips_generated_crawler_prefix(self):
        self.assertEqual(display_site_name("Custom: prism-go-kr-homepage"), "prism-go-kr-homepage")
        self.assertEqual(display_site_name("Custom:cerema-fr-fr"), "cerema-fr-fr")

    def test_leaves_other_names_alone(self):
        for name in ("Académie", "Customs Service", "My Custom: site", None, "", "Custom:"):
            self.assertEqual(display_site_name(name), name)


class SiteNameResponsesTest(unittest.TestCase):
    """API payloads show the name without the prefix; the stored value is not rewritten."""

    def test_catalogue_facets_from_counts(self):
        rows = [{"site_id": "prism", "lang": "ko", "n": 3, "site_name": "Custom: prism-go-kr"}]
        facets = _facets_from_counts(rows, {})
        self.assertEqual(facets["sites"], [{"value": "prism", "label": "prism-go-kr", "count": 3}])

    def test_catalogue_fallback_facets(self):
        conn = Mock()
        conn.execute.return_value.fetchall.return_value = [{"site_id": "prism", "site_name": "Custom: prism-go-kr"}]
        self.assertEqual(_fallback_facets(conn, {})["sites"][0]["label"], "prism-go-kr")

    def test_catalogue_items(self):
        conn = Mock()
        conn.execute.return_value.fetchall.return_value = [{"seq_id": 7, "site_id": "prism", "site_name": "Custom: prism-go-kr"}]
        [item] = _hydrate(conn, [7], {})
        self.assertEqual(item["site_name"], "prism-go-kr")
        self.assertEqual(item["doc_type"], "기타")

    def test_document_detail(self):
        row = {key: None for key in (
            "lang", "title", "abstract", "authors", "publisher", "journal", "keywords",
            "published_date", "listed_date", "published_date_raw", "published_date_status",
            "published_date_precision", "listed_date_raw", "listed_date_status",
            "listed_date_precision", "collected_at", "summary", "summary_model",
            "meta_url", "pdf_url", "pdf_size_bytes", "original_filename")}
        row.update(seq_id=7, site_id="prism", site_name="Custom: prism-go-kr", site_url="https://p.test",
                   sheet=None, pdf_downloaded=0, text_extracted=0)
        conn = Mock()
        conn.execute.return_value.fetchone.side_effect = [row, None]
        conn.execute.return_value.fetchall.return_value = []
        detail = collect_document_detail(conn, 7, {})
        self.assertEqual(detail["site"]["site_name"], "prism-go-kr")
        self.assertEqual(detail["site"]["site_id"], "prism")

    def test_freshness(self):
        conn = Mock()
        conn.execute.return_value.fetchall.return_value = [{
            "site_id": "prism", "site_name": "Custom: prism-go-kr", "sheet": None,
            "documents": 0, "last_collected_at": None}]
        result = collect_freshness(conn, measured_at=datetime(2026, 10, 22, tzinfo=timezone.utc))
        self.assertEqual(result["sites"][0]["site_name"], "prism-go-kr")


if __name__ == "__main__":
    unittest.main()

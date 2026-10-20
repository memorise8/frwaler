import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")


@unittest.skipUnless(TEST_PG_DSN, "set TEST_PG_DSN to run")
class DeliveryCatalogueTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from crawler import db_pg
        from delivery.be.app import create_app

        conn = db_pg.open_db(TEST_PG_DSN)
        for table in ("document_counts", "crawl_jobs", "document_translations", "document_lang", "documents", "sites"):
            conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        conn.commit()
        db_pg.init_db(conn)
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        conn.execute("ALTER TABLE documents ADD COLUMN fts tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(abstract, ''))) STORED")
        conn.execute("CREATE INDEX idx_test_doc_fts ON documents USING GIN (fts)")
        conn.execute("CREATE INDEX idx_test_doc_title_trgm ON documents USING GIN (title gin_trgm_ops)")
        conn.execute("CREATE TABLE document_lang (seq_id BIGINT PRIMARY KEY REFERENCES documents(seq_id), lang TEXT NOT NULL)")
        conn.execute("""
            CREATE TABLE document_translations (
              translation_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
              seq_id BIGINT NOT NULL REFERENCES documents(seq_id), state TEXT NOT NULL,
              translation_text TEXT
            )
        """)
        db_pg.upsert_site(conn, "academie-sciences-fr-espace-presse", "Académie", "https://a.test", "French Public Institutions")
        db_pg.upsert_site(conn, "unknown-fixture", "Unknown Fixture", "https://u.test", "Unknown")
        self.first = db_pg.insert_document(conn, {
            "site_id": "academie-sciences-fr-espace-presse", "post_number": "1",
            "meta_url": "https://a.test/1", "title": "Climate change budget",
            "abstract": "Energy policy", "published_date": "2025-01-15",
            "pdf_downloaded": 1, "text_extracted": 1, "publisher": "Académie",
        })
        self.second = db_pg.insert_document(conn, {
            "site_id": "academie-sciences-fr-espace-presse", "post_number": "2",
            "meta_url": "https://a.test/2", "title": "기후 변화 연구",
            "published_date": "2024-02-01", "pdf_downloaded": 0, "text_extracted": 1,
        })
        self.third = db_pg.insert_document(conn, {
            "site_id": "unknown-fixture", "post_number": "3",
            "meta_url": "https://u.test/3", "title": "Unclassified record",
            "published_date": "not-a-date", "pdf_downloaded": 1, "text_extracted": 0,
        })
        conn.execute("INSERT INTO document_lang(seq_id, lang) VALUES (%s, 'en'), (%s, 'ko')", (self.first, self.second))
        conn.execute("INSERT INTO document_translations(seq_id, state, translation_text) VALUES (%s, 'completed', '번역')", (self.first,))
        conn.commit()
        conn.close()
        self.client = TestClient(create_app(TEST_PG_DSN))

    def test_empty_query_pagination_and_facets(self):
        from crawler import db_pg
        from delivery.be.counts import refresh_counts
        conn = db_pg.open_db(TEST_PG_DSN)
        refresh_counts(conn)
        conn.close()
        body = self.client.get("/documents?page=1&page_size=2&sort=seq_desc").json()
        pagination = body["pagination"]
        self.assertEqual({k: pagination[k] for k in ("page", "page_size", "total", "total_exact", "pages")},
                         {"page": 1, "page_size": 2, "total": 3, "total_exact": True, "pages": 2})
        self.assertTrue(pagination["has_next"])
        self.assertFalse(pagination["has_prev"])
        self.assertIsNotNone(pagination["next_cursor"])
        self.assertEqual([item["seq_id"] for item in body["items"]], [self.third, self.second])
        self.assertEqual(body["items"][0]["country"], "기타")
        self.assertEqual(body["items"][0]["lang"], "unknown")
        self.assertEqual(sum(row["count"] for row in body["facets"]["countries"]), 3)
        self.assertEqual(body["total"]["source"], "counts")

    def test_multilingual_search_and_file_filters(self):
        site = "academie-sciences-fr-espace-presse"
        korean = self.client.get("/documents", params={"q": "기후 변화", "site_id": site}).json()
        self.assertEqual(korean["pagination"]["total"], 1)
        self.assertEqual(korean["items"][0]["seq_id"], self.second)
        english = self.client.get("/documents", params={"q": "climate", "site_id": site,
                                                        "has_pdf": "true", "lang": "en"}).json()
        self.assertEqual(english["pagination"]["total"], 1)
        self.assertTrue(english["items"][0]["has_translation"])
        self.assertEqual(korean["query"]["sort"], "published_desc")  # default with q; relevance is opt-in
        ranked = self.client.get("/documents", params={"q": "climate", "site_id": site, "sort": "relevance"}).json()
        self.assertEqual(ranked["query"]["sort"], "relevance")

    def test_combined_taxonomy_and_date_filters(self):
        response = self.client.get("/documents", params={
            "country": "프랑스", "doc_type": "보도자료", "published_from": "2025-01-01",
            "published_to": "2025-12-31", "has_text": "true",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pagination"]["total"], 1)
        self.assertEqual(response.json()["items"][0]["site_name"], "Académie")

    def test_last_and_empty_pages(self):
        last = self.client.get("/documents?page=2&page_size=2&sort=seq_desc").json()
        self.assertEqual(len(last["items"]), 1)
        empty = self.client.get("/documents?page=3&page_size=2&sort=seq_desc").json()
        self.assertEqual(empty["items"], [])
        self.assertEqual(empty["pagination"]["pages"], 2)

    def test_invalid_parameters(self):
        for path in (
            "/documents?page_size=101", "/documents?sort=relevance",
            "/documents?country=Atlantis", "/documents?doc_type=invalid",
            "/documents?published_from=2025-02-01&published_to=2025-01-01",
            "/documents?collected_from=2025-02-01&collected_to=2025-01-01",
            "/documents?q=climate", "/documents?page=6", "/documents?cursor=bogus",
        ):
            self.assertEqual(self.client.get(path).status_code, 422, path)

    def test_date_sort_and_filter_ignore_calendar_invalid_values(self):
        from crawler import db_pg
        conn=db_pg.open_db(TEST_PG_DSN)
        conn.execute("UPDATE documents SET published_date='2025-99-99' WHERE seq_id=%s",(self.third,))
        conn.commit();conn.close()
        sorted_response=self.client.get("/documents?sort=published_desc")
        self.assertEqual(sorted_response.status_code,200)
        self.assertEqual(sorted_response.json()["items"][-1]["seq_id"],self.third)
        filtered=self.client.get("/documents?published_from=2025-01-01")
        self.assertEqual(filtered.status_code,200)
        self.assertNotIn(self.third,[item["seq_id"] for item in filtered.json()["items"]])

    def _walk(self, params, direction="next_cursor"):
        seen, cursor = [], None
        for _ in range(10):
            query = dict(params, **({"cursor": cursor} if cursor else {}))
            response = self.client.get("/documents", params=query)
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            seen.extend(item["seq_id"] for item in body["items"])
            cursor = body["pagination"][direction]
            if not cursor:
                return seen, body
        self.fail("cursor walk did not terminate")

    def test_cursor_walk_matches_offset_order_for_every_sort(self):
        from crawler import db_pg
        conn = db_pg.open_db(TEST_PG_DSN)
        for n in range(4, 12):
            db_pg.insert_document(conn, {
                "site_id": "unknown-fixture", "post_number": str(n), "meta_url": f"https://u.test/{n}",
                "title": f"Record {n}", "published_date": None if n % 3 == 0 else f"2024-0{n % 9 + 1}-01",
            })
        conn.execute("UPDATE documents SET collected_at = NULL WHERE post_number = '5'")
        conn.execute("UPDATE documents SET collected_at = '2026-01-01T00:00:00Z' WHERE post_number IN ('6','7')")
        conn.commit()
        conn.close()
        for sort in ("seq_desc", "collected_desc", "published_desc"):
            full = [i["seq_id"] for i in self.client.get(
                "/documents", params={"sort": sort, "page_size": 100}).json()["items"]]
            self.assertEqual(len(full), 11)
            walked, last = self._walk({"sort": sort, "page_size": 3})
            self.assertEqual(walked, full, sort)
            self.assertFalse(last["pagination"]["has_next"])
            back, first = self._walk({"sort": sort, "page_size": 3, "cursor": last["pagination"]["prev_cursor"]},
                                     "prev_cursor")
            self.assertFalse(first["pagination"]["has_prev"])
            self.assertEqual(first["items"][0]["seq_id"], full[0], sort)
        published = [i for i in self.client.get("/documents", params={"sort": "published_desc",
                                                                       "page_size": 100}).json()["items"]]
        dates = [i["published_date"] for i in published]
        valid = [d for d in dates if d and d[:4].isdigit() and d != "2024-02-30"]
        self.assertEqual(dates[:len(valid)], sorted(valid, reverse=True))

        # Small filtered results are read in one pass and keyset-ordered in Python;
        # the walk must match the SQL order exactly, NULL keys last.
        for sort in ("collected_desc", "published_desc", "seq_desc"):
            params = {"sort": sort, "collected_from": "2000-01-01"}
            full = [i["seq_id"] for i in self.client.get(
                "/documents", params=dict(params, page_size=100)).json()["items"]]
            walked, last = self._walk(dict(params, page_size=4))
            self.assertEqual(walked, full, sort)
            self.assertEqual(sorted(full), sorted(set(full)))
        unordered = self.client.get("/documents", params={"sort": "collected_desc", "page_size": 100}).json()
        dated = self.client.get("/documents", params={"sort": "collected_desc", "page_size": 100,
                                                      "collected_from": "2000-01-01"}).json()
        self.assertEqual([i["seq_id"] for i in dated["items"]],
                         [i["seq_id"] for i in unordered["items"] if i["collected_at"] is not None])

    def test_cursor_from_other_filters_is_rejected(self):
        body = self.client.get("/documents", params={"page_size": 1, "sort": "seq_desc"}).json()
        response = self.client.get("/documents", params={"page_size": 1, "sort": "seq_desc", "has_pdf": "true",
                                                         "cursor": body["pagination"]["next_cursor"]})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["reason"], "cursor_filter_mismatch")

    def test_counts_table_drives_totals_facets_and_stats(self):
        from crawler import db_pg
        from delivery.be.counts import refresh_counts
        conn = db_pg.open_db(TEST_PG_DSN)
        refresh_counts(conn)
        body = self.client.get("/documents", params={"has_pdf": "true"}).json()
        self.assertEqual(body["total"]["source"], "counts")
        self.assertEqual(body["pagination"]["total"], 2)
        self.assertEqual({f["value"]: f["count"] for f in body["facets"]["languages"]}, {"en": 1, "unknown": 1})
        self.assertEqual({f["value"]: f["count"] for f in body["facets"]["countries"]}, {"프랑스": 1, "기타": 1})
        other = self.client.get("/documents", params={"country": "기타"}).json()
        self.assertEqual([i["seq_id"] for i in other["items"]], [self.third])
        self.assertEqual(other["pagination"]["total"], 1)
        # per-site refresh replaces only that site's rows
        conn.execute("UPDATE documents SET text_extracted = 1 WHERE seq_id = %s", (self.third,))
        conn.commit()
        refresh_counts(conn, ["unknown-fixture"])
        texts = self.client.get("/documents", params={"has_text": "true"}).json()
        self.assertEqual(texts["pagination"]["total"], 3)
        conn.close()
        stats = self.client.get("/stats").json()
        self.assertEqual(stats["source"], "counts")
        self.assertEqual(stats["overview"]["documents"], 3)
        self.assertEqual(stats["overview"]["text_extracted"], 3)
        self.assertEqual({row["key"]: row["documents"] for row in stats["by_site"]},
                         {"academie-sciences-fr-espace-presse": 2, "unknown-fixture": 1})

    def test_stale_zero_count_still_lists_new_matches(self):
        from crawler import db_pg
        from delivery.be.counts import refresh_counts
        conn = db_pg.open_db(TEST_PG_DSN)
        refresh_counts(conn)
        # after the refresh the unknown-fixture site has no text: count row (has_text) is 0
        conn.execute("UPDATE documents SET text_extracted = 1 WHERE seq_id = %s", (self.third,))
        conn.commit()
        conn.close()
        body = self.client.get("/documents", params={"site_id": "unknown-fixture", "has_text": "true"}).json()
        self.assertEqual([i["seq_id"] for i in body["items"]], [self.third])
        self.assertFalse(body["pagination"]["total_exact"])

    def test_empty_database(self):
        from crawler import db_pg
        conn = db_pg.open_db(TEST_PG_DSN)
        conn.execute("TRUNCATE documents CASCADE")
        conn.commit()
        conn.close()
        body = self.client.get("/documents").json()
        self.assertEqual(body["items"], [])
        self.assertEqual(body["pagination"]["total"], 0)
        self.assertEqual(body["pagination"]["pages"], 0)


@unittest.skipUnless(TEST_PG_DSN, "set TEST_PG_DSN to run")
class CatalogueMigrationLockTest(unittest.TestCase):
    def test_rerun_does_not_lock_documents_behind_a_reader(self):
        from crawler import db_pg
        from delivery.be.migrate import migrate
        from delivery.db.schema import init_catalogue_schema
        conn = db_pg.open_db(TEST_PG_DSN)
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
        conn.close()
        migrate(TEST_PG_DSN)
        reader = db_pg.open_db(TEST_PG_DSN)
        writer = db_pg.open_db(TEST_PG_DSN)
        try:
            reader.execute("SELECT count(*) FROM documents").fetchone()  # holds ACCESS SHARE until rollback
            writer.execute("SET lock_timeout = '2s'")
            init_catalogue_schema(writer)  # would wait (then fail) on an ACCESS EXCLUSIVE ALTER
            self.assertIsNotNone(writer.execute("SELECT to_regclass('public.document_counts') AS r").fetchone()["r"])
        finally:
            reader.rollback()
            reader.close()
            writer.close()


if __name__ == "__main__":
    unittest.main()

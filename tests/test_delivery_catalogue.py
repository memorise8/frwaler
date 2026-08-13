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
        for table in ("crawl_jobs", "document_translations", "document_lang", "documents", "sites"):
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
        body = self.client.get("/documents?page=1&page_size=2&sort=seq_desc").json()
        self.assertEqual(body["pagination"], {"page": 1, "page_size": 2, "total": 3, "pages": 2})
        self.assertEqual([item["seq_id"] for item in body["items"]], [self.third, self.second])
        self.assertEqual(body["items"][0]["country"], "기타")
        self.assertEqual(body["items"][0]["lang"], "unknown")
        self.assertEqual(sum(row["count"] for row in body["facets"]["countries"]), 3)

    def test_multilingual_search_and_file_filters(self):
        korean = self.client.get("/documents", params={"q": "기후 변화"}).json()
        self.assertEqual(korean["pagination"]["total"], 1)
        self.assertEqual(korean["items"][0]["seq_id"], self.second)
        english = self.client.get("/documents", params={"q": "climate", "has_pdf": "true", "lang": "en"}).json()
        self.assertEqual(english["pagination"]["total"], 1)
        self.assertTrue(english["items"][0]["has_translation"])

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


if __name__ == "__main__":
    unittest.main()

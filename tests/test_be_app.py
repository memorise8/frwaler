# -*- coding: utf-8 -*-
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")


@unittest.skipUnless(TEST_PG_DSN, "set TEST_PG_DSN to run")
class BeAppTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from crawler import db_pg
        from delivery.db import schema
        from delivery.be.app import create_app

        conn = db_pg.open_db(TEST_PG_DSN)
        for t in ("crawl_jobs", "document_translations", "document_lang", "documents", "sites"):
            conn.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
        conn.commit()
        db_pg.init_db(conn)
        schema.init_delivery_schema(conn)
        conn.execute("CREATE TABLE document_lang (seq_id BIGINT PRIMARY KEY REFERENCES documents(seq_id), lang TEXT NOT NULL)")
        conn.execute("""
            CREATE TABLE document_translations (
              translation_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
              seq_id BIGINT NOT NULL REFERENCES documents(seq_id), source_field TEXT NOT NULL,
              target_locale TEXT NOT NULL, source_fingerprint TEXT NOT NULL,
              model_version TEXT NOT NULL, prompt_version TEXT NOT NULL, state TEXT NOT NULL,
              translation_text TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_error_code TEXT,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              claimed_at TIMESTAMPTZ, completed_at TIMESTAMPTZ
            )
        """)
        db_pg.upsert_site(conn, "s1", "Site One", "https://s1.example")
        self.seq = db_pg.insert_document(conn, {"site_id": "s1", "post_number": "1",
                                                "meta_url": "https://s1/1", "title": "Doc One"})
        conn.close()
        self.client = TestClient(create_app(TEST_PG_DSN))

    def test_health_ok(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

    def test_get_document(self):
        r = self.client.get(f"/documents/{self.seq}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["source"]["title"], "Doc One")
        self.assertEqual(r.json()["source"]["lang"], "unknown")
        self.assertIsNone(r.json()["translations"]["title"])

    def test_get_document_404(self):
        r = self.client.get("/documents/999999")
        self.assertEqual(r.status_code, 404)

    def test_get_document_rejects_non_positive_id(self):
        self.assertEqual(self.client.get("/documents/0").status_code, 422)

    def test_get_document_with_latest_completed_translations(self):
        from crawler import db_pg
        conn = db_pg.open_db(TEST_PG_DSN)
        conn.execute("INSERT INTO document_lang(seq_id, lang) VALUES (%s, 'en')", (self.seq,))
        values = (self.seq, "a" * 64)
        conn.execute("""
          INSERT INTO document_translations
            (seq_id, source_field, target_locale, source_fingerprint, model_version,
             prompt_version, state, translation_text, completed_at)
          VALUES
            (%s, 'title', 'ko-KR', %s, 'old-model', 'v1', 'completed', '이전 제목', now() - interval '1 day'),
            (%s, 'title', 'ko-KR', %s, 'new-model', 'v2', 'completed', '문서 하나', now()),
            (%s, 'description', 'ko-KR', %s, 'model', 'v1', 'failed', NULL, NULL),
            (%s, 'title', 'en-US', %s, 'model', 'v1', 'completed', 'English title', now())
        """, values * 4)
        conn.commit()
        conn.close()
        body = self.client.get(f"/documents/{self.seq}").json()
        self.assertEqual(body["source"]["lang"], "en")
        self.assertEqual(body["translations"]["title"]["text"], "문서 하나")
        self.assertEqual(body["translations"]["title"]["model_version"], "new-model")
        self.assertIsNone(body["translations"]["description"])

    def test_post_and_get_job(self):
        r = self.client.post("/jobs", json={"site_id": "s1", "mode": "full", "limit_n": 3})
        self.assertEqual(r.status_code, 200)
        jid = r.json()["id"]
        r2 = self.client.get(f"/jobs/{jid}")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["site_id"], "s1")
        self.assertEqual(r2.json()["status"], "queued")

    def test_list_cancel_and_retry_jobs(self):
        created = self.client.post("/jobs", json={"site_id": "s1", "mode": "full"}).json()
        jid = created["id"]
        listed = self.client.get("/jobs?status=queued&limit=10").json()
        self.assertEqual([row["id"] for row in listed["jobs"]], [jid])

        cancelled = self.client.post(f"/jobs/{jid}/cancel")
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.assertEqual(self.client.post(f"/jobs/{jid}/cancel").status_code, 409)

        retried = self.client.post(
            f"/jobs/{jid}/retry", json={"requested_by": "test-operator"}
        )
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.json()["status"], "queued")
        self.assertEqual(retried.json()["retry_of"], jid)
        self.assertEqual(self.client.post(f"/jobs/{jid}/retry", json={}).status_code, 409)

    def test_job_management_not_found_and_invalid_filter(self):
        self.assertEqual(self.client.post("/jobs/999999/cancel").status_code, 404)
        self.assertEqual(self.client.post("/jobs/999999/retry", json={}).status_code, 404)
        self.assertEqual(self.client.get("/jobs?status=bogus").status_code, 422)

    def test_get_freshness(self):
        r = self.client.get("/freshness")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["summary"]["total_sites"], 1)
        self.assertEqual(body["summary"]["with_documents"], 1)
        self.assertEqual(body["sites"][0]["site_id"], "s1")
        self.assertEqual(body["sites"][0]["documents"], 1)
        self.assertEqual(body["sites"][0]["freshness_bucket"], "within_7_days")


if __name__ == "__main__":
    unittest.main()

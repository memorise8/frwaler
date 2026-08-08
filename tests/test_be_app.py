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
        for t in ("crawl_jobs", "documents", "sites"):
            conn.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
        conn.commit()
        db_pg.init_db(conn)
        schema.init_delivery_schema(conn)
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
        self.assertEqual(r.json()["title"], "Doc One")

    def test_get_document_404(self):
        r = self.client.get("/documents/999999")
        self.assertEqual(r.status_code, 404)

    def test_post_and_get_job(self):
        r = self.client.post("/jobs", json={"site_id": "s1", "mode": "full", "limit_n": 3})
        self.assertEqual(r.status_code, 200)
        jid = r.json()["id"]
        r2 = self.client.get(f"/jobs/{jid}")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["site_id"], "s1")
        self.assertEqual(r2.json()["status"], "queued")


if __name__ == "__main__":
    unittest.main()

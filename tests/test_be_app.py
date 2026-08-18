# -*- coding: utf-8 -*-
import os
import sys
import unittest
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")


@unittest.skipUnless(TEST_PG_DSN, "set TEST_PG_DSN to run")
class BeAppTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from crawler import db_pg
        from delivery.db import schema
        from delivery.be.app import create_app

        self.auth_patch=mock.patch.dict(os.environ,{"DELIVERY_AUTH_MODE":"disabled"})
        self.auth_patch.start()
        conn = db_pg.open_db(TEST_PG_DSN)
        for t in ("document_summary_quality", "translation_job_attempts", "translation_worker_heartbeats", "translation_system_observations", "translation_jobs", "translation_batches", "document_summaries", "crawl_job_logs", "crawl_schedules", "crawl_jobs", "document_translations", "document_lang", "documents", "sites"):
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

    def test_secure_pdf_and_text_delivery(self):
        from crawler.blob_storage import get_blob_path
        from crawler import db_pg
        with tempfile.TemporaryDirectory() as root:
            pdf=get_blob_path(self.seq,"pdf",root=root);pdf.parent.mkdir(parents=True);pdf.write_bytes(b"%PDF-1.7 demo")
            text=get_blob_path(self.seq,"txt",root=root);text.write_text("extracted demo",encoding="utf-8")
            conn=db_pg.open_db(TEST_PG_DSN);conn.execute("""UPDATE documents SET pdf_downloaded=1,
              text_extracted=1,pdf_size_bytes=%s,original_filename='demo.pdf' WHERE seq_id=%s""",(pdf.stat().st_size,self.seq));conn.commit();conn.close()
            with mock.patch.dict(os.environ,{"LIBERTREE_BLOB_ROOT":root}):
                pdf_response=self.client.get(f"/documents/{self.seq}/pdf")
                text_response=self.client.get(f"/documents/{self.seq}/text")
            self.assertEqual(pdf_response.status_code,200);self.assertEqual(pdf_response.content,b"%PDF-1.7 demo")
            self.assertEqual(text_response.text,"extracted demo")
            self.assertIn("inline",pdf_response.headers["content-disposition"])
        self.assertEqual(self.client.get(f"/documents/{self.seq}/pdf").status_code,404)

    def test_mutations_require_configured_operator_token(self):
        token="test-secret-token-that-is-at-least-32-characters"
        with mock.patch.dict(os.environ,{"DELIVERY_AUTH_MODE":"token","DELIVERY_API_TOKEN":token}):
            denied=self.client.post("/jobs",json={"site_id":"s1"})
            allowed=self.client.post("/jobs",json={"site_id":"s1"},headers={"x-delivery-token":token})
        self.assertEqual(denied.status_code,401)
        self.assertEqual(allowed.status_code,200)

    def test_job_summary_reports_queue_depth_without_a_token(self):
        response = self.client.get("/jobs/summary")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("counts", body)
        self.assertIn("queued", body["counts"])
        for key in ("active", "finished_24h", "avg_seconds", "samples"):
            self.assertIn(key, body)

    # /jobs/{job_id} 가 먼저 매칭되면 "summary"를 작업 번호로 읽어 422가 된다.
    # 두 경로가 실제로 구별되는지를 보려면 양쪽을 함께 눌러야 한다 —
    # /jobs/summary 만 200인지 보는 것은 위 테스트의 반복일 뿐이다.
    def test_the_summary_path_and_the_job_detail_path_stay_distinct(self):
        self.assertEqual(self.client.get("/jobs/summary").status_code, 200)
        self.assertEqual(self.client.get("/jobs/999999999").status_code, 404)

    def test_missing_operator_token_configuration_fails_closed(self):
        with mock.patch.dict(os.environ,{"DELIVERY_AUTH_MODE":"token","DELIVERY_API_TOKEN":""}):
            response=self.client.post("/jobs",json={"site_id":"s1"})
        self.assertEqual(response.status_code,503)

    def test_backend_startup_auth_validation_rejects_missing_secret(self):
        from delivery.be.access import validate_auth_config
        with mock.patch.dict(os.environ,{"DELIVERY_AUTH_MODE":"token","DELIVERY_API_TOKEN":""}):
            with self.assertRaisesRegex(RuntimeError,"at least 32"):
                validate_auth_config()

    # The example placeholder is 48 characters, so the length check alone let
    # it through. A customer who copied .env.example and filled in only
    # POSTGRES_PASSWORD would have shipped with a token published in this
    # repository -- and nothing anywhere would have said so.
    def test_backend_startup_auth_validation_rejects_the_example_placeholder(self):
        from delivery.be.access import PLACEHOLDER_TOKENS, validate_auth_config
        for placeholder in PLACEHOLDER_TOKENS:
            self.assertGreaterEqual(len(placeholder),32,"a short placeholder is already caught by length")
            with mock.patch.dict(os.environ,{"DELIVERY_AUTH_MODE":"token","DELIVERY_API_TOKEN":placeholder}):
                with self.assertRaisesRegex(RuntimeError,"openssl rand"):
                    validate_auth_config()

    # The shipped example must not carry a value that would start the backend.
    def test_env_example_does_not_ship_a_usable_token(self):
        from pathlib import Path
        example=Path(__file__).resolve().parents[1]/"delivery"/".env.example"
        assigned=[line.split("=",1)[1].strip() for line in example.read_text(encoding="utf-8").splitlines()
                  if line.startswith("DELIVERY_API_TOKEN=")]
        self.assertEqual(assigned,[""],"delivery/.env.example must leave DELIVERY_API_TOKEN empty")

    def tearDown(self):
        self.auth_patch.stop()

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

    def test_get_document_with_structured_korean_summary(self):
        from crawler import db_pg
        conn=db_pg.open_db(TEST_PG_DSN)
        summary_id=conn.execute("""INSERT INTO document_summaries
          (seq_id,target_locale,source_fingerprint,model_version,prompt_version,state,
           summary_text,key_points,institutions,source_facts,completed_at)
          VALUES(%s,'ko-KR',%s,'qwen','title-summary-ko-v1','completed',%s,%s::jsonb,%s::jsonb,%s::jsonb,now())
          RETURNING summary_id""",
          (self.seq,"b"*64,"문서 핵심 요약.",'["핵심 사항"]','["Site One"]','{"urls":["https://s1/1"]}')).fetchone()["summary_id"]
        conn.execute("""INSERT INTO document_summary_quality
          (summary_id,gate_version,decision,score,reason_codes,checks,evidence)
          VALUES(%s,'summary-quality-v1','review_recommended',82,'["low_evidence"]','{"evidence_average":64}','[]')""",(summary_id,))
        conn.commit();conn.close()
        summary=self.client.get(f"/documents/{self.seq}").json()["generated_summary"]
        self.assertEqual(summary["summary_text"],"문서 핵심 요약.")
        self.assertEqual(summary["key_points"],["핵심 사항"])
        self.assertEqual(summary["source_facts"]["urls"],["https://s1/1"])
        self.assertEqual(summary["quality_decision"],"review_recommended")

    def test_translation_quality_dashboard_and_validation(self):
        from crawler import db_pg
        conn=db_pg.open_db(TEST_PG_DSN)
        conn.execute("INSERT INTO document_lang(seq_id,lang) VALUES(%s,'en')",(self.seq,))
        conn.execute("""INSERT INTO translation_jobs
          (seq_id,source_field,task_type,source_fingerprint,source_lang,target_locale,provider,
           model_version,prompt_version,status) VALUES
          (%s,'description','summarize',%s,'en','ko-KR','internal','qwen','v1','completed')""",
          (self.seq,"c"*64))
        sid=conn.execute("""INSERT INTO document_summaries
          (seq_id,target_locale,source_fingerprint,model_version,prompt_version,state,summary_text)
          VALUES(%s,'ko-KR',%s,'qwen','v1','completed','redacted') RETURNING summary_id""",
          (self.seq,"c"*64)).fetchone()["summary_id"]
        conn.execute("""INSERT INTO document_summary_quality
          (summary_id,gate_version,decision,score,reason_codes,checks,evidence)
          VALUES(%s,'summary-quality-v1','review_recommended',81,'["low_evidence"]',
                 '{"evidence_average":62}','[{"key_point_index":0,"source_sentence_index":1,"similarity":62}]')""",(sid,))
        conn.commit();conn.close()
        body=self.client.get("/translation/quality?decision=review_recommended").json()
        self.assertEqual(body["summary"]["total"],1)
        self.assertEqual(body["items"][0]["seq_id"],self.seq)
        self.assertNotIn("summary_text",body["items"][0])
        self.assertEqual(body["top_reasons"][0]["code"],"low_evidence")
        self.assertEqual(self.client.get("/translation/quality?decision=unknown").status_code,422)
        self.assertEqual(self.client.get("/translation/quality?limit=0").status_code,422)

    def test_translation_preview_enqueue_list_cancel(self):
        preview = self.client.post("/translation/preview", json={"tasks": ["title_translation"]})
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["total"], 1)
        created = self.client.post("/translation/jobs", json={
            "tasks": ["title_translation"], "provider": "internal", "model_version": "qwen-test",
            "limit": 10, "requested_by": "operator",
        })
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["created"], 1)
        self.assertIsInstance(created.json()["batch_id"],int)
        jid = created.json()["job_ids"][0]
        listed = self.client.get("/translation/jobs?status=pending").json()
        self.assertEqual(listed["summary"]["pending"], 1)
        self.assertEqual(listed["jobs"][0]["provider"], "internal")
        cancelled = self.client.post(f"/translation/jobs/{jid}/cancel")
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.assertEqual(self.client.post(f"/translation/jobs/{jid}/retry").status_code, 409)
        batches=self.client.get("/translation/batches").json()["batches"]
        self.assertEqual(batches[0]["created_count"],1)
        detail=self.client.get(f"/translation/batches/{created.json()['batch_id']}")
        self.assertEqual(detail.status_code,200)
        self.assertNotIn("source_text",str(detail.json()))
        self.assertEqual(self.client.get("/translation/batches/999999").status_code,404)

    def test_translation_api_validation_and_missing(self):
        self.assertEqual(self.client.post("/translation/preview", json={"tasks": []}).status_code, 422)
        self.assertEqual(self.client.post("/translation/jobs", json={
            "tasks": ["title_translation"], "provider": "external", "model_version": "", "limit": 1,
        }).status_code, 422)
        self.assertEqual(self.client.get("/translation/jobs?status=bogus").status_code, 422)
        self.assertEqual(self.client.post("/translation/jobs/999999/cancel").status_code, 404)
        self.assertEqual(self.client.post("/translation/jobs/999999/retry").status_code, 404)

    def test_operator_can_report_numeric_gpu0_observations(self):
        response=self.client.post("/translation/operations/observations",json={"endpoint_healthy":True,
          "model_version":"qwen","gpu_memory_free_bytes":2048,"gpu_utilization_percent":42,"disk_free_bytes":4096})
        self.assertEqual(response.status_code,200)
        body=self.client.get("/translation/operations?model_version=qwen").json()
        self.assertTrue(any(row["metric"]=="gpu_memory_free_bytes" for row in body["observations"]))
        self.assertEqual(self.client.post("/translation/operations/observations",json={"endpoint_healthy":True,"model_version":"qwen","gpu_utilization_percent":101}).status_code,422)
        self.assertEqual(self.client.post("/translation/operations/observations",json={"endpoint_healthy":True}).status_code,422)

    def test_operations_uses_configured_model_when_query_omits_it(self):
        response = self.client.post(
            "/translation/operations/observations",
            json={"endpoint_healthy": True, "model_version": "configured-qwen"},
        )
        self.assertEqual(response.status_code, 200)
        with mock.patch.dict(
            os.environ, {"TRANSLATION_INTERNAL_MODEL": "configured-qwen"}
        ):
            body = self.client.get("/translation/operations").json()
        self.assertTrue(
            any(row["metric"] == "endpoint_healthy" for row in body["observations"])
        )

    def test_observation_write_prunes_expired_metrics(self):
        from crawler import db_pg
        conn=db_pg.open_db(TEST_PG_DSN)
        conn.execute("""INSERT INTO translation_system_observations
          (worker_id,provider,model_version,metric,value_boolean,observed_at)
          VALUES('old','internal','qwen','endpoint_healthy',false,now()-interval '8 days')""")
        conn.commit();conn.close()
        response=self.client.post("/translation/operations/observations",json={
          "endpoint_healthy":True,"model_version":"qwen"})
        self.assertEqual(response.status_code,200)
        conn=db_pg.open_db(TEST_PG_DSN)
        count=conn.execute("SELECT count(*) AS n FROM translation_system_observations WHERE worker_id='old'").fetchone()["n"]
        conn.close();self.assertEqual(count,0)

    def test_post_and_get_job(self):
        r = self.client.post("/jobs", json={"site_id": "s1", "mode": "full", "limit_n": 3})
        self.assertEqual(r.status_code, 200)
        jid = r.json()["id"]
        r2 = self.client.get(f"/jobs/{jid}")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["site_id"], "s1")
        self.assertEqual(r2.json()["status"], "queued")
        self.assertEqual(r2.json()["logs"][0]["event"],"queued")

    def test_crawl_job_validates_site_mode_limit_and_active_duplicate(self):
        self.assertEqual(self.client.post("/jobs",json={"site_id":"missing"}).status_code,404)
        self.assertEqual(self.client.post("/jobs",json={"site_id":"s1","mode":"unsafe"}).status_code,422)
        self.assertEqual(self.client.post("/jobs",json={"site_id":"s1","limit_n":0}).status_code,422)
        first=self.client.post("/jobs",json={"site_id":"s1","mode":"incremental"})
        self.assertEqual(first.status_code,200)
        duplicate=self.client.post("/jobs",json={"site_id":"s1","mode":"full"})
        self.assertEqual(duplicate.status_code,409)

    def test_schedule_api(self):
        made=self.client.put("/schedules/s1",json={"interval_hours":24,"limit_n":20})
        self.assertEqual(made.status_code,200)
        self.assertEqual(made.json()["site_id"],"s1")
        self.assertEqual(len(self.client.get("/schedules").json()["schedules"]),1)
        self.assertEqual(self.client.put("/schedules/missing",json={"interval_hours":24}).status_code,404)

    def test_delete_schedule_requires_an_operator_token(self):
        with mock.patch.dict(os.environ, {"DELIVERY_AUTH_MODE": "token", "DELIVERY_API_TOKEN": "x" * 32}):
            response = self.client.delete("/schedules/whatever")
        self.assertEqual(response.status_code, 401)

    def test_delete_unknown_schedule_reports_404(self):
        self.assertEqual(self.client.delete("/schedules/never-scheduled").status_code, 404)

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

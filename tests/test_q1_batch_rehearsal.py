"""Opt-in isolated PostgreSQL rehearsal for Q1 batch safety."""
import contextlib
import io
import os
import unittest

from delivery.translation.providers import SummaryResult

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")


class _GroundedProvider:
    name="internal";model="q1-rehearsal-model";prompt_version="q1-rehearsal-v1";max_chars=8000
    def summarize(self,request):
        point=request.text.split(".",1)[0]+"."
        return SummaryResult(request.text,(point,),(),self.name,self.model,self.prompt_version,
          len(request.title)+len(request.text),len(request.text),25,120,55,"stop")


@unittest.skipUnless(TEST_PG_DSN,"set TEST_PG_DSN to run isolated Q1 rehearsal")
class Q1BatchRehearsalTest(unittest.TestCase):
    def setUp(self):
        from crawler import db_pg
        from delivery.db.schema import init_delivery_schema
        self.conn=db_pg.open_db(TEST_PG_DSN)
        for table in ("document_summary_quality","translation_job_attempts","translation_worker_heartbeats",
                      "translation_system_observations","translation_jobs","translation_batches",
                      "document_summaries","crawl_jobs","document_translations","document_lang","documents","sites"):
            self.conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        self.conn.commit();db_pg.init_db(self.conn)
        self.conn.execute("CREATE TABLE document_lang(seq_id BIGINT PRIMARY KEY REFERENCES documents(seq_id),lang TEXT NOT NULL)")
        self.conn.execute("""CREATE TABLE document_translations(translation_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          seq_id BIGINT REFERENCES documents(seq_id),source_field TEXT,target_locale TEXT,source_fingerprint TEXT,
          model_version TEXT,prompt_version TEXT,state TEXT,translation_text TEXT,attempts INT DEFAULT 0,
          last_error_code TEXT,created_at TIMESTAMPTZ DEFAULT now(),updated_at TIMESTAMPTZ DEFAULT now(),
          claimed_at TIMESTAMPTZ,completed_at TIMESTAMPTZ,
          UNIQUE(seq_id,source_field,target_locale,source_fingerprint,model_version,prompt_version))""")
        init_delivery_schema(self.conn);db_pg.upsert_site(self.conn,"q1-site","Q1 Site","https://q1.invalid")
        source=("정부는 지역 기후 정책 지원을 확대하고 현장 수요를 반영한다. "
                "연구기관은 정책 효과를 분석하고 결과를 공개한다. "
                "지원 대상은 지역 기업이며 전환 사업을 단계적으로 시행한다. "
                "세부 계획은 지역별 여건과 검증 결과를 반영해 계속 개선한다.")
        for index in range(201):
            seq=db_pg.insert_document(self.conn,{"site_id":"q1-site","post_number":str(index),
              "meta_url":f"https://q1.invalid/{index}","title":f"정책 문서 {index}","abstract":source})
            self.conn.execute("INSERT INTO document_lang VALUES(%s,'ko')",(seq,))
        self.conn.commit()

    def tearDown(self):self.conn.close()

    def test_100_job_baseline_and_breakers(self):
        from delivery.translation import jobs
        from delivery.translation.safety import evaluate_circuit
        made=jobs.enqueue_targets(self.conn,tasks=["abstract_summary"],provider="internal",
          model_version="q1-rehearsal-model",prompt_version="q1-rehearsal-v1",limit=100)
        self.assertEqual(made["created"],100)
        with contextlib.redirect_stdout(io.StringIO()) as logs:
            for _ in range(100):
                self.assertTrue(jobs.run_once(self.conn,lambda _name:_GroundedProvider(),worker_id="q1-worker"))
        events=logs.getvalue().splitlines()
        self.assertEqual(len(events),100)
        self.assertNotIn("정부는",logs.getvalue())
        attempts=self.conn.execute("""SELECT count(*) AS n,sum(prompt_tokens) AS prompt,sum(completion_tokens) AS completion,
          count(*) FILTER(WHERE finish_reason='stop') AS stopped FROM translation_job_attempts""").fetchone()
        self.assertEqual((attempts["n"],attempts["prompt"],attempts["completion"],attempts["stopped"]),(100,12000,5500,100))
        quality=self.conn.execute("SELECT decision,count(*) AS n FROM document_summary_quality GROUP BY decision").fetchone()
        self.assertEqual((quality["decision"],quality["n"]),("auto_approved",100))
        safety=evaluate_circuit(self.conn,provider="internal",model_version="q1-rehearsal-model",
          prompt_version="q1-rehearsal-v1",requested_limit=101)
        self.assertFalse(safety["open"]);self.assertEqual(safety["baseline_sample"],100)

        self.conn.execute("""INSERT INTO translation_system_observations(worker_id,metric,value_boolean)
          VALUES('q1-worker','endpoint_healthy',false)""");self.conn.commit()
        stopped=evaluate_circuit(self.conn,provider="internal",model_version="q1-rehearsal-model",
          prompt_version="q1-rehearsal-v1",requested_limit=100)
        self.assertTrue(stopped["open"]);self.assertIn("endpoint_unhealthy",stopped["reason_codes"])

        self.conn.execute("DELETE FROM translation_system_observations WHERE metric='endpoint_healthy'")
        self.conn.execute("UPDATE translation_job_attempts SET latency_ms=61001 WHERE id IN (SELECT id FROM translation_job_attempts ORDER BY id DESC LIMIT 10)")
        self.conn.commit()
        slow=evaluate_circuit(self.conn,provider="internal",model_version="q1-rehearsal-model",
          prompt_version="q1-rehearsal-v1",requested_limit=100)
        self.assertTrue(slow["open"]);self.assertIn("provider_latency_p95",slow["reason_codes"])

    def test_failure_rate_and_stale_lease(self):
        from delivery.translation import jobs
        from delivery.translation.safety import evaluate_circuit
        seqs=[row["seq_id"] for row in self.conn.execute("SELECT seq_id FROM documents ORDER BY seq_id LIMIT 10").fetchall()]
        batch=self.conn.execute("""INSERT INTO translation_batches
          (provider,model_version,prompt_version,tasks,requested_limit,target_count,created_count)
          VALUES('internal','q1-failure-model','q1-failure-v1','["abstract_summary"]',10,10,10)
          RETURNING id""").fetchone()["id"]
        for index,seq in enumerate(seqs):
            job=self.conn.execute("""INSERT INTO translation_jobs
              (seq_id,source_field,task_type,source_fingerprint,source_lang,provider,model_version,
               prompt_version,status,attempts,batch_id) VALUES
              (%s,'description','summarize',%s,'ko','internal','q1-failure-model','q1-failure-v1',%s,1,%s)
              RETURNING id""",(seq,f"{index:064x}","failed" if index<2 else "completed",batch)).fetchone()["id"]
            self.conn.execute("""INSERT INTO translation_job_attempts
              (job_id,batch_id,attempt_no,worker_id,status,error_code,latency_ms,finished_at)
              VALUES(%s,%s,1,'q1-worker',%s,%s,100,now())""",
              (job,batch,"failed" if index<2 else "completed","timeout" if index<2 else None))
        self.conn.commit()
        safety=evaluate_circuit(self.conn,provider="internal",model_version="q1-failure-model",
          prompt_version="q1-failure-v1",requested_limit=10)
        self.assertTrue(safety["open"]);self.assertIn("provider_failure_rate",safety["reason_codes"])

        made=jobs.enqueue_targets(self.conn,tasks=["abstract_summary"],provider="internal",
          model_version="q1-lease-model",prompt_version="q1-lease-v1",limit=1)
        claimed=jobs.claim_next(self.conn,worker_id="q1-expired-worker",lease_seconds=1)
        self.conn.execute("UPDATE translation_jobs SET lease_expires_at=now()-interval '1 second' WHERE id=%s",(claimed["id"],));self.conn.commit()
        self.assertEqual(jobs.recover_stale(self.conn),1)
        recovered=self.conn.execute("SELECT status,error_code FROM translation_jobs WHERE id=%s",(claimed["id"],)).fetchone()
        attempt=self.conn.execute("SELECT status,error_code FROM translation_job_attempts WHERE id=%s",(claimed["attempt_id"],)).fetchone()
        self.assertEqual(made["created"],1)
        self.assertEqual((recovered["status"],recovered["error_code"]),("pending","worker_lease_expired"))
        self.assertEqual((attempt["status"],attempt["error_code"]),("stale_lease","worker_lease_expired"))


if __name__=="__main__":unittest.main()

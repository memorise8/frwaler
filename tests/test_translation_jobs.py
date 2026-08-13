import os
import unittest

from delivery.translation.providers import ProviderError, SummaryResult, TranslationResult

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")


class _Provider:
    name="internal"; model="test-model"; prompt_version="translate-ko-v1"; max_chars=8000
    def translate(self, request):
        return TranslationResult("번역: " + request.text, self.name, self.model,
                                 self.prompt_version, len(request.text), len(request.text)+4, 12)
    def summarize(self, request):
        return SummaryResult("정책 핵심 요약.",( "핵심 수치 확인",),("Site",),self.name,self.model,
                             self.prompt_version,len(request.title)+len(request.text),20,15)


class _FailProvider(_Provider):
    def __init__(self, retryable=True): self.retryable=retryable
    def translate(self, request):
        raise ProviderError("timeout", "timed out", retryable=self.retryable)


@unittest.skipUnless(TEST_PG_DSN, "set TEST_PG_DSN to run")
class TranslationJobsTest(unittest.TestCase):
    def setUp(self):
        from crawler import db_pg
        from delivery.db.schema import init_delivery_schema
        self.conn=db_pg.open_db(TEST_PG_DSN)
        for table in ("document_summary_quality","translation_job_attempts","translation_worker_heartbeats","translation_system_observations","translation_jobs","translation_batches","document_summaries","crawl_jobs","document_translations","document_lang","documents","sites"):
            self.conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        self.conn.commit(); db_pg.init_db(self.conn)
        self.conn.execute("CREATE TABLE document_lang(seq_id BIGINT PRIMARY KEY REFERENCES documents(seq_id),lang TEXT NOT NULL)")
        self.conn.execute("""CREATE TABLE document_translations(
          translation_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          seq_id BIGINT NOT NULL REFERENCES documents(seq_id), source_field TEXT NOT NULL,
          target_locale TEXT NOT NULL,source_fingerprint TEXT NOT NULL,model_version TEXT NOT NULL,
          prompt_version TEXT NOT NULL,state TEXT NOT NULL,translation_text TEXT,attempts INT DEFAULT 0,
          last_error_code TEXT,created_at TIMESTAMPTZ DEFAULT now(),updated_at TIMESTAMPTZ DEFAULT now(),
          claimed_at TIMESTAMPTZ,completed_at TIMESTAMPTZ,
          UNIQUE(seq_id,source_field,target_locale,source_fingerprint,model_version,prompt_version))""")
        db_pg.upsert_site(self.conn,"s1","Site","https://s.test")
        self.seq=db_pg.insert_document(self.conn,{"site_id":"s1","post_number":"1","meta_url":"https://s/1","title":"Budget 2025","abstract":"Long policy abstract"})
        self.conn.execute("INSERT INTO document_lang VALUES(%s,'en')",(self.seq,)); self.conn.commit()
        init_delivery_schema(self.conn)
    def tearDown(self): self.conn.close()

    def _enqueue(self, **extra):
        from delivery.translation.jobs import enqueue_targets
        args=dict(fields=["title"],provider="internal",model_version="test-model",
                  prompt_version="translate-ko-v1",limit=10)
        args.update(extra); return enqueue_targets(self.conn,**args)

    def test_preview_enqueue_idempotent_and_complete(self):
        from delivery.translation import jobs
        self.assertEqual(jobs.preview_targets(self.conn,fields=["title","description"],lang="en")["total"],2)
        made=self._enqueue(); self.assertEqual(made["created"],1)
        again=self._enqueue(); self.assertEqual(again["created"],0)
        claimed=jobs.claim_next(self.conn); self.assertTrue(jobs.run_job(self.conn,claimed,_Provider()))
        row=self.conn.execute("SELECT status,input_chars,latency_ms FROM translation_jobs").fetchone()
        attempt=self.conn.execute("SELECT status,attempt_no,worker_id,latency_ms FROM translation_job_attempts").fetchone()
        batch=self.conn.execute("SELECT status,created_count FROM translation_batches WHERE created_count=1").fetchone()
        saved=self.conn.execute("SELECT state,translation_text FROM document_translations").fetchone()
        self.assertEqual(row["status"],"completed"); self.assertEqual(row["latency_ms"],12)
        self.assertEqual(saved["translation_text"],"번역: Budget 2025")
        self.assertEqual((attempt["status"],attempt["attempt_no"],attempt["worker_id"]),("completed",1,"worker-default"))
        self.assertEqual((batch["status"],batch["created_count"]),("completed",1))

    def test_circuit_breaker_blocks_large_batch_without_baseline(self):
        from delivery.translation import jobs
        with self.assertRaisesRegex(RuntimeError,"circuit_open:insufficient_baseline"):
            jobs.enqueue_targets(self.conn,tasks=["title_translation"],provider="internal",
              model_version="new-model",prompt_version="v1",limit=101)
        self.assertEqual(self.conn.execute("SELECT count(*) AS n FROM translation_batches").fetchone()["n"],0)

    def test_stale_lease_is_recorded_and_requeued(self):
        from delivery.translation import jobs
        self._enqueue();claimed=jobs.claim_next(self.conn,worker_id="worker-q1",lease_seconds=1)
        self.conn.execute("UPDATE translation_jobs SET lease_expires_at=now()-interval '1 second' WHERE id=%s",(claimed["id"],));self.conn.commit()
        self.assertEqual(jobs.recover_stale(self.conn),1)
        job=self.conn.execute("SELECT status,error_code FROM translation_jobs WHERE id=%s",(claimed["id"],)).fetchone()
        attempt=self.conn.execute("SELECT status,error_code FROM translation_job_attempts WHERE id=%s",(claimed["attempt_id"],)).fetchone()
        self.assertEqual((job["status"],attempt["status"]),("pending","stale_lease"))
        self.assertEqual(attempt["error_code"],"worker_lease_expired")

    def test_source_change_is_skipped(self):
        from delivery.translation import jobs
        self._enqueue(); claimed=jobs.claim_next(self.conn)
        self.conn.execute("UPDATE documents SET title='Changed' WHERE seq_id=%s",(self.seq,)); self.conn.commit()
        self.assertFalse(jobs.run_job(self.conn,claimed,_Provider()))
        self.assertEqual(self.conn.execute("SELECT status FROM translation_jobs").fetchone()["status"],"skipped")

    def test_retryable_failure_and_manual_retry(self):
        from delivery.translation import jobs
        self._enqueue(); claimed=jobs.claim_next(self.conn)
        self.assertFalse(jobs.run_job(self.conn,claimed,_FailProvider()))
        row=self.conn.execute("SELECT status,error_code FROM translation_jobs").fetchone()
        self.assertEqual((row["status"],row["error_code"]),("pending","timeout"))
        self.conn.execute("UPDATE translation_jobs SET status='failed' WHERE id=%s",(claimed["id"],)); self.conn.commit()
        self.assertEqual(jobs.retry(self.conn,claimed["id"])["status"],"pending")

    def test_cancel_and_claim_is_atomic(self):
        from delivery.translation import jobs
        ids=self._enqueue()["job_ids"]
        self.assertEqual(jobs.cancel(self.conn,ids[0])["status"],"cancelled")
        self.assertIsNone(jobs.claim_next(self.conn))

    def test_long_text_is_split_without_loss(self):
        from delivery.translation import jobs
        self.conn.execute("UPDATE documents SET title=%s WHERE seq_id=%s", ("A" * 12, self.seq)); self.conn.commit()
        self._enqueue(); claimed=jobs.claim_next(self.conn)
        provider=_Provider(); provider.max_chars=5
        self.assertTrue(jobs.run_job(self.conn,claimed,provider))
        saved=self.conn.execute("SELECT translation_text FROM document_translations").fetchone()["translation_text"]
        self.assertEqual(saved,"번역: AAAAA번역: AAAAA번역: AA")

    def test_missing_url_or_number_retries_as_invalid_response(self):
        from delivery.translation import jobs
        self.conn.execute("UPDATE documents SET title='Budget 2025 https://example.test' WHERE seq_id=%s",(self.seq,));self.conn.commit()
        self._enqueue(); claimed=jobs.claim_next(self.conn)
        class LosesTokens(_Provider):
            def translate(self,request): return TranslationResult("예산",self.name,self.model,self.prompt_version,len(request.text),2,1)
        self.assertFalse(jobs.run_job(self.conn,claimed,LosesTokens()))
        row=self.conn.execute("SELECT status,error_code FROM translation_jobs").fetchone()
        self.assertEqual((row["status"],row["error_code"]),("pending","invalid_response"))

    def test_abstract_summary_is_structured_and_keeps_source_facts(self):
        from delivery.translation import jobs
        made=jobs.enqueue_targets(self.conn,tasks=["abstract_summary"],provider="internal",
            model_version="test-model",prompt_version="title-summary-ko-v1",limit=1)
        self.assertEqual(made["created"],1)
        claimed=jobs.claim_next(self.conn); self.assertEqual(claimed["task_type"],"summarize")
        provider=_Provider();provider.prompt_version="title-summary-ko-v1"
        self.assertTrue(jobs.run_job(self.conn,claimed,provider))
        saved=self.conn.execute("""SELECT s.summary_text,s.key_points,s.institutions,s.source_facts,
            q.decision,q.reason_codes,q.evidence FROM document_summaries s
            JOIN document_summary_quality q USING(summary_id)""").fetchone()
        self.assertEqual(saved["summary_text"],"정책 핵심 요약.")
        self.assertEqual(saved["key_points"],["핵심 수치 확인"])
        self.assertIn("https://s/1",saved["source_facts"]["urls"])
        self.assertEqual(saved["decision"],"rejected")
        self.assertIn("invalid_sentence_count",saved["reason_codes"])
        self.assertNotIn("source_text",saved["evidence"])

    def test_provider_provenance_mismatch_fails_without_saving_result(self):
        from delivery.translation import jobs
        self._enqueue(); claimed=jobs.claim_next(self.conn)
        provider=_Provider();provider.model="different-model"
        self.assertFalse(jobs.run_job(self.conn,claimed,provider))
        row=self.conn.execute("SELECT status,error_code FROM translation_jobs WHERE id=%s",(claimed["id"],)).fetchone()
        self.assertEqual((row["status"],row["error_code"]),("failed","configuration"))
        self.assertEqual(self.conn.execute("SELECT count(*) AS n FROM document_translations").fetchone()["n"],0)


if __name__ == "__main__": unittest.main()

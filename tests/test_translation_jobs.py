import os
import unittest

from delivery.translation.providers import ProviderError, TranslationResult

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")


class _Provider:
    name="internal"; model="test-model"; prompt_version="translate-ko-v1"; max_chars=8000
    def translate(self, request):
        return TranslationResult("번역: " + request.text, self.name, self.model,
                                 self.prompt_version, len(request.text), len(request.text)+4, 12)


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
        for table in ("translation_jobs","crawl_jobs","document_translations","document_lang","documents","sites"):
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
        saved=self.conn.execute("SELECT state,translation_text FROM document_translations").fetchone()
        self.assertEqual(row["status"],"completed"); self.assertEqual(row["latency_ms"],12)
        self.assertEqual(saved["translation_text"],"번역: Budget 2025")

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


if __name__ == "__main__": unittest.main()

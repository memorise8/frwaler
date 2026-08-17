# -*- coding: utf-8 -*-
"""The worker runs one translation lane and one crawl lane on a single thread.

Before run_cycle existed the crawl lane was reached only when the translation
lane came back empty. That reads like a priority order but behaves as a
starvation bug: a translation queue that keeps producing work means crawling
never happens, so an 804-site sweep can sit untouched for hours while the
console shows nothing but "대기 중".
"""
import os
import unittest

from delivery.translation.providers import TranslationResult

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")


class _Provider:
    name = "internal"; model = "test-model"; prompt_version = "translate-ko-v1"; max_chars = 8000

    def translate(self, request):
        return TranslationResult("번역: " + request.text, self.name, self.model,
                                 self.prompt_version, len(request.text), len(request.text) + 4, 12)


def _provider_factory(name, model_version=None, prompt_version=None):
    return _Provider()


class _CountingCrawler:
    """Records that the crawl lane actually got a turn."""
    runs = []
    site_id = "s1"
    site_name = "Site"
    base_url = "https://s.test"

    def __init__(self, db_conn, delay=1.0):
        self._conn = db_conn

    def crawl(self, limit=None):
        _CountingCrawler.runs.append(limit)


@unittest.skipUnless(TEST_PG_DSN, "set TEST_PG_DSN to run")
class WorkerCycleTest(unittest.TestCase):
    def setUp(self):
        from crawler import db_pg
        from delivery.db.schema import init_delivery_schema
        os.environ["LIBERTREE_DB_BACKEND"] = "postgres"
        _CountingCrawler.runs = []
        self.conn = db_pg.open_db(TEST_PG_DSN)
        self.conn.execute("DROP SCHEMA public CASCADE")
        self.conn.execute("CREATE SCHEMA public")
        self.conn.commit()
        db_pg.init_db(self.conn)
        self.conn.execute(
            "CREATE TABLE document_lang(seq_id BIGINT PRIMARY KEY REFERENCES documents(seq_id),lang TEXT NOT NULL)")
        self.conn.execute("""CREATE TABLE document_translations(
          translation_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          seq_id BIGINT NOT NULL REFERENCES documents(seq_id), source_field TEXT NOT NULL,
          target_locale TEXT NOT NULL,source_fingerprint TEXT NOT NULL,model_version TEXT NOT NULL,
          prompt_version TEXT NOT NULL,state TEXT NOT NULL,translation_text TEXT,attempts INT DEFAULT 0,
          last_error_code TEXT,created_at TIMESTAMPTZ DEFAULT now(),updated_at TIMESTAMPTZ DEFAULT now(),
          claimed_at TIMESTAMPTZ,completed_at TIMESTAMPTZ,
          UNIQUE(seq_id,source_field,target_locale,source_fingerprint,model_version,prompt_version))""")
        db_pg.upsert_site(self.conn, "s1", "Site", "https://s.test")
        for index in range(3):
            seq = db_pg.insert_document(self.conn, {
                "site_id": "s1", "post_number": str(index), "meta_url": f"https://s/{index}",
                "title": f"Budget {index}", "abstract": "Long policy abstract"})
            self.conn.execute("INSERT INTO document_lang VALUES(%s,'en')", (seq,))
        self.conn.commit()
        init_delivery_schema(self.conn)

    def tearDown(self):
        os.environ.pop("LIBERTREE_DB_BACKEND", None)
        self.conn.close()

    def _queue_translations(self):
        from delivery.translation.jobs import enqueue_targets
        return enqueue_targets(self.conn, fields=["title"], provider="internal",
                               model_version="test-model", prompt_version="translate-ko-v1", limit=10)

    def _queue_crawl(self):
        from delivery.worker import jobs
        return jobs.enqueue_job(self.conn, "s1", limit_n=7)

    def _cycle(self):
        from delivery.worker import worker
        return worker.run_cycle(self.conn, worker_id="test-worker", delay=0,
                                crawler_registry={"s1": _CountingCrawler},
                                translation_provider=_provider_factory)

    # The regression itself: a translation waiting must not consume the cycle.
    def test_a_pending_translation_does_not_block_the_crawl_lane(self):
        self._queue_translations()
        job_id = self._queue_crawl()
        translated, crawled = self._cycle()
        self.assertTrue(translated)
        self.assertTrue(crawled)
        self.assertEqual(_CountingCrawler.runs, [7])
        status = self.conn.execute(
            "SELECT status FROM crawl_jobs WHERE id=%s", (job_id,)).fetchone()["status"]
        self.assertEqual(status, "done")

    # A deep translation queue is the case that used to starve crawling
    # outright: every cycle had translation work, so the crawl never ran.
    def test_crawling_advances_every_cycle_while_translations_remain(self):
        self._queue_translations()
        for _ in range(3):
            self._queue_crawl()
            translated, crawled = self._cycle()
            self.assertTrue(crawled)
        self.assertEqual(len(_CountingCrawler.runs), 3)
        remaining = self.conn.execute(
            "SELECT count(*) AS n FROM crawl_jobs WHERE status<>'done'").fetchone()["n"]
        self.assertEqual(remaining, 0)

    def test_crawl_alone_still_runs_when_no_translation_is_waiting(self):
        self._queue_crawl()
        translated, crawled = self._cycle()
        self.assertFalse(translated)
        self.assertTrue(crawled)

    def test_translation_alone_still_runs_when_no_crawl_is_waiting(self):
        self._queue_translations()
        translated, crawled = self._cycle()
        self.assertTrue(translated)
        self.assertFalse(crawled)

    # main() sleeps only on a cycle that did nothing, so an idle cycle has to
    # be distinguishable from a busy one.
    def test_an_idle_cycle_reports_both_lanes_empty(self):
        self.assertEqual(self._cycle(), (False, False))


if __name__ == "__main__":
    unittest.main()

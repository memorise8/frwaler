# -*- coding: utf-8 -*-
import io
import os
import unittest

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

CATALOGUE = (
    "site_id,site_name,status,category,reason,collected\n"
    "alpha-site,알파 기관,된다,,,12\n"
    "beta-site,베타 기관,안된다,IP차단,\"우리 IP 차단(403/WAF)\",0\n"
    "no-crawler-site,크롤러 없는 항목,된다,,,0\n"
)


class _Crawler:
    def __init__(self, site_id, site_name, base_url):
        self.site_id = site_id
        self.site_name = site_name
        self.base_url = base_url


REGISTRY = {
    "alpha-site": _Crawler("alpha-site", "Alpha", "https://alpha.example"),
    "beta-site": _Crawler("beta-site", "Beta", "https://beta.example"),
    "extra-site": _Crawler("extra-site", "Extra", "https://extra.example"),
}


@unittest.skipUnless(TEST_PG_DSN, "set TEST_PG_DSN to run")
class SeedCatalogueSitesTest(unittest.TestCase):
    def setUp(self):
        from crawler import db_pg
        from delivery.db import schema
        self.conn = db_pg.open_db(TEST_PG_DSN)
        self.conn.execute("DROP SCHEMA public CASCADE")
        self.conn.execute("CREATE SCHEMA public")
        self.conn.commit()
        db_pg.init_db(self.conn)
        schema.init_delivery_schema(self.conn)
        self.catalogue = os.path.join(os.path.dirname(__file__), "_seed_catalogue.csv")
        with io.open(self.catalogue, "w", encoding="utf-8") as handle:
            handle.write(CATALOGUE)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.catalogue):
            os.remove(self.catalogue)

    def _seed(self):
        from delivery.db.seed import seed_catalogue_sites
        return seed_catalogue_sites(self.conn, catalogue_path=self.catalogue, registry=REGISTRY)

    def _sites(self):
        return {row["site_id"]: dict(row) for row in
                self.conn.execute("SELECT site_id, site_name, site_url FROM sites").fetchall()}

    def test_registers_every_catalogue_crawler(self):
        self.assertEqual(self._seed(), 2)
        sites = self._sites()
        self.assertEqual(set(sites), {"alpha-site", "beta-site"})
        self.assertEqual(sites["alpha-site"]["site_url"], "https://alpha.example")

    def test_registers_crawlers_the_audit_marked_broken(self):
        # An IP-blocked crawler is exactly the one a customer needs to re-run
        # from their own network, so it must be registered like any other.
        self._seed()
        self.assertIn("beta-site", self._sites())

    def test_uses_the_catalogue_name_the_console_displays(self):
        self._seed()
        self.assertEqual(self._sites()["alpha-site"]["site_name"], "알파 기관")

    # The registry carries 1,242 keys against the catalogue's 804. Seeding the
    # extras would make /stats report more data sources than the crawler screen
    # lists and fill the "수집기 미등록" notice on day one.
    def test_does_not_register_crawlers_outside_the_catalogue(self):
        self._seed()
        self.assertNotIn("extra-site", self._sites())

    def test_skips_a_catalogue_entry_with_no_crawler_behind_it(self):
        self._seed()
        self.assertNotIn("no-crawler-site", self._sites())

    # Migration runs against live deployments; it must never rewrite a row a
    # crawler already produced, and never touch documents.
    def test_is_additive_and_leaves_existing_rows_untouched(self):
        from crawler import db_pg
        db_pg.upsert_site(self.conn, "alpha-site", "운영 중 이름", "https://existing.example")
        self.conn.commit()
        self.assertEqual(self._seed(), 1)
        sites = self._sites()
        self.assertEqual(sites["alpha-site"]["site_name"], "운영 중 이름")
        self.assertEqual(sites["alpha-site"]["site_url"], "https://existing.example")

    def test_is_idempotent(self):
        self.assertEqual(self._seed(), 2)
        self.assertEqual(self._seed(), 0)
        self.assertEqual(len(self._sites()), 2)

    def test_writes_no_documents(self):
        self._seed()
        count = self.conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"]
        self.assertEqual(count, 0)

    def test_missing_catalogue_file_is_not_an_error(self):
        from delivery.db.seed import seed_catalogue_sites
        self.assertEqual(
            seed_catalogue_sites(self.conn, catalogue_path="/nonexistent/catalogue.csv", registry=REGISTRY), 0)
        self.assertEqual(self._sites(), {})

    def test_seed_backfill_estimates_upserts_only_100k_sites(self):
        from delivery.scripts.seed_backfill_estimates import seed
        n = seed(self.conn, csv_path="scripts/audit/capacity_corrected.csv")
        self.assertEqual(n, 25)
        row = self.conn.execute(
            "SELECT total_estimate FROM crawl_site_progress WHERE site_id=%s",
            ("doaj-org-search",)).fetchone()
        self.assertEqual(row["total_estimate"], 13373055)
        # 재실행해도 행이 늘지 않는다
        self.assertEqual(seed(self.conn, csv_path="scripts/audit/capacity_corrected.csv"), 25)

    def test_seed_never_clobbers_existing_backfill_progress(self):
        # 시드의 가장 위험한 실수는 진행 중인 백필의 커서를 덮어쓰는 것이다 —
        # ON CONFLICT 의 SET 절이 total_estimate/updated_at 만 만져야 한다.
        from delivery.scripts.seed_backfill_estimates import seed
        from delivery.worker import jobs
        jobs.save_progress(self.conn, "doaj-org-search", {"page": 4211}, items_delta=210550)
        seed(self.conn, csv_path="scripts/audit/capacity_corrected.csv")
        row = self.conn.execute(
            "SELECT cursor, items_done, total_estimate, completed_at"
            " FROM crawl_site_progress WHERE site_id=%s",
            ("doaj-org-search",)).fetchone()
        self.assertEqual(row["cursor"], {"page": 4211})
        self.assertEqual(row["items_done"], 210550)
        self.assertEqual(row["total_estimate"], 13373055)
        self.assertIsNone(row["completed_at"])

    def test_migration_registers_the_real_catalogue(self):
        # End to end against the shipped CSV and the real crawler registry:
        # this is what makes POST /jobs reachable on a new install.
        from delivery.be.migrate import migrate
        migrate(TEST_PG_DSN)
        count = self.conn.execute("SELECT count(*) AS n FROM sites").fetchone()["n"]
        self.assertGreater(count, 700)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM sites WHERE site_id='bfr-bund-de-en'").fetchone())


if __name__ == "__main__":
    unittest.main()

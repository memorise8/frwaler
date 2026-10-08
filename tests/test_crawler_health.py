import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from delivery.health.results import atomic_json, read_result, site_path
from delivery.health.probe import child_environment, run_probe, sample_metadata


class HealthRecordsTest(unittest.TestCase):
    def test_registry_scope_includes_additional_sites_without_changing_default(self):
        from delivery.health.inventory import inventory
        from crawler.sites import CRAWLERS
        delivered=inventory()
        full=inventory(include_registry=True)
        delivered_ids={r['site_id'] for r in delivered['sites']}
        self.assertEqual({r['site_id'] for r in full['sites']},delivered_ids|set(CRAWLERS))
        self.assertGreater(len(full['sites']),len(delivered['sites']))
        self.assertEqual(sum(r['in_delivery_catalogue'] for r in full['sites']),len(delivered['sites']))
        self.assertTrue(all(r['source_hash'] and r['code_status']=='ready' for r in full['sites']))

    def test_resume_requires_same_profile_source_and_valid_attempt(self):
        from delivery.health.runner import reusable_result
        site={'site_id':'fixture','source_hash':'code-v1'}
        profile={'id':'frozen-environment-1','limit':3,'timeout':60,'delay':1}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            record={**site,'probe_profile':profile,'state':'blocked','checked_at':datetime.now(timezone.utc).isoformat()}
            atomic_json(site_path(root,'fixture'),record)
            self.assertEqual(reusable_result(root,site,profile)['state'],'blocked')
            self.assertIsNone(reusable_result(root,{**site,'source_hash':'code-v2'},profile))
            self.assertIsNone(reusable_result(root,site,{**profile,'id':'different-credentials'}))
            self.assertIsNone(reusable_result(root,site,{**profile,'limit':6}))
            site_path(root,'fixture').write_text('{')
            self.assertIsNone(reusable_result(root,site,profile))

    def test_entire_catalogue_loads_without_hidden_discovery_errors(self):
        from delivery.health.inventory import inventory
        report = inventory()
        self.assertEqual(report["registry_errors"], [])
        self.assertGreater(report["summary"]["catalogue"], 800)
        self.assertTrue(all(site["code_status"] == "ready" for site in report["sites"]))

    def test_latest_failure_is_not_masked_and_stale_or_changed_code_is_not_healthy(self):
        site = {"site_id": "fixture", "source_hash": "new"}
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(read_result(root, site)["state"], "unchecked")
            record = {"site_id": "fixture", "state": "healthy", "checked_at": now.isoformat(), "source_hash": "new"}
            atomic_json(site_path(root, "fixture"), record)
            self.assertEqual(read_result(root, site)["state"], "healthy")
            record["state"] = "blocked"
            atomic_json(site_path(root, "fixture"), record)
            self.assertEqual(read_result(root, site)["state"], "blocked")
            record.update(state="healthy", checked_at=(now - timedelta(days=3)).isoformat())
            atomic_json(site_path(root, "fixture"), record)
            self.assertEqual(read_result(root, site)["state"], "stale")
            record.update(checked_at=now.isoformat(), source_hash="old")
            atomic_json(site_path(root, "fixture"), record)
            self.assertEqual(read_result(root, site)["state"], "code_changed")

    def test_bad_report_and_path_traversal_do_not_become_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            site_path(root, "fixture").write_text('{"state": "healthy"}')
            self.assertEqual(read_result(root, {"site_id": "fixture"})["state"], "report_error")
            with self.assertRaises(ValueError):
                site_path(root, "../outside")

    def test_child_does_not_inherit_production_credentials(self):
        with patch.dict(os.environ, {"LIBERTREE_PG_DSN": "secret", "DELIVERY_API_TOKEN": "secret", "GITHUB_TOKEN": "secret"}):
            env = child_environment(Path("/tmp/probe-fixture"), 10)
        self.assertNotIn("LIBERTREE_PG_DSN", env)
        self.assertNotIn("DELIVERY_API_TOKEN", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertGreater(int(env["LIBERTREE_MAX_WALL_S"]), 60)
        self.assertIn("PLAYWRIGHT_BROWSERS_PATH", env)


class MetadataProbeTest(unittest.TestCase):
    def probe(self, action):
        from crawler.base_crawler import BaseCrawler

        class Fixture(BaseCrawler):
            site_id = "fixture"
            site_name = "Fixture"
            base_url = "https://fixture.invalid"

            def crawl(self, limit=None):
                action(self)

        with tempfile.TemporaryDirectory() as directory:
            return sample_metadata(Fixture, limit=2, delay=0.1, progress=Path(directory) / "progress.json")

    def test_valid_sample_does_not_reach_database_or_save_content_in_report(self):
        def action(crawler):
            for i in range(100):
                crawler._save_paper({"title": "private sample", "url": f"https://fixture.invalid/{i}"})
        result = self.probe(action)
        self.assertEqual(result["state"], "healthy")
        self.assertEqual(result["sample_count"], 2)
        self.assertNotIn("private sample", json.dumps(result))

    def test_empty_and_invalid_rows_are_not_success(self):
        self.assertEqual(self.probe(lambda c: None)["state"], "empty")
        result = self.probe(lambda c: c._save_paper({"title": "", "url": "bad"}))
        self.assertEqual(result["state"], "invalid_data")

    def test_explicit_crawler_failure_is_not_empty_or_healthy(self):
        self.assertEqual(self.probe(lambda c: c._report_source_failure("fetch_failed"))["state"], "network_error")
        self.assertEqual(self.probe(lambda c: c._report_source_failure("invalid_response"))["state"], "invalid_data")
        def partial(crawler):
            crawler._save_paper({"title": "Sample", "url": "https://fixture.invalid/1"})
            crawler._report_source_failure("fetch_failed")
        self.assertEqual(self.probe(partial)["state"], "partial")

    def test_swallowed_http_block_still_becomes_blocked(self):
        import requests
        response = requests.Response()
        response.status_code = 403

        def action(crawler):
            try:
                crawler._session.get("https://fixture.invalid").raise_for_status()
            except requests.RequestException:
                pass
        with patch.object(requests.Session, "request", return_value=response):
            self.assertEqual(self.probe(action)["state"], "blocked")

    def test_nonstandard_storage_is_unsupported_not_silently_successful(self):
        from crawler import blob_storage
        self.assertEqual(self.probe(lambda c: blob_storage.save_pdf(1, b"content"))["state"], "unsupported")

    def test_swallowed_direct_database_write_is_reported_as_unsupported(self):
        import sqlite3

        def action(crawler):
            try:
                crawler._conn.execute("DELETE FROM documents")
            except sqlite3.DatabaseError:
                pass
        self.assertEqual(self.probe(action)["state"], "unsupported")

    def test_timeout_is_hard_even_if_crawler_ignores_its_limit(self):
        original = subprocess.Popen
        with patch("delivery.health.probe.subprocess.Popen", side_effect=lambda *a, **kw:
                   original([sys.executable, "-c", "import time; time.sleep(30)"], **kw)):
            start = time.monotonic()
            result = run_probe({"site_id": "fixture", "source_hash": "v1", "code_status": "ready"}, timeout=1)
        self.assertEqual(result["state"], "timeout")
        self.assertLess(time.monotonic() - start, 5)


TEST_PG_DSN = os.environ.get("TEST_PG_DSN")


@unittest.skipUnless(TEST_PG_DSN, "requires disposable PostgreSQL")
class HealthIntegrationTest(unittest.TestCase):
    def setUp(self):
        from crawler import db_pg
        from delivery.be.migrate import migrate
        self.conn = db_pg.open_db(TEST_PG_DSN)
        self.conn.execute("DROP SCHEMA public CASCADE")
        self.conn.execute("CREATE SCHEMA public")
        self.conn.commit()
        migrate(TEST_PG_DSN)

    def tearDown(self):
        self.conn.close()

    def test_old_schema_and_missing_blob_are_reported_without_repairing_them(self):
        from delivery.health.overview import system_overview
        from crawler import db_pg
        seq = db_pg.insert_document(self.conn, {"site_id": "doaj-org-search", "meta_url": "https://fixture.invalid", "title": "Fixture"})
        db_pg.update_document_pdf(self.conn, seq, downloaded=True, size_bytes=10, sha256="a" * 64)
        self.conn.execute("DROP TABLE crawl_site_progress")
        self.conn.commit()
        with tempfile.TemporaryDirectory() as directory:
            report = system_overview(self.conn, blob_root=Path(directory))
        self.assertEqual(report["schema"], "migration_required")
        self.assertEqual(report["blob_sample"]["missing"], 1)
        self.assertIsNone(self.conn.execute("SELECT to_regclass('public.crawl_site_progress') AS t").fetchone()["t"])

    def test_idle_and_busy_workers_are_checked_independently(self):
        from delivery.health.overview import worker_rows
        self.conn.execute("INSERT INTO translation_worker_heartbeats(worker_id,status,last_seen_at) VALUES ('idle','idle',now()),('busy','idle',now()-interval '1 hour'),('dead','idle',now()-interval '1 hour')")
        self.conn.execute("INSERT INTO crawl_jobs(site_id,status,worker_id,lease_expires_at,started_at) VALUES ('fixture','running','busy',now()+interval '1 minute',now())")
        self.conn.commit()
        self.assertEqual({r["worker_id"]: r["alive"] for r in worker_rows(self.conn)}, {"idle": True, "busy": True, "dead": False})

    def test_health_api_requires_token_and_does_not_start_jobs(self):
        from fastapi.testclient import TestClient
        from delivery.be.app import create_app
        with patch.dict(os.environ, {"DELIVERY_AUTH_MODE": "token", "DELIVERY_API_TOKEN": "a" * 32}):
            client = TestClient(create_app(TEST_PG_DSN))
            for route in ("/health/crawlers", "/health/system"):
                self.assertEqual(client.get(route).status_code, 401)
                self.assertEqual(client.get(route, headers={"x-delivery-token": "a" * 32}).status_code, 200)
            self.assertEqual(self.conn.execute("SELECT count(*) AS n FROM crawl_jobs").fetchone()["n"], 0)

"""api_cursor strategy and the openalex-doaj collector, against a fake OpenAlex API (no network)."""
import copy
import importlib.util
import json
import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from crawler import db_libertree
from crawler.enumeration.http import Response
from crawler.enumeration.plan import PlanError, load_plan, site_plan
from crawler.enumeration.report import write_report
from crawler.enumeration.run import ensure_documents_db, make_adapter, make_fetcher
from crawler.enumeration.runner import SiteRunner
from crawler.sites import CRAWLERS
from tests.enumeration_fakes import Env

ROOT = Path(__file__).resolve().parents[1]
SITE = "openalex-doaj"
KEY = "test-key"
PLAN = load_plan(ROOT / "docs" / "examples" / "openalex-doaj-plan.json")
_spec = importlib.util.spec_from_file_location("openalex_doaj_module",
                                               ROOT / "crawler" / "sites" / "custom" / "openalex-doaj.py")
MODULE = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(MODULE)


def work(n, updated, *, doi=True, title=None, kind="article", doaj=True, **over):
    out = {
        "id": f"https://openalex.org/W{n}",
        "doi": f"https://doi.org/10.1234/ABC.{n}" if doi else None,
        "title": f"  Title {n} " if title is None else title,
        "display_name": f"Title {n}" if title is None else title,
        "publication_date": "2026-01-02",
        "publication_year": 2026,
        "language": "en",
        "type": kind,
        "authorships": [{"author": {"display_name": "Kim, A."}}, {"author": {"display_name": "Lee B"}}, {"author": {}}],
        "primary_location": {"landing_page_url": f"https://journal.test/{n}", "pdf_url": None, "license": "cc-by",
                             "source": {"id": "https://openalex.org/S9", "display_name": "Journal of Tests",
                                        "issn_l": "1234-5678", "host_organization_name": "Pub, Inc.",
                                        "is_in_doaj": doaj}},
        "best_oa_location": {"pdf_url": f"https://pdf.test/{n}.pdf", "license": "cc-by"},
        "open_access": {"oa_url": f"https://pdf.test/{n}.pdf"},
        "keywords": [{"display_name": "Health, Safety"}, {"display_name": "Testing"}],
        "abstract_inverted_index": {"open": [0, 3], "science": [1], "is": [2]},
        "updated_date": updated,
    }
    out.update(over)
    return out


class FakeOpenAlex:
    """/works with filter from_updated_date, sort updated_date:asc and cursor paging. Like the API, a
    page with results names a next cursor; the page after the last result is empty with a null one."""

    def __init__(self, works):
        self.works = list(works)
        self.list_urls = []
        self.honour_sort = True  # False: newest first (sort ignored); None: in stored order
        self.refuse_after = None  # list requests answered before every further one is a 429

    def __call__(self, url):
        parts = urlsplit(url)
        if parts.path == "/robots.txt":
            return Response(404, {}, b"")
        if parts.path != "/works":
            return Response(404, {}, b"")
        self.list_urls.append(url)
        if self.refuse_after is not None and len(self.list_urls) > self.refuse_after:
            return Response(429, {"Content-Type": "application/json"}, b'{"error": "Rate limit exceeded"}')
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        assert q["sort"] == "updated_date:asc", q
        assert "primary_location.source.is_in_doaj:true" in q["filter"] and "type:article|review" in q["filter"], q
        since = re.search(r"from_updated_date:(\d{4}-\d{2}-\d{2})$", q["filter"]).group(1)
        rows = [w for w in self.works if w["updated_date"][:10] >= since]
        if self.honour_sort is not None:
            rows.sort(key=lambda w: w["updated_date"], reverse=not self.honour_sort)
        per = int(q["per_page"])
        offset = 0 if q["cursor"] == "*" else int(q["cursor"][1:])
        chunk = rows[offset:offset + per]
        body = {"meta": {"count": len(rows), "per_page": per, "next_cursor": f"c{offset + per}" if chunk else None},
                "results": chunk}
        return Response(200, {"Content-Type": "application/json"}, json.dumps(body).encode())


class HeaderTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, method, url, *, headers=None, data=None):
        self.calls.append((url, dict(headers or {})))
        return self.handler(url)


def site(**over):
    out = copy.deepcopy(PLAN["sites"][SITE])
    out["request_interval_s"] = 0
    out["listing"]["params"]["per_page"] = 2
    for key, value in over.items():
        out[key] = value
    return out


def query(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


class ApiCursorTest(unittest.TestCase):
    def setUp(self):
        self.env = Env(SITE)
        self.addCleanup(self.env.close)
        self.ref = db_libertree.open_db(self.env.root / "reference.db")  # the runner's empty reference DB
        db_libertree.init_db(self.ref)
        self.addCleanup(self.ref.close)
        self.log = []
        self.gates = self.env.root

    def go(self, api, *, over=None, incremental=False):
        plan = self.env.plan(site(**(over or {})))
        transport = HeaderTransport(api)
        with mock.patch.dict(os.environ, {"OPENALEX_API_KEY": KEY}):
            fetcher = make_fetcher(plan, self.gates, transport)  # the run.py path: request_headers_env
        fetcher.sleep = lambda s: None
        adapter = make_adapter(plan, self.env.conn)
        try:
            runner = SiteRunner(plan, self.env.store, fetcher, adapter, log=self.log.append, reference_conn=self.ref,
                                identity_required=True, allow_empty_reference=True)
            result = runner.run_incremental() if incremental else runner.run()
        finally:
            adapter.close()
        result["transport"] = transport
        return result

    def watermark(self):
        return (self.env.store.site(SITE)["state"].get("api_cursor") or {}).get("since")

    def post_numbers(self):
        return sorted(r[0] for r in self.env.conn.execute("SELECT post_number FROM documents"))

    def list_queries(self, result):
        return [query(u) for u, _ in result["transport"].calls if urlsplit(u).path == "/works"]

    # -- plan --------------------------------------------------------------
    def test_plan_validation(self):
        plan = site_plan(PLAN, SITE, registered=set(CRAWLERS))
        self.assertEqual(plan.strategy, "api_cursor")

        def bad(**over):
            with self.assertRaises(PlanError):
                self.env.plan(site(**over))
        listing = site()["listing"]
        bad(listing=dict(listing, params=dict(listing["params"], filter="type:article")))  # no {since}
        bad(listing=dict(listing, params=dict(listing["params"], sort="{since}")))  # two parameters
        bad(listing=dict(listing, next_cursor=None))
        bad(listing={k: v for k, v in listing.items() if k != "total"})
        bad(api_cursor={"initial_since": "2026-13-01"})
        bad(api_cursor={"initial_since": "2026-10-14", "resume_token": "x"})
        bad(api_cursor={})
        bad(incremental={"known_run": 5})
        with self.assertRaises(PlanError):
            self.env.plan(dict(site(), strategy="oldest_first_sort"))  # api_cursor section on another strategy

    # -- runs --------------------------------------------------------------
    def test_first_run_uses_initial_since_and_follows_the_cursor_to_the_end(self):
        api = FakeOpenAlex([work(1, "2026-10-13T05:00:00"), work(2, "2026-10-14T01:00:00"),
                            work(3, "2026-10-14T09:00:00"), work(4, "2026-10-15T02:00:00"),
                            work(5, "2026-10-16T03:00:00")])
        result = self.go(api)
        self.assertEqual((result["status"], result["stop_reason"]), ("complete", "exhausted_cursor_end"), result)
        self.assertEqual(self.post_numbers(), ["W2", "W3", "W4", "W5"])
        lists = self.list_queries(result)
        self.assertTrue(lists[0]["filter"].endswith("from_updated_date:2026-10-14"))
        # The empty page after the last one (re-read once, as every empty page) ends it.
        self.assertEqual([q["cursor"] for q in lists], ["*", "c2", "c4", "c4"])
        self.assertTrue(all(q["filter"] == lists[0]["filter"] for q in lists))  # one query per run
        self.assertEqual(self.watermark(), "2026-10-16")
        sent = {urlsplit(u).hostname: h.get("Authorization") for u, h in result["transport"].calls}
        self.assertEqual(sent, {"api.openalex.org": "Bearer " + KEY})

    def test_next_run_starts_from_the_watermark_and_skips_known_works(self):
        api = FakeOpenAlex([work(1, "2026-10-14T01:00:00"), work(2, "2026-10-15T01:00:00"),
                            work(3, "2026-10-15T08:00:00")])
        self.assertEqual(self.go(api)["status"], "complete")
        api.works += [work(4, "2026-10-15T23:00:00"), work(5, "2026-10-17T01:00:00")]
        api.works[0] = work(1, "2026-10-17T05:00:00")  # updated again: listed again, never saved twice
        result = self.go(api)
        self.assertEqual(result["status"], "complete", result)
        lists = self.list_queries(result)
        self.assertTrue(lists[0]["filter"].endswith("from_updated_date:2026-10-15"))
        self.assertEqual(lists[0]["cursor"], "*")  # never a token of an earlier run
        self.assertEqual(self.post_numbers(), ["W1", "W2", "W3", "W4", "W5"])
        self.assertEqual(result["stats"]["saved_new"], 2)
        self.assertEqual(result["stats"]["skipped_known"], 3)  # W2, W3 (watermark day) and W1
        self.assertEqual(self.watermark(), "2026-10-17")
        # The collect runner's incremental mode (수집 예약) runs the same pass.
        api.works.append(work(6, "2026-10-18T01:00:00"))
        result = self.go(api, incremental=True)
        self.assertEqual((result["mode"], result["status"]), ("incremental", "complete"), result)
        self.assertTrue(self.list_queries(result)[0]["filter"].endswith("from_updated_date:2026-10-17"))
        self.assertEqual(self.post_numbers()[-1], "W6")
        self.assertEqual(result["duplicates"]["new"], 0)

    def test_no_updates_since_the_watermark_is_complete(self):
        api = FakeOpenAlex([work(1, "2026-10-14T01:00:00")])
        self.go(api)
        self.assertEqual(self.watermark(), "2026-10-14")
        api.works = []
        result = self.go(api)
        self.assertEqual((result["status"], result["stop_reason"]), ("complete", "exhausted_empty_first_page"), result)
        self.assertEqual(self.watermark(), "2026-10-14")

    def test_order_violation_stops_before_saving(self):
        api = FakeOpenAlex([work(1, "2026-10-14T01:00:00"), work(2, "2026-10-15T01:00:00"),
                            work(3, "2026-10-16T01:00:00")])
        api.honour_sort = False
        result = self.go(api)
        self.assertEqual((result["status"], result["stop_reason"]), ("incomplete", "sort_order_not_ascending"))
        self.assertEqual(self.post_numbers(), [])
        self.assertIsNone(self.watermark())

    def test_page_cap_resumes_from_the_watermark(self):
        api = FakeOpenAlex([work(i, f"2026-10-{13 + i:02d}T01:00:00") for i in range(1, 7)])
        result = self.go(api, over={"max_pages": 2})
        self.assertEqual((result["status"], result["stop_reason"]), ("incomplete", "page_cap_reached"))
        self.assertEqual(self.post_numbers(), ["W1", "W2", "W3", "W4"])
        # Page 2 confirmed page 1's order; page 2 itself is not confirmed by a page after it.
        self.assertEqual(self.watermark(), "2026-10-15")
        result = self.go(api, over={"max_pages": 4})  # W2..W6 and the closing empty page
        self.assertEqual(result["status"], "complete", result)
        first = self.list_queries(result)[0]
        self.assertEqual((first["cursor"], first["filter"][-10:]), ("*", "2026-10-15"))
        self.assertEqual(self.post_numbers(), ["W1", "W2", "W3", "W4", "W5", "W6"])
        self.assertEqual(result["stats"]["skipped_known"], 3)  # W2..W4, read again
        self.assertEqual(self.watermark(), "2026-10-19")

    def test_page_cap_inside_one_day_is_reported(self):
        api = FakeOpenAlex([work(i, f"2026-10-14T0{i}:00:00") for i in range(1, 7)])
        result = self.go(api, over={"max_pages": 2})
        self.assertEqual((result["status"], result["stop_reason"]), ("incomplete", "page_cap_within_one_day"))
        self.assertEqual(len(self.env.store.events(SITE, "api_cursor_watermark_not_advanced")), 1)

    def test_order_violation_in_a_later_batch_keeps_the_unread_works(self):
        api = FakeOpenAlex([work(1, "2026-10-14T01:00:00"), work(2, "2026-10-20T01:00:00"),
                            work(3, "2026-10-15T01:00:00"), work(4, "2026-10-16T01:00:00")])
        api.honour_sort = None  # served as stored: page 1 [W1, W2], page 2 [W3, W4]
        result = self.go(api)
        self.assertEqual((result["status"], result["stop_reason"]), ("incomplete", "sort_order_not_ascending"))
        self.assertIsNone(self.watermark())  # page 1's date was never committed
        api.honour_sort = True
        self.assertEqual(self.go(api)["status"], "complete")
        self.assertEqual(self.post_numbers(), ["W1", "W2", "W3", "W4"])

    def test_budget_429_stops_with_a_cooldown_and_resumes(self):
        api = FakeOpenAlex([work(i, f"2026-10-{13 + i:02d}T01:00:00") for i in range(1, 5)])
        api.refuse_after = 1
        result = self.go(api)
        self.assertEqual((result["status"], result["stop_reason"]), ("incomplete", "rate_limited_cooldown"), result)
        self.assertEqual(len(api.list_urls), 4)  # page 1, then the refused page tried 3 times, no loop
        self.assertEqual(self.post_numbers(), ["W1", "W2"])
        saved, failed = self.env.store.item_counts(SITE)
        self.assertEqual((saved, failed), (2, {}))  # nothing marked failed
        self.assertIsNone(self.watermark())  # page 1 was saved, but its order was not confirmed yet
        self.assertGreater(self.env.store.site(SITE)["state"]["cooldown_until"], time.time())
        held = self.go(api)
        self.assertEqual(held["stop_reason"], "cooling_down")
        self.assertEqual(len(api.list_urls), 4)  # no request while cooling down
        state = self.env.store.site(SITE)["state"]
        state["cooldown_until"] = time.time() - 1  # the reset time has passed
        self.env.store.save_state(SITE, state)
        self.gates = self.env.root / "later"  # the host gate's own cooldown has passed too
        api.refuse_after = None
        result = self.go(api)
        self.assertEqual(result["status"], "complete", result)
        self.assertTrue(self.list_queries(result)[0]["filter"].endswith("2026-10-14"))
        self.assertEqual(self.post_numbers(), ["W1", "W2", "W3", "W4"])

    def test_untitled_work_is_skipped_by_rule_and_counted(self):
        api = FakeOpenAlex([work(i, f"2026-10-14T0{i}:00:00") for i in range(1, 7)])
        api.works[2]["title"] = None
        api.works[2]["display_name"] = "  "
        result = self.go(api)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(self.post_numbers(), ["W1", "W2", "W4", "W5", "W6"])
        self.assertEqual(self.env.store.item_counts(SITE)[1], {"failed_permanent:skipped_by_rule": 1})
        self.assertIn("skipped_by_rule:1", self.env.store.site(SITE)["state"]["warnings"])

    def test_work_outside_the_filter_stops_the_site(self):
        for bad in (work(1, "2026-10-14T01:00:00", kind="editorial"), work(1, "2026-10-14T01:00:00", doaj=False)):
            env = Env(SITE)
            self.addCleanup(env.close)
            self.env = env
            result = self.go(FakeOpenAlex([bad]))
            self.assertEqual((result["status"], result["stop_reason"]),
                             ("config_error", "openalex_work_outside_filter"), result)

    # -- mapping -----------------------------------------------------------
    def test_abstract_restoration(self):
        restore = MODULE.restore_abstract
        self.assertEqual(restore({"science": [1], "open": [0, 3], "is": [2]}), "open science is open")
        self.assertEqual(restore({"b": [5], "a": [2]}), "a b")  # gaps in positions are closed
        self.assertIsNone(restore(None))
        self.assertIsNone(restore({}))

    def test_documents_match_the_bulk_export_rows(self):
        works = [work(1, "2026-10-14T01:00:00"),
                 work(2, "2026-10-14T02:00:00", doi=False, language=None, publication_date=None,
                      best_oa_location=None, abstract_inverted_index=None, keywords=[],
                      primary_location={"pdf_url": "https://journal.test/2.pdf",
                                        "source": {"id": "https://openalex.org/S8", "display_name": "J2",
                                                   "is_in_doaj": True}}),
                 work(3, "2026-10-14T03:00:00", doi="HTTPS://DX.DOI.ORG/10.5555/XyZ.3 ", authorships=[])]
        self.go(FakeOpenAlex(works))
        cols = ("site_id", "post_number", "meta_url", "title", "published_date", "listed_date", "authors", "publisher",
                "journal", "pdf_url", "keywords", "abstract", "published_date_raw", "published_date_status",
                "published_date_precision", "listed_date_raw", "listed_date_status", "listed_date_precision")
        rows = [tuple(r) for r in self.env.conn.execute(f"SELECT {','.join(cols)} FROM documents ORDER BY seq_id")]
        self.assertEqual(rows, [bulk_document(w) for w in works])

    def test_site_row_matches_the_bulk_folders(self):
        cls = CRAWLERS[SITE]
        conn = db_libertree.open_db(self.env.root / "enum_site.db")
        self.addCleanup(conn.close)
        ensure_documents_db(conn, SITE, cls.site_name, cls.base_url, db_name="enum_site.db", sheet=cls.sheet)
        row = conn.execute("SELECT site_name, site_url, sheet FROM sites WHERE site_id=?", (SITE,)).fetchone()
        self.assertEqual(tuple(row), ("OpenAlex (CC0), DOAJ 등재 학술지", "https://openalex.org", "OpenAlex"))

    # -- secrets -----------------------------------------------------------
    def test_key_is_never_stored_or_logged(self):
        api = FakeOpenAlex([work(i, f"2026-10-{13 + i:02d}T01:00:00") for i in range(1, 5)])
        api.refuse_after = 1
        results = [self.go(api)]
        api.refuse_after = None
        state = self.env.store.site(SITE)["state"]
        state["cooldown_until"] = time.time() - 1
        self.env.store.save_state(SITE, state)
        self.gates = self.env.root / "later"
        results.append(self.go(api))
        self.assertTrue(all("Bearer " + KEY == h.get("Authorization") for r in results for _, h in r["transport"].calls))
        report = self.env.root / "reports"
        write_report(self.env.store, [SITE], report, doc_conn=self.env.conn)
        texts = ["\n".join(self.env.store.conn.iterdump()), "\n".join(self.env.conn.iterdump()), "\n".join(self.log),
                 json.dumps([{k: v for k, v in r.items() if k != "transport"} for r in results], default=str)]
        texts += [p.read_text(encoding="utf-8") for p in report.iterdir()]
        self.assertTrue(texts[0] and any("api_cursor_since" in t for t in texts))
        for text in texts:
            self.assertNotIn(KEY, text)
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(PlanError) as caught:
                make_fetcher(self.env.plan(site()), self.gates, HeaderTransport(api))
        self.assertIn("OPENALEX_API_KEY", str(caught.exception))


def bulk_document(w):
    """Expected row: the snapshot filter's slim record (filter_one.py) through the bulk export's
    document() (build_folders.py), as they ran for the 2026-10-25 import; collected_at left out."""
    pl = w.get("primary_location") or {}
    src = pl.get("source") or {}
    best = w.get("best_oa_location") or {}

    def abstract(inv):
        if not inv:
            return None
        pos = {}
        for word, places in inv.items():
            for p in places:
                pos[p] = word
        return " ".join(pos[i] for i in sorted(pos)) or None

    def norm_doi(doi):
        if not doi:
            return None
        d = doi.strip().lower()
        for p in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/"):
            if d.startswith(p):
                d = d[len(p):]
        return d or None
    r = {"id": w["id"].rsplit("/", 1)[-1], "doi": w.get("doi"), "title": w.get("title") or w.get("display_name"),
         "publication_date": w.get("publication_date"), "language": w.get("language"),
         "authors": [((a.get("author") or {}).get("display_name")) for a in (w.get("authorships") or [])
                     if (a.get("author") or {}).get("display_name")],
         "journal": src.get("display_name"), "issn_l": src.get("issn_l"), "publisher": src.get("host_organization_name"),
         "pdf_url": best.get("pdf_url") or pl.get("pdf_url"),
         "keywords": [k.get("display_name") for k in (w.get("keywords") or []) if k.get("display_name")],
         "abstract": abstract(w.get("abstract_inverted_index"))}
    doi = norm_doi(r.get("doi"))
    kw = list(r.get("keywords") or [])
    if doi:
        kw.append("DOI:" + doi)
    if r.get("issn_l"):
        kw.append("ISSN-L:" + r["issn_l"])
    if r.get("language"):
        kw.append("lang:" + r["language"])
    kw.append("source:OpenAlex (CC0)")
    pd = r.get("publication_date")
    return (SITE, r["id"], ("https://doi.org/" + doi) if doi else ("https://openalex.org/" + r["id"]),
            r["title"].strip(), pd, None, "; ".join(r.get("authors") or []) or None, r.get("publisher"), r.get("journal"),
            r.get("pdf_url"), "; ".join(kw), r.get("abstract"),
            pd, "normalized" if pd else "missing", "day" if pd else None, None, "missing", None)


@unittest.skipIf(os.geteuid() == 0, "run-pilot.sh refuses root")
class RunPilotEnvFileTest(unittest.TestCase):
    """deploy/enum-pilot/run-pilot.sh ENUM_ENV_FILE, with a fake docker that only records its arguments."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "docker"
        fake.write_text('#!/usr/bin/env bash\necho "$*" >> "$FAKE_DOCKER_LOG"\n'
                        'for a in "$@"; do [[ "$a" == "--help" ]] && { echo "--seed-holds --incremental"; exit 0; }; done\n'
                        '[[ "$1 $2" == "network inspect" ]] && echo true\nexit 0\n')
        fake.chmod(0o755)
        (self.dir / "plan.json").write_text("{}")
        (self.dir / "seed-holds.csv").write_text("host,reason\n")
        self.secret = self.dir / "openalex.env"
        self.secret.write_text("OPENALEX_API_KEY=" + KEY + "\n")
        self.env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_DOCKER_LOG=str(self.dir / "docker.log"),
                        ENUM_PILOT_ROOT=str(self.dir / "runs"), ENUM_PILOT_DATE="20261026-t1")
        for name in ("ENUM_INCREMENTAL", "ENUM_KNOWN_KEYS", "ENUM_EXTRA_ARGS", "ENUM_REFERENCE_DB", "ENUM_ENV_FILE"):
            self.env.pop(name, None)

    def run_pilot(self, env_file):
        env = dict(self.env, ENUM_ENV_FILE=str(env_file))
        return subprocess.run([str(ROOT / "deploy" / "enum-pilot" / "run-pilot.sh"), "run", str(self.dir / "plan.json"),
                               SITE], env=env, capture_output=True, text=True, timeout=60)

    def docker_log(self):
        path = self.dir / "docker.log"
        return path.read_text() if path.exists() else ""

    def test_private_regular_file_goes_to_docker_env_file(self):
        self.secret.chmod(0o600)
        proc = self.run_pilot(self.secret)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        final = [line for line in self.docker_log().splitlines() if line.startswith("run --rm --name")]
        self.assertEqual(len(final), 1)
        self.assertIn(f"--env-file {self.secret}", final[0])
        self.assertNotIn(KEY, proc.stdout + proc.stderr + self.docker_log())

    def test_readable_or_linked_files_are_refused_before_docker(self):
        for mode in (0o640, 0o620, 0o604):  # readable or writable by others
            self.secret.chmod(mode)
            proc = self.run_pilot(self.secret)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("ENUM_ENV_FILE", proc.stderr)
        self.secret.chmod(0o600)
        link = self.dir / "link.env"
        link.symlink_to(self.secret)
        for path in (link, self.dir / "missing.env", self.dir):
            proc = self.run_pilot(path)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("ENUM_ENV_FILE", proc.stderr)
        self.assertEqual(self.docker_log(), "")
        self.assertNotIn(KEY, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()

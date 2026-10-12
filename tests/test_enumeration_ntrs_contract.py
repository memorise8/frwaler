"""ntrs-nasa-gov-search through the adapter contract (no network).

1. The registered crawler's network-free methods (_parse_search_results,
   _paper_from_result) against its unchanged crawl(), fed the same synthetic search
   API pages (crawl()'s curl helper is replaced by the fake).
2. The contract plan (docs/examples/ntrs-contract-plan.json, or NTRS_CONTRACT_PLAN)
   end to end through SiteRunner with a synthetic NTRS search API: range splitting,
   paper building without detail requests, identity equal to the legacy crawl() rows,
   budget stop and resume.
"""
import contextlib
import copy
import io
import json
import os
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from crawler import db_libertree
from crawler.enumeration.adapters import Adapter, AdapterConfigError, clean_paper, paper_identity
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import Response
from crawler.enumeration.plan import load_plan, site_plan
from tests.enumeration_adapter_helpers import ROOT, load_crawler
from tests.enumeration_fakes import Env, FakeTransport

SITE = "ntrs-nasa-gov-search"
SCOPE = "Contractor Report (CR)"
PLAN = Path(os.environ.get("NTRS_CONTRACT_PLAN") or ROOT / "docs/examples/ntrs-contract-plan.json")
WINDOW = 10000  # observed by an earlier probe (from+size above it -> 400); not verified here
TODAY = "2026-10-12"
LONG = "Synthetic offline abstract of a contractor report, used only in tests. "
_CLS = []


def crawler_class():
    if not _CLS:
        _CLS.append(type(load_crawler(SITE)))
    return _CLS[0]


def record(n, day, *, abstract=None, title=None, tod="05:00:00", variant=None, dated=True):
    rid = 20000000000 + n
    v = n % 6 if variant is None else variant
    rec = {"id": rid, "title": f"CR report {n}" if title is None else title, "stiType": "CONTRACTOR_REPORT",
           "stiTypeDetails": SCOPE, "abstract": LONG * 2 if abstract is None else abstract,
           "publications": [{"publicationDate": f"{day.isoformat()}T{tod}.0000000+00:00"}] if dated else [],
           "submittedDate": f"{day.isoformat()}T00:00:00.0000000+00:00",
           "distributionDate": f"{(day + timedelta(days=30)).isoformat()}T00:00:00.0000000+00:00",
           "status": "CURATED", "disseminated": "DOCUMENT_AND_METADATA", "distribution": "PUBLIC",
           "center": {"code": "CDMS", "name": "Legacy CDMS"}}
    if v != 1:
        rec["authorAffiliations"] = [{"meta": {"author": {"name": "Ada One"}, "organization": {"name": "Org X"}}},
                                     {"meta": {"author": {"name": "Ada One"}, "organization": {"name": "Org Y"}}},
                                     {"meta": {"author": {"name": " Bo Two "}}}]
    if v != 2:
        rec["keywords"] = ["Aerodynamics", "Wind tunnel"]
        rec["subjectCategories"] = ["Aeronautics (General)", "Fluid Mechanics"]
    if v == 3:
        rec["downloads"] = [{"name": "r.pdf",
                             "links": {"original": f"https://ntrs.nasa.gov/api/citations/{rid}/downloads/r.pdf"}}]
    elif v != 4:
        rec["downloads"] = [{"name": "r.pdf", "links": {"pdf": f"/api/citations/{rid}/downloads/r.pdf"}}]
    if v == 5:
        rec["otherReportNumbers"] = [f"NASA-CR-{n}"]
        rec["fundingNumbers"] = [{"number": f"NAS1-{n}"}, {}]
    return rec


def spread(start, end, count, first):
    days = (end - start).days + 1
    return [record(first + i, start + timedelta(days=(i * 7) % days)) for i in range(count)]


def pubdate(rec):
    return rec["publications"][0]["publicationDate"] if rec.get("publications") else ""


class FakeNTRS:
    """GET /api/citations/search. published[lte]=D includes the whole day D here; whether
    the live API does that is one of the README's pre-run checks, not a fact."""

    def __init__(self, records, *, html_search=False):
        self.records = records
        self.html_search = html_search
        self.urls = []
        self.violations = []
        self.max_window_end = 0

    def __call__(self, url):
        self.urls.append(url)
        parts = urlsplit(url)
        if parts.netloc != "ntrs.nasa.gov":
            return Response(404, {}, b"")
        if parts.path == "/robots.txt":
            return Response(200, {"Content-Type": "text/plain"}, b"User-agent: *\nDisallow:\n")
        if parts.path != "/api/citations/search":
            return Response(404, {}, b"")
        if self.html_search:
            return Response(200, {"Content-Type": "text/html"}, "<html><body>maintenance</body></html>", url)
        q = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        if q.get("stiTypeDetails") != SCOPE or q.get("page[size]") != "100":
            self.violations.append(url)
        start, size = int(q.get("page[from]", 0)), int(q.get("page[size]", 25))
        self.max_window_end = max(self.max_window_end, start + size)
        if start + size > WINDOW:
            return Response(400, {"Content-Type": "application/json"},
                            json.dumps({"message": "search_phase_execution_exception"}))
        gte, lte = q.get("published[gte]"), q.get("published[lte]")
        rows = [r for r in self.records
                if (gte is None and lte is None) or
                (pubdate(r) and (not gte or pubdate(r)[:10] >= gte) and (not lte or pubdate(r)[:10] <= lte))]
        rows.sort(key=lambda r: (pubdate(r), r["id"]))
        body = {"stats": {"took": 1, "total": len(rows), "estimate": False, "maxScore": 0},
                "results": copy.deepcopy(rows[start:start + size]), "aggregations": {}}
        return Response(200, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(body), url)


def legacy_crawl(conn, api):
    """The registered crawl(), unchanged, with its curl helper answered by ``api``.
    Returns the paper dicts it passed to _save_paper."""
    cls = crawler_class()
    crawler = cls(db_conn=conn)  # BaseCrawler only builds a requests.Session (no request)
    saved = []
    original = crawler._save_paper

    def save(paper):
        saved.append(copy.deepcopy(paper))
        return original(paper)
    crawler._save_paper = save

    def curl(url, retries=3):
        return api(url).text()
    with patch.dict(cls.crawl.__globals__, {"_curl_get": curl}), contextlib.redirect_stdout(io.StringIO()):
        crawler.crawl()
    return saved


def documents_db(root, name):
    conn = db_libertree.open_db(Path(root) / name)
    db_libertree.init_db(conn)
    db_libertree.upsert_site(conn, SITE, SITE, "https://ntrs.nasa.gov")
    return conn


def rows_by_post(conn):
    cur = conn.execute("SELECT * FROM documents WHERE site_id=?", (SITE,))
    names = [d[0] for d in cur.description]
    return {r[names.index("post_number")]: {k: v for k, v in zip(names, r) if k not in ("seq_id", "collected_at")}
            for r in cur.fetchall()}


class RegisteredMethods(unittest.TestCase):
    """The split methods build exactly what crawl() saves, without any network."""

    def corpus(self):
        recs = [record(i, date(1975, 1, 1) + timedelta(days=i)) for i in range(1, 25)]
        recs.append(record(30, date(1976, 1, 1), dated=False))           # submittedDate fallback
        recs.append(record(31, date(1976, 1, 2), abstract="too short"))    # crawl() skips (< 100 chars)
        recs.append(record(32, date(1976, 1, 3), title="  "))              # crawl() skips (no title)
        no_id = record(33, date(1976, 1, 4))
        no_id["id"] = None                                                 # crawl() skips (no id)
        recs.append(no_id)
        return recs

    def test_methods_match_crawl_papers(self):
        recs = self.corpus()
        api = FakeNTRS(recs)
        env = Env(site_id=SITE)
        self.addCleanup(env.close)
        legacy = legacy_crawl(env.conn, api)
        self.assertEqual(25, len(legacy))  # 24 dated + 1 undated; short/untitled/no-id skipped
        crawler = load_crawler(SITE)
        page = json.loads(api(api.urls[0]).text())
        with patch.dict(type(crawler).crawl.__globals__, {"_curl_get": None}), guarded():
            rows = crawler._parse_search_results(page)
            papers = {str(row["id"]): crawler._paper_from_result(row["result"]) for row in rows}
        self.assertEqual([r["id"] for r in page["results"] if r["id"]], [row["id"] for row in rows])
        for row in rows:
            self.assertEqual(f"https://ntrs.nasa.gov/citations/{row['id']}", row["url"])
        for paper in legacy:
            self.assertEqual(paper, papers[paper["post_number"]])
            self.assertEqual(paper_identity(clean_paper(paper)),
                             paper_identity(clean_paper(papers[paper["post_number"]])))
        short, untitled = papers[str(20000000031)], papers[str(20000000032)]
        # The short-abstract filter lives only in crawl(): the method keeps the item.
        self.assertEqual("too short", short["abstract"])
        self.assertIsNone(untitled)

    def test_list_method_without_results_list(self):
        crawler = load_crawler(SITE)
        with guarded():
            for data in ({}, [], {"results": None}, {"results": "x"}, "x"):
                self.assertIsNone(crawler._parse_search_results(data))
            self.assertEqual([], crawler._parse_search_results({"results": []}))


class ContractPlan(unittest.TestCase):
    def test_plan_validates_and_adapter_builds(self):
        data = load_plan(PLAN)
        sp = site_plan(data, SITE, registered=[SITE])  # real 1.5 s minimum interval
        self.assertEqual("date_range_split", sp.strategy)
        adapter = Adapter(SITE, sp.section("adapter"), None, crawler=load_crawler(SITE))
        self.assertIsNotNone(adapter.contract)
        self.assertFalse(adapter.needs_detail())
        self.assertTrue(adapter.identity_uses_list_fields())

    def test_registered_crawler_without_contract_still_refused(self):
        with self.assertRaisesRegex(AdapterConfigError, "needs detail_method, paper_method or list_returns_paper"):
            Adapter(SITE, {"type": "crawler"}, None, crawler=load_crawler(SITE))


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.env = Env(site_id=SITE)
        self.addCleanup(self.env.close)

    def site(self, **segments):
        site = copy.deepcopy(load_plan(PLAN)["sites"][SITE])
        site["request_interval_s"] = 0  # tests only; the plan keeps 1.5 s
        site["segments"].update(segments)
        return site

    def run_plan(self, api, site, **kw):
        crawler = crawler_class()(db_conn=self.env.conn)
        return self.env.run(site, None, crawler=crawler, transport=FakeTransport(api), today=TODAY, site_id=SITE,
                            **kw)

    def segments(self):
        fp = self.env.store.site(SITE)["plan_fp"]
        return {r["segment_key"]: r for r in self.env.store.segments(SITE, fp)}

    def test_split_fields_and_identity_equal_to_crawl(self):
        recs = (spread(date(1962, 1, 1), date(1962, 12, 31), 20, 1)
                + spread(date(1975, 1, 1), date(1975, 2, 28), 15, 100)
                + spread(date(1975, 3, 1), date(1975, 3, 31), 45, 200)
                + [record(300, date(1975, 6, 1), abstract="short")]
                + spread(date(2026, 1, 1), date(2026, 9, 30), 5, 400))
        api = FakeNTRS(recs)
        result = self.run_plan(api, self.site(hidden_cap=40))
        self.assertEqual(("complete", "all_segments_reconciled"), (result["status"], result["stop_reason"]), result)
        segs = self.segments()
        self.assertEqual("split", segs["1975"]["status"])
        self.assertEqual("split", segs["1975-03"]["status"])
        self.assertEqual("complete", segs["1975-03-01"]["status"])
        paths = {urlsplit(u).path for u in api.urls}
        self.assertEqual({"/robots.txt", "/api/citations/search"}, paths)  # no detail request
        self.assertEqual([], api.violations)
        docs = rows_by_post(self.env.conn)
        self.assertEqual({str(r["id"]) for r in recs}, set(docs))
        for post, row in docs.items():
            self.assertEqual(f"https://ntrs.nasa.gov/citations/{post}", row["meta_url"])
        # The legacy crawl() over the same corpus: identical rows for everything it saves;
        # the only extra contract row is the short-abstract item crawl() skips.
        with __import__("tempfile").TemporaryDirectory() as tmp:
            legacy_conn = documents_db(tmp, "legacy.db")
            try:
                legacy_crawl(legacy_conn, FakeNTRS(recs))
                legacy = rows_by_post(legacy_conn)
            finally:
                legacy_conn.close()
        self.assertEqual({str(20000000300)}, set(docs) - set(legacy))
        self.assertEqual(set(), set(legacy) - set(docs))
        for post, row in legacy.items():
            self.assertEqual(row, docs[post])
        sample = docs[str(20000000201)]
        self.assertTrue(sample["abstract"] and sample["authors"] and sample["pdf_url"])

    def test_identity_precheck_against_legacy_rows(self):
        recs = spread(date(1801, 1, 1), date(1801, 12, 31), 30, 1) + [record(99, date(1801, 12, 30), abstract="x")]
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ref = documents_db(tmp.name, "reference.db")
        self.addCleanup(ref.close)
        legacy_crawl(ref, FakeNTRS(recs))
        result = self.run_plan(FakeNTRS(recs), self.site(), reference_conn=ref, identity_required=True)
        self.assertEqual("complete", result["status"], result)
        identity = result["identity"]
        self.assertEqual(("listing_start_page", 1.0), (identity["source"], identity["match_rate"]))
        self.assertEqual(5, identity["compared"])
        self.assertEqual(5, identity["contract_data_errors"])  # reference URLs cannot rebuild list rows

    def test_real_cap_splits_year_month_day(self):
        recs = (spread(date(1990, 3, 1), date(1990, 3, 31), 9900, 1)
                + spread(date(1990, 5, 1), date(1990, 5, 31), 100, 20000))
        api = FakeNTRS(recs)
        result = self.run_plan(api, self.site())
        self.assertEqual("complete", result["status"], result)
        segs = self.segments()
        self.assertEqual(("split", "split", "complete"),
                         (segs["1990"]["status"], segs["1990-03"]["status"], segs["1990-05"]["status"]))
        self.assertLessEqual(api.max_window_end, WINDOW)
        self.assertEqual(10000, len(rows_by_post(self.env.conn)))

    def test_budget_stop_and_resume(self):
        recs = (spread(date(1801, 1, 1), date(1801, 12, 31), 150, 1)
                + spread(date(1802, 1, 1), date(1802, 12, 31), 150, 1000)
                + spread(date(1803, 1, 1), date(1803, 12, 31), 50, 2000))
        first = self.run_plan(FakeNTRS(recs), self.site(), max_requests=6)
        self.assertEqual(("incomplete", "request_budget"), (first["status"], first["stop_reason"]), first)
        partial = len(rows_by_post(self.env.conn))
        self.assertTrue(0 < partial < len(recs), partial)
        for _ in range(200):
            result = self.run_plan(FakeNTRS(recs), self.site(), max_requests=6)
            if result["status"] != "incomplete":
                break
        self.assertEqual("complete", result["status"], result)
        docs = rows_by_post(self.env.conn)
        self.assertEqual({str(r["id"]) for r in recs}, set(docs))
        self.assertEqual(0, (result.get("duplicates") or {}).get("after", 0))

    def test_short_abstracts_never_stop_and_untitled_is_review(self):
        recs = [record(i, date(1801, 2, i), abstract="") for i in range(1, 8)]   # 7 consecutive short
        recs += [record(20, date(1801, 3, 1), title="")]                         # crawl() skips: no title
        recs += spread(date(1801, 4, 1), date(1801, 4, 30), 10, 30)
        result = self.run_plan(FakeNTRS(recs), self.site())
        self.assertEqual(("complete_with_review", "adapter_contract_failures:1"),
                         (result["status"], result["stop_reason"]), result)
        _, failed = self.env.store.item_counts(SITE)
        self.assertEqual({"failed_permanent:empty_item": 1}, failed)
        self.assertEqual(17, len(rows_by_post(self.env.conn)))

    def test_non_json_search_page_is_a_listing_error(self):
        result = self.run_plan(FakeNTRS([record(1, date(1801, 1, 1))], html_search=True), self.site())
        self.assertEqual(("incomplete", "listing_failed:listing_parse_error:invalid_json"),
                         (result["status"], result["stop_reason"]))


if __name__ == "__main__":
    unittest.main()

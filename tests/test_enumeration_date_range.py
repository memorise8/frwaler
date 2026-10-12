"""date_range_split against a fake filtered search (no network)."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date, timedelta
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from crawler.enumeration.http import Response  # noqa: E402
from tests.enumeration_fakes import Env, FakeTransport, html  # noqa: E402

YEAR_COUNTS = {2018: 12, 2019: 40, 2020: 7}


def make_items(year_counts=YEAR_COUNTS, crowded_day=None):
    items, n = [], 0
    for year, count in sorted(year_counts.items()):
        for i in range(count):
            n += 1
            day = crowded_day if crowded_day and year == crowded_day.year else \
                date(year, 1, 1) + timedelta(days=(i * 9) % 360)
            items.append({"id": n, "date": day})
    return items


class FakeSearch:
    """Date-filtered search. Results newest-first, 10 per page, hidden cap on reachable rows."""

    def __init__(self, items, *, cap=None, required=None, undated=0, mode="range"):
        self.items = items
        self.cap = cap
        self.required = required or {}
        self.undated = undated
        self.mode = mode
        self.bad_requests = []

    def __call__(self, url):
        parts = urlsplit(url)
        if parts.path == "/robots.txt":
            return Response(404, {}, b"")
        q = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        if parts.path.startswith("/doc/"):
            i = int(parts.path.rsplit("/", 1)[1])
            return Response(200, {"Content-Type": "text/html"}, html(f"<h1>Doc {i}</h1>"))
        if not parts.path.startswith("/search"):
            return Response(404, {}, b"")
        for key, value in self.required.items():
            if q.get(key) != value:
                self.bad_requests.append(url)
                return Response(200, {"Content-Type": "text/html"}, html("<ul></ul><p>Total 0 results</p>"))
        rows = list(self.items)
        filtered = False
        if self.mode == "range" and "from" in q:
            lo, hi = date.fromisoformat(q["from"]), date.fromisoformat(q["to"])
            rows = [r for r in rows if lo <= r["date"] <= hi]
            filtered = True
        if self.mode == "parts" and "year" in q:
            rows = [r for r in rows if r["date"].year == int(q["year"])]
            if "month" in q:
                rows = [r for r in rows if r["date"].month == int(q["month"])]
            filtered = True
        if parts.path.startswith("/search/"):
            decade = parts.path.rsplit("/", 1)[1]
            rows = [r for r in rows if str(r["date"].year)[:3] == decade[:3]]
            filtered = True
        total = len(rows) + (0 if filtered else self.undated)
        rows.sort(key=lambda r: (r["date"], r["id"]), reverse=True)
        reachable = rows[: self.cap] if self.cap else rows
        page = int(q.get("page", 1))
        chunk = reachable[(page - 1) * 10: page * 10]
        body = "<ul>" + "".join(f'<li><a href="/doc/{r["id"]}">Doc {r["id"]}</a><time datetime="{r["date"]}">'
                                f'{r["date"]}</time></li>' for r in chunk) + "</ul>"
        body += f"<p>Total {total} results</p>"
        return Response(200, {"Content-Type": "text/html"}, html(body))


def plan(**over):
    site = {
        "strategy": "date_range_split",
        "request_interval_s": 0,
        "listing": {"url": "https://search.test/search", "page_param": "page", "start_page": 1,
                    "parser": {"type": "html", "item_container": "li", "item_link": "a[href]", "date": "time"},
                    "total": {"type": "regex", "pattern": r"Total\s+(\d+)\s+results"}},
        "segments": {"mode": "range", "from": "2018", "to": "2020", "granularity": "year",
                     "filter": {"type": "range", "from_param": "from", "to_param": "to"}},
        "adapter": {"type": "list_only"},
    }
    for key, value in over.items():
        site[key] = {**site[key], **value} if isinstance(value, dict) and isinstance(site.get(key), dict) else value
    return site


def doc_ids(env):
    return [int(d["meta_url"].rsplit("/", 1)[1]) for d in env.docs()]


class DateRangeSplitTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def segments(self):
        site = self.env.store.site(self.env.site_id)
        return {s["segment_key"]: s for s in self.env.store.segments(self.env.site_id, site["plan_fp"])}

    def test_year_segments_reconcile(self):
        items = make_items()
        result = self.env.run(plan(), FakeSearch(items))
        self.assertEqual(result["status"], "complete", result)
        segs = self.segments()
        self.assertEqual(sorted(segs), ["2018", "2019", "2020"])
        self.assertTrue(all(s["status"] == "complete" and s["evidence"] == "total_matched" for s in segs.values()))
        self.assertEqual(sorted(doc_ids(self.env)), list(range(1, len(items) + 1)))
        # Segments run oldest first: every 2018 document precedes every 2019 one.
        ids = doc_ids(self.env)
        self.assertEqual(sorted(ids[:12]), list(range(1, 13)))

    def test_reported_total_over_hidden_cap_splits_before_reading(self):
        items = make_items()
        transport = FakeTransport(FakeSearch(items, cap=25))
        result = self.env.run(plan(segments={"hidden_cap": 25}), None, transport=transport)
        self.assertEqual(result["status"], "complete", result)
        segs = self.segments()
        self.assertEqual(segs["2019"]["status"], "split")
        self.assertEqual(segs["2019"]["detail"]["split_reason"], "reported_total_over_cap")
        self.assertIn("2019-01", segs)
        self.assertEqual(sorted(doc_ids(self.env)), list(range(1, len(items) + 1)))
        # The over-cap year was split from its first page: no later page of it was read.
        self.assertFalse([u for u in transport.urls if "from=2019-01-01&to=2019-12-31&page=2" in u])

    def test_silent_cap_detected_by_listed_below_total(self):
        items = make_items()
        result = self.env.run(plan(), FakeSearch(items, cap=25))
        self.assertEqual(result["status"], "complete", result)
        segs = self.segments()
        self.assertEqual(segs["2019"]["status"], "split")
        self.assertEqual(segs["2019"]["detail"]["split_reason"], "listed_below_total")
        self.assertEqual(sorted(doc_ids(self.env)), list(range(1, len(items) + 1)))

    def test_cap_at_finest_granularity_is_reported(self):
        items = make_items({2019: 30}, crowded_day=date(2019, 3, 3))
        site = plan(segments={"from": "2019", "to": "2019", "hidden_cap": 20})
        result = self.env.run(site, FakeSearch(items, cap=20))
        self.assertEqual(result["status"], "complete_with_mismatch", result)
        segs = self.segments()
        self.assertEqual(segs["2019-03-03"]["status"], "cap_unsplittable")
        self.assertEqual(len(doc_ids(self.env)), 20)  # what is reachable is still saved

    def test_preencoded_filter_keys_are_not_double_encoded(self):
        items = make_items()
        fake = FakeSearch(items, required={"f[0]": "type:report", "f[1]": "lang:en"})
        transport = FakeTransport(fake)
        site = plan(listing={"url": "https://search.test/search?f%5B1%5D=lang%3Aen",
                             "params": {"f%5B0%5D": "type:report"}})
        result = self.env.run(site, None, transport=transport)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(fake.bad_requests, [])
        self.assertFalse([u for u in transport.urls if "%25" in u])
        self.assertEqual(len(doc_ids(self.env)), len(items))

    def test_overall_total_exposes_undated_items(self):
        items = make_items()
        result = self.env.run(plan(overall_total_check=True), FakeSearch(items, undated=4))
        self.assertEqual(result["status"], "complete_with_mismatch", result)
        event = self.env.store.events(self.env.site_id, "overall_total_mismatch")[-1]["detail"]
        self.assertEqual(event, {"overall_total": len(items) + 4, "listed": len(items)})

    def test_year_month_parts_filter(self):
        items = make_items()
        site = plan(segments={"filter": {"type": "parts", "year_param": "year", "month_param": "month"},
                              "hidden_cap": 25})
        result = self.env.run(site, FakeSearch(items, cap=25, mode="parts"))
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(self.segments()["2019"]["status"], "split")
        self.assertEqual(len(doc_ids(self.env)), len(items))

    def test_explicit_segments_with_path_template(self):
        items = make_items({1995: 5, 2004: 6, 2013: 3})
        site = plan(listing={"url": "https://search.test/search/{segment}"},
                    segments={"mode": "explicit", "items": [{"key": "1990s"}, {"key": "2000s"}, {"key": "2010s"}]})
        result = self.env.run(site, FakeSearch(items))
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(sorted(doc_ids(self.env)), list(range(1, 15)))

    def test_resume_skips_completed_segments(self):
        items = make_items()
        first = self.env.run(plan(), FakeSearch(items), max_requests=4)
        self.assertEqual((first["status"], first["stop_reason"]), ("incomplete", "request_budget"))
        self.assertEqual(self.segments()["2018"]["status"], "complete")
        transport = FakeTransport(FakeSearch(items))
        second = self.env.run(plan(), None, transport=transport)
        self.assertEqual(second["status"], "complete", second)
        self.assertFalse([u for u in transport.urls if "from=2018-01-01" in u])
        self.assertEqual(sorted(doc_ids(self.env)), list(range(1, len(items) + 1)))

    def test_all_empty_segments_are_not_completion(self):
        result = self.env.run(plan(), FakeSearch([]))
        self.assertEqual((result["status"], result["stop_reason"]), ("incomplete", "all_segments_empty_unverified"))


if __name__ == "__main__":
    unittest.main()

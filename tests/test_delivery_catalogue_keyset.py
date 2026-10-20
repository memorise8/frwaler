"""Database-free tests for catalogue cursors, keyset SQL and request limits."""
from __future__ import annotations

import base64
import json
import os
import sys
import unittest
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from delivery.be import catalogue
from delivery.be.catalogue import (CatalogueError, CatalogueFilters, Cursor, collect_catalogue,
                                   decode_cursor, encode_cursor, filters_fingerprint)


class FakeConn:
    """Routes SQL by substring to canned rows and records every statement."""

    def __init__(self, routes):
        self.routes = routes
        self.statements: list[tuple[str, object]] = []

    def execute(self, sql, params=None, **_kwargs):
        self.statements.append((sql, params))
        for needle, rows in self.routes:
            if needle in sql:
                result = rows(sql, params) if callable(rows) else rows
                break
        else:
            raise AssertionError(f"unexpected SQL: {sql[:120]}")
        return _Result(result)


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


def _token(payload: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


class CursorTest(unittest.TestCase):
    def setUp(self):
        self.fp = filters_fingerprint(CatalogueFilters(site_id="s"))

    def test_round_trip_for_every_sort(self):
        cases = {
            "collected_desc": ((datetime(2026, 10, 1, 3, 4, 5, 123456, tzinfo=timezone.utc),), (None,)),
            "published_desc": ((date(2025, 1, 15),), (None,)),
            "seq_desc": ((),),
            "relevance": ((0.10000000149011612, 0.25),),
        }
        for sort, keys in cases.items():
            for key in keys:
                for direction in ("after", "before"):
                    token = encode_cursor(sort, direction, key, 42, self.fp)
                    self.assertNotIn("=", token)
                    decoded = decode_cursor(token, sort, self.fp)
                    self.assertEqual(decoded, Cursor(direction, key, 42), (sort, key))

    def test_rejects_malformed_tokens(self):
        good = {"v": 1, "s": "published_desc", "d": "after", "k": ["2025-01-15"], "i": 9, "f": self.fp}
        bad_payloads = [
            {**good, "v": 2}, {**good, "d": "sideways"}, {**good, "i": 0}, {**good, "i": "9"},
            {**good, "i": True}, {**good, "k": "2025-01-15"}, {**good, "k": ["2025-13-01"]},
            {**good, "k": ["2025-01-15T00:00:00"]}, {**good, "k": [1]}, {**good, "k": []},
        ]
        for payload in bad_payloads:
            with self.assertRaises(CatalogueError) as ctx:
                decode_cursor(_token(payload), "published_desc", self.fp)
            self.assertEqual(ctx.exception.reason, "invalid_cursor", payload)
        for token in ("", "not base64!", "x" * 600, base64.urlsafe_b64encode(b"[1,2]").decode(),
                      base64.urlsafe_b64encode(b"\xff\xfe").decode().rstrip("=")):
            with self.assertRaises(CatalogueError):
                decode_cursor(token, "published_desc", self.fp)

    def test_rejects_naive_timestamp_and_non_finite_rank(self):
        naive = _token({"v": 1, "s": "collected_desc", "d": "after", "k": ["2026-01-01T00:00:00"],
                        "i": 1, "f": self.fp})
        with self.assertRaises(CatalogueError):
            decode_cursor(naive, "collected_desc", self.fp)
        raw = '{"v":1,"s":"relevance","d":"after","k":[NaN,0.1],"i":1,"f":"%s"}' % self.fp
        token = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
        with self.assertRaises(CatalogueError):
            decode_cursor(token, "relevance", self.fp)

    def test_cursor_is_bound_to_sort_and_filters(self):
        token = encode_cursor("seq_desc", "after", (), 5, self.fp)
        with self.assertRaises(CatalogueError) as ctx:
            decode_cursor(token, "collected_desc", self.fp)
        self.assertEqual(ctx.exception.reason, "cursor_sort_mismatch")
        other = filters_fingerprint(CatalogueFilters(site_id="other"))
        with self.assertRaises(CatalogueError) as ctx:
            decode_cursor(token, "seq_desc", other)
        self.assertEqual(ctx.exception.reason, "cursor_filter_mismatch")

    def test_fingerprint_ignores_paging_fields(self):
        self.assertEqual(filters_fingerprint(CatalogueFilters(site_id="s", page=3, page_size=50, cursor="x")),
                         self.fp)
        self.assertNotEqual(filters_fingerprint(CatalogueFilters(site_id="s", has_pdf=True)), self.fp)


def _plan(scope=None, key="d.collected_at"):
    return catalogue._Plan(scope, "TRUE", {}, key, "delivery_published_day(d.published_date)")


class KeysetSqlTest(unittest.TestCase):
    def test_first_page_reads_non_null_then_null_segment(self):
        segments = catalogue._segments(_plan(), "collected_desc", None)
        self.assertEqual([s[:2] for s in segments], [
            ("d.collected_at IS NOT NULL", "k DESC, seq_id DESC"),
            ("d.collected_at IS NULL", "seq_id DESC"),
        ])

    def test_after_non_null_key_uses_row_comparison_then_null_tail(self):
        when = datetime(2026, 1, 1, tzinfo=timezone.utc)
        segments = catalogue._segments(_plan(), "collected_desc", Cursor("after", (when,), 7))
        self.assertEqual(segments[0][0],
                         "d.collected_at IS NOT NULL AND (d.collected_at, d.seq_id) < (%(c_key)s::timestamptz, %(c_seq)s::bigint)")
        self.assertEqual(segments[0][2], {"c_key": when, "c_seq": 7})
        self.assertEqual(segments[1][:2], ("d.collected_at IS NULL", "seq_id DESC"))

    def test_after_null_key_stays_in_null_segment(self):
        plan = _plan(key="delivery_published_day(d.published_date)")
        segments = catalogue._segments(plan, "published_desc", Cursor("after", (None,), 7))
        self.assertEqual(segments, [("delivery_published_day(d.published_date) IS NULL "
                                     "AND d.seq_id < %(c_seq)s::bigint", "seq_id DESC", {"c_key": None, "c_seq": 7})])

    def test_before_cursor_walks_backwards(self):
        plan = _plan(key="delivery_published_day(d.published_date)")
        null_back = catalogue._segments(plan, "published_desc", Cursor("before", (None,), 7))
        self.assertEqual([s[1] for s in null_back], ["seq_id ASC", "k ASC, seq_id ASC"])
        self.assertIn("d.seq_id > %(c_seq)s::bigint", null_back[0][0])
        day_back = catalogue._segments(plan, "published_desc", Cursor("before", (date(2025, 1, 1),), 7))
        self.assertEqual(len(day_back), 1)
        self.assertIn("> (%(c_key)s::date, %(c_seq)s::bigint)", day_back[0][0])

    def test_seq_sort(self):
        self.assertEqual(catalogue._segments(_plan(key=None), "seq_desc", Cursor("after", (), 9)),
                         [("d.seq_id < %(c_seq)s::bigint", "seq_id DESC", {"c_seq": 9})])

    def test_scope_shapes(self):
        sql_all = catalogue.segment_sql(_plan(None), "TRUE", "k DESC, seq_id DESC")
        self.assertNotIn("site_id", sql_all)
        self.assertIn("LIMIT %(lim)s", sql_all)
        # one site is read through LATERAL too, so the site-leading index is used
        sql_one = catalogue.segment_sql(_plan(["a"]), "TRUE", "k DESC, seq_id DESC")
        self.assertIn("CROSS JOIN LATERAL", sql_one)
        self.assertIn("d.site_id = sc.site_id", sql_one)
        bitmap_one = catalogue.segment_sql(_plan(["a"]), "TRUE", "k DESC, seq_id DESC", lateral=False)
        self.assertIn("d.site_id = %(scope_site)s", bitmap_one)
        self.assertNotIn("LATERAL", bitmap_one)
        sql_many = catalogue.segment_sql(_plan(["a", "b"]), "TRUE", "k DESC, seq_id DESC")
        self.assertIn("CROSS JOIN LATERAL", sql_many)
        self.assertEqual(sql_many.count("LIMIT %(lim)s"), 2)

    def test_relevance_caps_candidates_and_keysets_on_rank(self):
        sql, params = catalogue.relevance_sql(_plan(["a"], key=None), Cursor("after", (0.5, 0.2), 3))
        self.assertIn("ORDER BY d.seq_id DESC LIMIT %(cap)s", sql)  # deterministic newest-N candidates
        self.assertIn("CROSS JOIN LATERAL", sql)
        self.assertNotIn("LATERAL", catalogue.relevance_sql(_plan(["a"], key=None), None, lateral=False)[0])
        self.assertEqual(params["cap"], catalogue.RELEVANCE_CANDIDATES)
        self.assertIn("(r1, r2, seq_id) < (%(c_r1)s::real, %(c_r2)s::real, %(c_seq)s::bigint)", sql)

    def test_capped_count_limits_rows(self):
        self.assertIn("LIMIT %(cap)s) capped", catalogue.capped_count_sql(_plan(["a", "b"])))


class DefaultSortTest(unittest.TestCase):
    def test_search_defaults_to_published_not_relevance(self):
        self.assertEqual(catalogue.default_sort("연구"), "published_desc")
        self.assertEqual(catalogue.default_sort(None), "collected_desc")
        self.assertEqual(catalogue.default_sort(""), "collected_desc")


class RequestLimitTest(unittest.TestCase):
    def test_q_requires_site(self):
        with self.assertRaises(CatalogueError) as ctx:
            collect_catalogue(FakeConn([]), CatalogueFilters(q="climate", sort="collected_desc"), {})
        self.assertEqual(ctx.exception.reason, "q_requires_site")

    def test_relevance_requires_q_and_site(self):
        with self.assertRaises(CatalogueError) as ctx:
            collect_catalogue(FakeConn([]), CatalogueFilters(site_id="s", sort="relevance"), {})
        self.assertEqual(ctx.exception.reason, "relevance_requires_q")

    def test_deep_offset_requires_cursor(self):
        with self.assertRaises(CatalogueError) as ctx:
            collect_catalogue(FakeConn([]), CatalogueFilters(page=catalogue.MAX_OFFSET_PAGE + 1), {})
        self.assertEqual(ctx.exception.reason, "page_requires_cursor")

    def test_app_returns_422_reasons_without_touching_the_database(self):
        os.environ["DELIVERY_DB_POOL_MIN"] = "0"
        try:
            from fastapi.testclient import TestClient
            from delivery.be.app import create_app
        except ImportError as exc:  # pragma: no cover - optional test dependency
            self.skipTest(str(exc))
        client = TestClient(create_app("postgresql://unused.invalid/none"))
        response = client.get("/documents", params={"q": "climate"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["reason"], "q_requires_site")
        response = client.get("/documents", params={"page": 6})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["reason"], "page_requires_cursor")
        self.assertEqual(client.get("/documents", params={"cursor": "x" * 513}).status_code, 422)


class CountsFallbackTest(unittest.TestCase):
    def _conn(self, counts_table: bool, counted: int, rows=None, estimate=5, n=120):
        rows = rows if rows is not None else [{"seq_id": 3, "k": None}]
        return FakeConn([
            ("EXPLAIN (FORMAT JSON)", [{"QUERY PLAN": [{"Plan": {"Plan Rows": estimate}}]}]),
            ("SELECT 1 AS found", lambda sql, p: rows[:1]),
            ("to_regprocedure", [{"fn": "delivery_published_day(text)"}]),
            ("to_regclass('public.document_counts')", [{"relation": "document_counts" if counts_table else None}]),
            ("EXISTS(SELECT 1 FROM document_counts)", [{"present": True}]),
            ("FROM document_counts c", [{"site_id": "s", "lang": "en", "n": n, "site_name": "S",
                                          "refreshed_at": datetime(2026, 10, 20, tzinfo=timezone.utc)}]),
            ("capped", [{"count": counted}]),
            ("reltuples", [{"n": 4_200_000}]),
            ("SELECT site_id, site_name FROM sites", [{"site_id": "s", "site_name": "S"}]),
            ("WHERE d.seq_id = ANY", [{"seq_id": 3, "site_id": "s", "site_name": "S", "title": "t"}]),
            ("FROM documents d", rows),
        ])

    def test_missing_counts_table_uses_capped_count_and_option_only_facets(self):
        conn = self._conn(False, 7)
        body = collect_catalogue(conn, CatalogueFilters(), {})
        self.assertEqual(body["total"], {"value": 7, "exact": True, "source": "capped_count"})
        self.assertEqual(body["facets_meta"]["source"], "fallback")
        self.assertEqual(body["facets"]["sites"], [{"value": "s", "label": "S", "count": None}])

    def test_missing_counts_table_on_big_table_reports_estimate(self):
        many = [{"seq_id": n, "k": None} for n in range(30, 0, -1)]
        body = collect_catalogue(self._conn(False, catalogue.COUNT_CAP + 1, many), CatalogueFilters(), {})
        self.assertEqual(body["total"], {"value": 4_200_000, "exact": False, "source": "estimate"})
        self.assertIsNone(body["pagination"]["pages"])
        self.assertFalse(body["pagination"]["total_exact"])

    def test_counts_table_supplies_total_and_facets(self):
        conn = self._conn(True, 0)
        body = collect_catalogue(conn, CatalogueFilters(site_id="s"),
                                 {"s": {"country": "한국", "doc_type": "논문"}})
        self.assertEqual(body["total"]["source"], "counts")
        self.assertEqual(body["pagination"]["total"], 120)
        self.assertEqual(body["pagination"]["pages"], 6)
        self.assertEqual(body["facets"]["countries"], [{"value": "한국", "count": 120}])
        self.assertEqual(body["facets"]["sites"], [{"value": "s", "label": "S", "count": 120}])
        self.assertFalse(any("capped" in sql for sql, _ in conn.statements))

    def test_text_query_counts_are_capped_and_flagged(self):
        conn = self._conn(True, catalogue.COUNT_CAP + 1, [{"seq_id": n, "k": None} for n in range(30, 0, -1)])
        body = collect_catalogue(conn, CatalogueFilters(q="x", site_id="s"), {})
        self.assertEqual(body["total"], {"value": catalogue.COUNT_CAP, "exact": False, "source": "capped_count"})
        self.assertFalse(body["facets_meta"]["exact"])
        self.assertEqual(body["facets_meta"]["ignores"], ["q"])

    def test_country_without_matching_site_short_circuits(self):
        conn = self._conn(True, 0)
        body = collect_catalogue(conn, CatalogueFilters(site_id="s", country="프랑스"),
                                 {"s": {"country": "한국", "doc_type": "논문"}})
        self.assertEqual(body["items"], [])
        self.assertEqual(body["total"]["value"], 0)
        self.assertFalse(any("FROM documents d" in sql for sql, _ in conn.statements))

    def test_next_cursor_is_issued_when_more_rows_exist(self):
        rows = [{"seq_id": n, "k": None} for n in (9, 8, 7)]
        conn = self._conn(False, 3, rows)
        conn.routes.insert(0, ("WHERE d.seq_id = ANY", lambda sql, p: [
            {"seq_id": i, "site_id": "s", "site_name": "S", "title": "t"} for i in p["ids"]]))
        body = collect_catalogue(conn, CatalogueFilters(page_size=2, sort="seq_desc"), {})
        self.assertEqual([item["seq_id"] for item in body["items"]], [9, 8])
        self.assertTrue(body["pagination"]["has_next"])
        cursor = decode_cursor(body["pagination"]["next_cursor"], "seq_desc",
                               filters_fingerprint(CatalogueFilters(sort="seq_desc")))
        self.assertEqual(cursor, Cursor("after", (), 8))
        self.assertIsNone(body["pagination"]["prev_cursor"])

    def test_common_term_reports_estimate_without_counting(self):
        rows = [{"seq_id": n, "k": None} for n in range(30, 0, -1)]
        conn = self._conn(True, 0, rows, estimate=320_000)
        body = collect_catalogue(conn, CatalogueFilters(q="연구", site_id="s"), {})
        self.assertEqual(body["total"], {"value": 320_000, "exact": False, "source": "estimate"})
        self.assertFalse(any("capped" in sql for sql, _ in conn.statements))
        self.assertTrue(body["pagination"]["has_next"])

    def test_wrong_estimate_is_corrected_by_a_short_result(self):
        conn = self._conn(True, 0, [{"seq_id": 4, "k": None}], estimate=320_000)
        body = collect_catalogue(conn, CatalogueFilters(q="연구", site_id="s", sort="seq_desc"), {})
        self.assertEqual(body["total"], {"value": 1, "exact": True, "source": "listed"})

    def test_stale_zero_count_is_verified_by_probe(self):
        conn = self._conn(True, 0, [{"seq_id": 3, "k": None}], n=0)
        body = collect_catalogue(conn, CatalogueFilters(site_id="s", has_pdf=True), {})
        self.assertTrue(any("SELECT 1 AS found" in sql for sql, _ in conn.statements))
        self.assertEqual([item["seq_id"] for item in body["items"]], [3])
        self.assertEqual(body["total"]["value"], 1)
        self.assertFalse(body["total"]["exact"])
        empty = collect_catalogue(self._conn(True, 0, [], n=0), CatalogueFilters(site_id="s", has_pdf=True), {})
        self.assertEqual(empty["items"], [])
        self.assertEqual(empty["total"]["value"], 0)

    def test_small_path_overflow_falls_back_and_marks_total_inexact(self):
        rows = [{"seq_id": n, "k": None} for n in range(catalogue.MATERIALIZE_MAX + 1, 0, -1)]
        conn = self._conn(True, 0, rows, n=120)
        body = collect_catalogue(conn, CatalogueFilters(site_id="s", sort="seq_desc"), {})
        self.assertFalse(body["total"]["exact"])
        self.assertGreater(body["total"]["value"], catalogue.MATERIALIZE_MAX)
        self.assertTrue(any("ORDER BY seq_id DESC LIMIT" in sql for sql, _ in conn.statements))


class SearchClauseTest(unittest.TestCase):
    def _where(self, q):
        conn = FakeConn([("to_regprocedure", [{"fn": None}])])
        return catalogue._build_plan(conn, CatalogueFilters(q=q, site_id="s"), {}).where

    def test_short_terms_skip_trigram_ilike(self):
        self.assertNotIn("ILIKE", self._where("연구"))
        self.assertIn("fts @@", self._where("연구"))
        self.assertIn("ILIKE", self._where("연구원"))

    def test_cursor_seq_id_is_bounded_and_cast(self):
        fp = filters_fingerprint(CatalogueFilters())
        token = _token({"v": 1, "s": "seq_desc", "d": "after", "k": [], "i": 2 ** 63, "f": fp})
        with self.assertRaises(CatalogueError):
            decode_cursor(token, "seq_desc", fp)
        self.assertIn("%(c_seq)s::bigint", catalogue._segments(_plan(key=None), "seq_desc",
                                                               Cursor("after", (), 9))[0][0])
        published = catalogue._segments(_plan(key="delivery_published_day(d.published_date)"), "published_desc",
                                        Cursor("after", (date(2025, 1, 1),), 9))
        self.assertIn("(%(c_key)s::date, %(c_seq)s::bigint)", published[0][0])


class StrategyTest(unittest.TestCase):
    def test_unbudgeted_walk_never_falls_back(self):
        strategy = catalogue._Strategy("walk", None)

        class Plain:  # no transaction(): runs the statement directly, no budget
            statements = []

            def execute(self, sql, params=None, **_kwargs):
                self.statements.append(sql)
                return _Result([{"seq_id": 1}])

        rows = strategy.run(Plain(), lambda mode: "SELECT walk", {})
        self.assertEqual(rows, [{"seq_id": 1}])
        self.assertEqual(strategy.mode, "walk")
        self.assertFalse(strategy.fell_back)

    def test_budget_exhaustion_switches_mode_for_the_rest_of_the_request(self):
        from contextlib import contextmanager
        from psycopg.errors import QueryCanceled

        class TxConn:
            def __init__(self):
                self.statements, self.cancel_next = [], True

            @contextmanager
            def transaction(self, force_rollback=False):
                self.statements.append(("SAVEPOINT", force_rollback))
                yield

            def execute(self, sql, params=None, **_kwargs):
                self.statements.append((sql, params))
                if sql.startswith("SELECT") and self.cancel_next:
                    self.cancel_next = False
                    raise QueryCanceled("budget")
                return _Result([{"seq_id": 1, "k": None}])

        conn = TxConn()
        strategy = catalogue._Strategy("walk")
        self.assertEqual(strategy.run(conn, lambda mode: "SELECT walk", {}), [{"seq_id": 1, "k": None}])
        self.assertEqual(strategy.mode, "bitmap")
        sqls = [sql for sql, _ in conn.statements]
        self.assertIn("SET LOCAL enable_bitmapscan = off", sqls)
        self.assertIn(f"SET LOCAL statement_timeout = {catalogue.STRATEGY_BUDGET_MS}", sqls)
        self.assertIn("SET LOCAL enable_indexscan = off", sqls)
        self.assertTrue(all(flag for name, flag in conn.statements if name == "SAVEPOINT"))
        conn.statements.clear()
        strategy.run(conn, lambda mode: "SELECT again", {})
        self.assertNotIn(f"SET LOCAL statement_timeout = {catalogue.STRATEGY_BUDGET_MS}",
                         [sql for sql, _ in conn.statements])


if __name__ == "__main__":
    unittest.main()

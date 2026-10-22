"""Read-only document catalogue queries for the delivery API.

Browsing is designed to stay fast at 100M documents on PostgreSQL alone:

* Lists use keyset pagination (``cursor``) over ``(sort key, seq_id)`` so every
  page is an index range scan of ``page_size + 1`` rows, whatever the depth.
  ``page`` (OFFSET) is only accepted for the first ``MAX_OFFSET_PAGE`` pages.
* Totals and facet counts come from the pre-computed ``document_counts`` table
  (see delivery/be/counts.py). Filters that table cannot express (q, date
  ranges) get a capped count instead: exact up to ``COUNT_CAP`` and
  ``{"value": COUNT_CAP, "exact": false}`` beyond.
* country/doc_type are resolved to site lists from the taxonomy in Python; a
  multi-site scope is merged per site with LATERAL index scans, so the cost is
  bounded by ``sites x page_size`` rows instead of the sites' document count.
* Nullable sort keys (collected_at, published day) are ordered
  ``DESC NULLS LAST`` by reading the non-NULL segment first and the NULL
  segment second, each from its own index (deploy/delivery-bundle/sql/
  20261020-catalogue-indexes.sql).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone
from pathlib import Path

from delivery.be.site_names import display_site_name

TAXONOMY_PATH = Path(__file__).resolve().parents[1] / "data" / "site-taxonomy.json"

PAGE_SIZE_MAX = 100
MAX_OFFSET_PAGE = 5
COUNT_CAP = 10_000
# Results known (by capped count) to have at most this many rows are read in
# one pass and ordered in Python instead of walking an ordered index.
MATERIALIZE_MAX = 2_000
# q / date-range requests cannot use the counts table, and the best way to
# read them depends on how many rows match:
#   many matches  -> "walk": the sort-order index with the filter stops after
#                    page_size hits (cost ~ page_size / selectivity);
#   few matches   -> "bitmap": GIN / date-range bitmap, then a top-N sort
#                    (cost ~ matches).
# The choice uses the planner's row estimate (EXPLAIN, ~1 ms). Because the
# estimate can be wrong (and a walk is slow when the matches sit far down the
# sort order, e.g. an old published year in a collected-order walk), the first
# choice runs under a statement_timeout budget and the other path is the
# fallback. Planner settings are SET LOCAL inside a savepoint that is always
# rolled back, so nothing leaks into later statements.
STRATEGY_ESTIMATE_SPLIT = int(os.environ.get("DELIVERY_CATALOGUE_WALK_MIN_ROWS", "50000"))
WALK_MIN_SELECTIVITY = float(os.environ.get("DELIVERY_CATALOGUE_WALK_MIN_SELECTIVITY", "0.05"))
# Above this estimate a walk that exceeds its budget keeps walking instead of
# falling back to bitmap + sort over every match.
BITMAP_FALLBACK_MAX = int(os.environ.get("DELIVERY_CATALOGUE_BITMAP_FALLBACK_MAX", "200000"))
CROSS_RANGE_BITMAP_MAX = int(os.environ.get("DELIVERY_CATALOGUE_CROSS_RANGE_BITMAP_MAX", "200000"))
STRATEGY_BUDGET_MS = int(os.environ.get("DELIVERY_CATALOGUE_BUDGET_MS", "150"))
PROBE_BUDGET_MS = int(os.environ.get("DELIVERY_CATALOGUE_PROBE_BUDGET_MS", "100"))
COUNT_BUDGET_MS = int(os.environ.get("DELIVERY_CATALOGUE_COUNT_BUDGET_MS", "100"))
_MODE_SETTINGS = {
    "walk": (("enable_bitmapscan", "off"), ("enable_seqscan", "off")),
    "bitmap": (("enable_indexscan", "off"), ("enable_indexonlyscan", "off"), ("enable_seqscan", "off")),
}
_OTHER_MODE = {"walk": "bitmap", "bitmap": "walk"}
# Relevance ranks only the newest N matches of the site, not every match:
# ts_rank_cd must read each candidate's tsvector, so an uncapped rank costs
# O(matches). Trade-off: for a term with more than N matches in the site, a
# better-ranked but older document is not shown. Newly collected matches
# entering the window between page requests can shift relevance pages.
RELEVANCE_CANDIDATES = max(1, int(os.environ.get("DELIVERY_RELEVANCE_CANDIDATES", "1000")))
SORTS = ("relevance", "published_desc", "collected_desc", "seq_desc")
PUBLISHED_DAY_FUNCTION = "delivery_published_day"
CURSOR_MAX_LENGTH = 512
TRIGRAM_MIN_LENGTH = 3
BIGINT_MAX = 2 ** 63 - 1
OTHER = "기타"


def _safe_date(column: str) -> str:
    """PostgreSQL 16 expression that never casts an invalid free-form date."""
    return (f"CASE WHEN {column} ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$' "
            f"AND pg_input_is_valid({column}, 'date') THEN {column}::date END")


def default_sort(q: str | None) -> str:
    """Sort used when the client chose none.

    With q this is published_desc, not relevance: published walks stay fast on
    every term, while relevance has to rank its candidate set and, for very
    common terms in a multi-million-row site, can take seconds. Relevance stays
    available when asked for explicitly.
    """
    return "published_desc" if q else "collected_desc"


def load_taxonomy(path: Path = TAXONOMY_PATH) -> dict[str, dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["sites"]


class CatalogueError(ValueError):
    """A request the catalogue refuses; ``reason`` is a stable machine code."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class CatalogueFilters:
    q: str | None = None
    page: int = 1
    page_size: int = 20
    site_id: str | None = None
    country: str | None = None
    doc_type: str | None = None
    lang: str | None = None
    published_from: date | None = None
    published_to: date | None = None
    collected_from: date | None = None
    collected_to: date | None = None
    has_pdf: bool | None = None
    has_text: bool | None = None
    sort: str = "collected_desc"
    cursor: str | None = None


@dataclass(frozen=True)
class Cursor:
    direction: str  # "after" (next page) or "before" (previous page)
    key: tuple
    seq_id: int


# --------------------------------------------------------------------------
# Cursor encoding
# --------------------------------------------------------------------------

def filters_fingerprint(filters: CatalogueFilters) -> str:
    """Short digest binding a cursor to the filter set and sort it came from."""
    scope = asdict(filters)
    for name in ("page", "page_size", "cursor"):
        scope.pop(name)
    canonical = json.dumps(scope, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _key_to_json(sort: str, key: tuple) -> list:
    if sort == "collected_desc":
        return [key[0].isoformat() if key[0] is not None else None]
    if sort == "published_desc":
        return [key[0].isoformat() if key[0] is not None else None]
    if sort == "relevance":
        return [float(key[0]), float(key[1])]
    return []


def encode_cursor(sort: str, direction: str, key: tuple, seq_id: int, fingerprint: str) -> str:
    payload = {"v": 1, "s": sort, "d": direction, "k": _key_to_json(sort, key),
               "i": int(seq_id), "f": fingerprint}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _invalid(message: str) -> CatalogueError:
    return CatalogueError("invalid_cursor", message)


def decode_cursor(token: str, sort: str, fingerprint: str) -> Cursor:
    if not token or len(token) > CURSOR_MAX_LENGTH:
        raise _invalid("cursor is empty or too long")
    if any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for ch in token):
        raise _invalid("cursor is not base64url")
    try:
        payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise _invalid("cursor is not decodable") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise _invalid("unsupported cursor version")
    if payload.get("s") != sort:
        raise CatalogueError("cursor_sort_mismatch", "cursor was issued for a different sort")
    if payload.get("f") != fingerprint:
        raise CatalogueError("cursor_filter_mismatch", "cursor was issued for different filters")
    direction = payload.get("d")
    if direction not in ("after", "before"):
        raise _invalid("cursor direction is invalid")
    seq_id = payload.get("i")
    if not isinstance(seq_id, int) or isinstance(seq_id, bool) or not 0 < seq_id <= BIGINT_MAX:
        raise _invalid("cursor seq_id is invalid")
    raw_key = payload.get("k")
    if not isinstance(raw_key, list):
        raise _invalid("cursor key is invalid")
    try:
        key = _key_from_json(sort, raw_key)
    except (TypeError, ValueError) as exc:
        raise _invalid("cursor key is invalid") from exc
    return Cursor(direction, key, seq_id)


def _key_from_json(sort: str, raw: list) -> tuple:
    if sort == "seq_desc":
        if raw:
            raise ValueError("seq cursor has no key")
        return ()
    if sort == "relevance":
        if len(raw) != 2 or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                    and math.isfinite(v) for v in raw):
            raise ValueError("relevance key")
        return (float(raw[0]), float(raw[1]))
    if len(raw) != 1 or not (raw[0] is None or isinstance(raw[0], str)):
        raise ValueError("key shape")
    if raw[0] is None:
        return (None,)
    if sort == "collected_desc":
        value = datetime.fromisoformat(raw[0])
        if value.tzinfo is None:
            raise ValueError("timestamp without zone")
        return (value,)
    if len(raw[0]) != 10:
        raise ValueError("date shape")
    return (date.fromisoformat(raw[0]),)


# --------------------------------------------------------------------------
# Query construction
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class _Plan:
    """Everything derived from the filters that the SQL builders need."""
    scope_sites: list[str] | None  # None = all sites
    where: str                     # predicates on documents d (no scope)
    params: dict
    key_expr: str | None           # sort key expression (None for seq/relevance)
    published_expr: str


def _published_expr(conn) -> str:
    row = conn.execute("SELECT to_regprocedure(%s) AS fn",
                       (f"public.{PUBLISHED_DAY_FUNCTION}(text)",)).fetchone()
    if row is not None and row["fn"] is not None:
        return f"{PUBLISHED_DAY_FUNCTION}(d.published_date)"
    return _safe_date("d.published_date")


def _resolve_scope(conn, filters: CatalogueFilters,
                   taxonomy: dict[str, dict[str, str]]) -> list[str] | None:
    """Translate site_id/country/doc_type into an explicit site list."""
    if not (filters.country or filters.doc_type):
        return [filters.site_id] if filters.site_id else None

    def matches(site: str) -> bool:
        entry = taxonomy.get(site) or {}
        country = entry.get("country", OTHER)
        doc_type = entry.get("doc_type", OTHER)
        return ((not filters.country or country == filters.country)
                and (not filters.doc_type or doc_type == filters.doc_type))

    if filters.site_id:
        return [filters.site_id] if matches(filters.site_id) else []
    needs_untaxonomised = filters.country == OTHER or filters.doc_type == OTHER
    candidates = set(taxonomy)
    if needs_untaxonomised:
        candidates |= {row["site_id"] for row in conn.execute("SELECT site_id FROM sites").fetchall()}
    return sorted(site for site in candidates if matches(site))


def _build_plan(conn, filters: CatalogueFilters, taxonomy) -> _Plan:
    published = _published_expr(conn)
    params: dict = {}
    clauses: list[str] = []
    if filters.q:
        params["q"] = filters.q
        if len(filters.q) >= TRIGRAM_MIN_LENGTH:
            params["q_like"] = f"%{filters.q}%"
            clauses.append("(d.fts @@ plainto_tsquery('simple', %(q)s) OR d.title ILIKE %(q_like)s)")
        else:
            # pg_trgm extracts no trigram from a 1-2 character LIKE pattern, so
            # the ILIKE branch could only be answered by reading every title of
            # the site. Short terms (e.g. '연구') match as whole fts tokens only.
            clauses.append("d.fts @@ plainto_tsquery('simple', %(q)s)")
    if filters.lang:
        params["lang"] = filters.lang
        if filters.lang == "unknown":
            clauses.append("NOT EXISTS (SELECT 1 FROM document_lang dl WHERE dl.seq_id = d.seq_id"
                           " AND dl.lang <> 'unknown')")
        else:
            clauses.append("EXISTS (SELECT 1 FROM document_lang dl WHERE dl.seq_id = d.seq_id"
                           " AND dl.lang = %(lang)s)")
    if filters.published_from is not None:
        params["published_from"] = filters.published_from
        clauses.append(f"{published} >= %(published_from)s")
    if filters.published_to is not None:
        params["published_to"] = filters.published_to
        clauses.append(f"{published} <= %(published_to)s")
    if filters.collected_from is not None:
        params["collected_from"] = filters.collected_from
        clauses.append("d.collected_at >= %(collected_from)s::date")
    if filters.collected_to is not None:
        params["collected_to"] = filters.collected_to
        clauses.append("d.collected_at < (%(collected_to)s::date + interval '1 day')")
    if filters.has_pdf is not None:
        clauses.append("d.pdf_downloaded <> 0" if filters.has_pdf else "d.pdf_downloaded = 0")
    if filters.has_text is not None:
        clauses.append("d.text_extracted <> 0" if filters.has_text else "d.text_extracted = 0")
    key_expr = {"collected_desc": "d.collected_at", "published_desc": published}.get(filters.sort)
    return _Plan(_resolve_scope(conn, filters, taxonomy), " AND ".join(clauses) or "TRUE",
                 params, key_expr, published)


def _segments(plan: _Plan, sort: str, cursor: Cursor | None) -> list[tuple[str, str, dict]]:
    """(predicate, ORDER BY, params) per index segment, in reading order.

    The visible order is ``key DESC NULLS LAST, seq_id DESC``. Each segment's
    ORDER BY matches one index exactly (``key DESC`` = the index's default
    NULLS FIRST, which is irrelevant inside a NOT NULL segment), so PostgreSQL
    reads it as an ordered index range and stops after LIMIT rows.
    """
    forward = cursor is None or cursor.direction == "after"
    if sort == "seq_desc":
        if cursor is None:
            return [("TRUE", "seq_id DESC", {})]
        op, order = ("<", "seq_id DESC") if forward else (">", "seq_id ASC")
        return [(f"d.seq_id {op} %(c_seq)s::bigint", order, {"c_seq": cursor.seq_id})]
    k = plan.key_expr
    # Explicit casts keep every comparison on the indexed types (a crafted
    # numeric/text parameter must not turn an index range into a filter).
    ck = "%(c_key)s::timestamptz" if sort == "collected_desc" else "%(c_key)s::date"
    desc, asc = "k DESC, seq_id DESC", "k ASC, seq_id ASC"
    if cursor is None:
        return [(f"{k} IS NOT NULL", desc, {}), (f"{k} IS NULL", "seq_id DESC", {})]
    params = {"c_key": cursor.key[0], "c_seq": cursor.seq_id}
    if forward:
        if cursor.key[0] is None:
            return [(f"{k} IS NULL AND d.seq_id < %(c_seq)s::bigint", "seq_id DESC", params)]
        return [(f"{k} IS NOT NULL AND ({k}, d.seq_id) < ({ck}, %(c_seq)s::bigint)", desc, params),
                (f"{k} IS NULL", "seq_id DESC", {})]
    if cursor.key[0] is None:
        return [(f"{k} IS NULL AND d.seq_id > %(c_seq)s::bigint", "seq_id ASC", params),
                (f"{k} IS NOT NULL", asc, {})]
    return [(f"{k} IS NOT NULL AND ({k}, d.seq_id) > ({ck}, %(c_seq)s::bigint)", asc, params)]


def segment_sql(plan: _Plan, predicate: str, order: str, lateral: bool = True) -> str:
    """One ordered, limited index read, per site through LATERAL.

    Even a single site goes through ``unnest(ARRAY[site]) CROSS JOIN LATERAL``:
    with ``site_id = 'const'`` PostgreSQL treats ORDER BY site_id as redundant
    and, when the site is most of the table, prefers walking a global index
    (collected_at / seq_id) and filtering site_id. If the newest rows belong
    to other sites that filter discards hundreds of thousands of rows. As a
    LATERAL parameter, site_id is estimated at 1/ndistinct, so the planner
    walks the site-leading index (site_id, key DESC, seq_id DESC) with the
    keyset row comparison as an index condition.

    ``lateral=False`` (bitmap reads of one site) keeps ``site_id = const``:
    there the 1/ndistinct estimate would make the planner AND a bitmap of the
    whole site into an otherwise small fts/date bitmap.
    """
    key = plan.key_expr or "NULL::int"
    inner_where = f"{plan.where} AND {predicate}"
    if plan.scope_sites is None:
        return (f"SELECT d.seq_id, {key} AS k FROM documents d WHERE {inner_where} "
                f"ORDER BY {order} LIMIT %(lim)s")
    if len(plan.scope_sites) == 1 and not lateral:
        return (f"SELECT d.seq_id, {key} AS k FROM documents d "
                f"WHERE d.site_id = %(scope_site)s AND {inner_where} ORDER BY {order} LIMIT %(lim)s")
    return (f"SELECT x.seq_id, x.k FROM unnest(%(scope_sites)s::text[]) AS sc(site_id) "
            f"CROSS JOIN LATERAL (SELECT d.seq_id, {key} AS k FROM documents d "
            f"WHERE d.site_id = sc.site_id AND {inner_where} ORDER BY {order} LIMIT %(lim)s) x "
            f"ORDER BY {order} LIMIT %(lim)s")


def _scope_params(plan: _Plan) -> dict:
    if plan.scope_sites is None:
        return {}
    if len(plan.scope_sites) == 1:
        return {"scope_site": plan.scope_sites[0]}
    return {"scope_sites": plan.scope_sites}


# prepare=False on the parameter-sensitive reads: psycopg auto-prepares a
# statement after 5 executions, and a generic plan cannot tell a 3.4M-row site
# from a 200-row one, so every read here is planned with its actual values.
def _query(conn, sql: str, params: dict, mode: str | None = None, budget_ms: int | None = None):
    """Rows of ``sql`` under ``mode`` planner settings; None if the budget ran out."""
    settings = _MODE_SETTINGS.get(mode, ())
    if (not settings and budget_ms is None) or not hasattr(conn, "transaction"):
        return conn.execute(sql, params, prepare=False).fetchall()
    from psycopg.errors import QueryCanceled
    try:
        with conn.transaction(force_rollback=True):
            for name, value in settings:
                conn.execute(f"SET LOCAL {name} = {value}")
            if budget_ms is not None:
                conn.execute(f"SET LOCAL statement_timeout = {int(budget_ms)}")
            return conn.execute(sql, params, prepare=False).fetchall()
    except QueryCanceled:
        if budget_ms is None:
            raise
        return None


class _Strategy:
    """Per-request read mode; switches for good once a budget runs out.

    ``budget_ms=None`` runs the first choice without a budget and never falls
    back: used for walks over huge estimates, where bitmap+sort over every
    match would be far slower than letting the walk finish.
    """

    def __init__(self, mode: str | None, budget_ms: int | None = STRATEGY_BUDGET_MS):
        self.mode = mode
        self.budget_ms = budget_ms
        self.fell_back = False

    def run(self, conn, sql_for, params: dict) -> list:
        if self.mode is None:
            return _query(conn, sql_for(None), params)
        if self.budget_ms is None:
            return _query(conn, sql_for(self.mode), params, self.mode)
        if not self.fell_back:
            rows = _query(conn, sql_for(self.mode), params, self.mode, self.budget_ms)
            if rows is not None:
                return rows
            self.fell_back = True
            self.mode = _OTHER_MODE[self.mode]
        return _query(conn, sql_for(self.mode), params, self.mode)


def _read_segments(conn, plan: _Plan, sort: str, cursor: Cursor | None, limit: int,
                   strategy: _Strategy) -> list[tuple]:
    rows: list[tuple] = []
    for predicate, order, extra in _segments(plan, sort, cursor):
        need = limit - len(rows)
        if need <= 0:
            break
        params = {**plan.params, **_scope_params(plan), **extra, "lim": need, "scope_sites": plan.scope_sites}
        for row in strategy.run(conn, lambda mode: segment_sql(plan, predicate, order, mode != "bitmap"), params):
            rows.append(((row["k"],) if plan.key_expr else (), row["seq_id"]))
    return rows


def relevance_sql(plan: _Plan, cursor: Cursor | None, lateral: bool = True) -> tuple[str, dict]:
    """Rank the newest RELEVANCE_CANDIDATES matches of the site.

    The candidate set is deterministic (seq_id DESC), so relevance pages are
    stable. Common terms are read by walking (site_id, seq_id DESC) until N
    hits ("walk", through LATERAL as in segment_sql); rare terms come from the
    fts/trigram bitmap followed by a top-N sort ("bitmap", site_id = const).
    """
    params = {**plan.params, **_scope_params(plan), "cap": RELEVANCE_CANDIDATES}
    keyset, order = "TRUE", "r1 DESC, r2 DESC, seq_id DESC"
    if cursor is not None:
        params.update(c_r1=cursor.key[0], c_r2=cursor.key[1], c_seq=cursor.seq_id)
        op = "<" if cursor.direction == "after" else ">"
        keyset = f"(r1, r2, seq_id) {op} (%(c_r1)s::real, %(c_r2)s::real, %(c_seq)s::bigint)"
        if cursor.direction == "before":
            order = "r1 ASC, r2 ASC, seq_id ASC"
    if lateral:
        candidates = f"""
          SELECT x.seq_id, x.fts, x.title FROM unnest(ARRAY[%(scope_site)s]::text[]) AS sc(site_id)
          CROSS JOIN LATERAL (
            SELECT d.seq_id, d.fts, d.title FROM documents d
            WHERE d.site_id = sc.site_id AND {plan.where}
            ORDER BY d.seq_id DESC LIMIT %(cap)s) x"""
    else:
        candidates = f"""
          SELECT d.seq_id, d.fts, d.title FROM documents d
          WHERE d.site_id = %(scope_site)s AND {plan.where}
          ORDER BY d.seq_id DESC LIMIT %(cap)s"""
    return f"""
        WITH candidates AS MATERIALIZED ({candidates}), ranked AS (
          SELECT seq_id, ts_rank_cd(fts, plainto_tsquery('simple', %(q)s)) AS r1,
                 similarity(title, %(q)s) AS r2
          FROM candidates
        )
        SELECT seq_id, r1, r2 FROM ranked WHERE {keyset} ORDER BY {order} LIMIT %(lim)s
    """, params


def _read_relevance(conn, plan: _Plan, cursor: Cursor | None, limit: int,
                    strategy: _Strategy) -> list[tuple]:
    _, params = relevance_sql(plan, cursor)
    params["lim"] = limit
    rows = strategy.run(conn, lambda mode: relevance_sql(plan, cursor, mode != "bitmap")[0], params)
    return [((row["r1"], row["r2"]), row["seq_id"]) for row in rows]


def _read(conn, plan, sort, cursor, limit, strategy: _Strategy):
    if sort == "relevance":
        return _read_relevance(conn, plan, cursor, limit, strategy)
    return _read_segments(conn, plan, sort, cursor, limit, strategy)


def _estimate_rows(conn, plan: _Plan) -> int:
    """Planner estimate of matching rows (planning only, nothing executes)."""
    sql = f"EXPLAIN (FORMAT JSON) SELECT 1 FROM documents d WHERE {_scope_sql(plan)} AND {plan.where}"
    plan_json = conn.execute(sql, {**plan.params, **_scope_params(plan)}, prepare=False).fetchone()["QUERY PLAN"]
    if isinstance(plan_json, str):
        plan_json = json.loads(plan_json)
    return int(plan_json[0]["Plan"]["Plan Rows"])


def _display_key(sort: str, key: tuple, seq_id: int) -> tuple:
    """Python mirror of the SQL order; larger tuples are displayed first."""
    if sort in ("collected_desc", "published_desc"):
        return (key[0] is not None, key[0], seq_id)
    return (seq_id,)


def _read_materialized(conn, plan: _Plan, sort: str, cursor: Cursor | None, limit: int,
                       mode: str | None = None) -> list[tuple] | None:
    """Read every match (expected <= MATERIALIZE_MAX) and keyset in Python.

    Returns None when more than MATERIALIZE_MAX rows match after all.

    Returns rows in the same order as _read_segments: display order for
    forward reads, closest-first (reversed) for ``before`` reads.
    """
    key = plan.key_expr or "NULL::int"
    rows = _query(conn, f"SELECT d.seq_id, {key} AS k FROM documents d WHERE {_scope_sql(plan)} "
                        f"AND {plan.where} LIMIT %(cap)s",
                  {**plan.params, **_scope_params(plan), "cap": MATERIALIZE_MAX + 1}, mode)
    if len(rows) > MATERIALIZE_MAX:
        return None
    keyed = [(((row["k"],) if plan.key_expr else ()), row["seq_id"]) for row in rows]
    keyed.sort(key=lambda item: _display_key(sort, item[0], item[1]), reverse=True)
    if cursor is None:
        return keyed[:limit]
    mark = _display_key(sort, cursor.key, cursor.seq_id)
    if cursor.direction == "after":
        return [item for item in keyed if _display_key(sort, item[0], item[1]) < mark][:limit]
    before = [item for item in keyed if _display_key(sort, item[0], item[1]) > mark]
    return list(reversed(before))[:limit]


def _scope_sql(plan: _Plan) -> str:
    if plan.scope_sites is None:
        return "TRUE"
    if len(plan.scope_sites) == 1:
        return "d.site_id = %(scope_site)s"
    return "d.site_id = ANY(%(scope_sites)s::text[])"


def capped_count_sql(plan: _Plan) -> str:
    return (f"SELECT count(*) AS count FROM (SELECT 1 FROM documents d WHERE {_scope_sql(plan)} "
            f"AND {plan.where} LIMIT %(cap)s) capped")


# --------------------------------------------------------------------------
# Counts table
# --------------------------------------------------------------------------

def _counts_rows(conn, filters: CatalogueFilters, scope_sites: list[str] | None) -> list[dict] | None:
    """Per (site_id, lang) counts for the non-text filters, None when unavailable."""
    from delivery.be.counts import counts_available
    if not counts_available(conn):
        return None
    clauses, params = ["TRUE"], {}
    if scope_sites is not None:
        clauses.append("site_id = ANY(%(sites)s::text[])")
        params["sites"] = scope_sites
    if filters.lang:
        clauses.append("lang = %(lang)s")
        params["lang"] = filters.lang
    if filters.has_pdf is not None:
        clauses.append("has_pdf IS TRUE" if filters.has_pdf else "has_pdf IS FALSE")
    if filters.has_text is not None:
        clauses.append("has_text IS TRUE" if filters.has_text else "has_text IS FALSE")
    return [dict(row) for row in conn.execute(f"""
        SELECT c.site_id, c.lang, sum(c.n)::bigint AS n, min(c.refreshed_at) AS refreshed_at,
               max(s.site_name) AS site_name
        FROM document_counts c LEFT JOIN sites s USING (site_id)
        WHERE {' AND '.join(clauses)} GROUP BY c.site_id, c.lang""", params).fetchall()]


def _facets_from_counts(rows: list[dict], taxonomy) -> dict[str, list[dict]]:
    buckets: dict[str, dict[str, int]] = {"countries": {}, "doc_types": {}, "sites": {}, "languages": {}}
    labels: dict[str, str] = {}
    for row in rows:
        entry = taxonomy.get(row["site_id"]) or {}
        n = int(row["n"])
        if n == 0:
            continue
        for facet, value in (("countries", entry.get("country", OTHER)),
                             ("doc_types", entry.get("doc_type", OTHER)),
                             ("sites", row["site_id"]), ("languages", row["lang"])):
            buckets[facet][value] = buckets[facet].get(value, 0) + n
        labels[row["site_id"]] = display_site_name(row["site_name"]) or row["site_id"]

    def ordered(facet: str) -> list[dict]:
        items = sorted(buckets[facet].items(), key=lambda item: (-item[1], item[0]))
        if facet == "sites":
            return [{"value": v, "label": labels[v], "count": c} for v, c in items]
        return [{"value": v, "count": c} for v, c in items]

    return {facet: ordered(facet) for facet in buckets}


def _fallback_facets(conn, taxonomy) -> dict[str, list[dict]]:
    """Counts table missing/empty: option lists without counts (count=None)."""
    sites = conn.execute("SELECT site_id, site_name FROM sites ORDER BY site_id").fetchall()
    countries = sorted({v["country"] for v in taxonomy.values()} | {OTHER})
    doc_types = sorted({v["doc_type"] for v in taxonomy.values()} | {OTHER})
    return {
        "countries": [{"value": v, "count": None} for v in countries],
        "doc_types": [{"value": v, "count": None} for v in doc_types],
        "sites": [{"value": r["site_id"], "label": display_site_name(r["site_name"]), "count": None} for r in sites],
        "languages": [],
    }


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def _hydrate(conn, ids: list[int], taxonomy) -> list[dict]:
    if not ids:
        return []
    rows = conn.execute("""
        SELECT d.seq_id, d.site_id, s.site_name, d.title, d.published_date, d.published_date_raw,
               d.published_date_status, d.published_date_precision,
               d.collected_at, d.authors, d.publisher, d.journal, COALESCE(dl.lang, 'unknown') AS lang,
               d.pdf_downloaded <> 0 AS has_pdf, d.text_extracted <> 0 AS has_text,
               EXISTS (SELECT 1 FROM document_translations tr
                       WHERE tr.seq_id = d.seq_id AND tr.state = 'completed') AS has_translation,
               d.meta_url
        FROM documents d JOIN sites s USING (site_id) LEFT JOIN document_lang dl USING (seq_id)
        WHERE d.seq_id = ANY(%(ids)s::bigint[])
    """, {"ids": ids}).fetchall()
    by_id = {}
    for row in rows:
        entry = taxonomy.get(row["site_id"]) or {}
        item = {"seq_id": row["seq_id"], "site_id": row["site_id"], "site_name": display_site_name(row["site_name"]),
                "country": entry.get("country", OTHER), "doc_type": entry.get("doc_type", OTHER)}
        item.update({key: value for key, value in dict(row).items() if key not in item})
        by_id[row["seq_id"]] = item
    return [by_id[i] for i in ids if i in by_id]


def collect_catalogue(conn, filters: CatalogueFilters,
                      taxonomy: dict[str, dict[str, str]] | None = None) -> dict:
    taxonomy = taxonomy if taxonomy is not None else load_taxonomy()
    if filters.sort not in SORTS:
        raise CatalogueError("invalid_sort", "unknown sort")
    if filters.sort == "relevance" and not (filters.q and filters.site_id):
        raise CatalogueError("relevance_requires_q", "relevance sort requires q and site_id")
    if filters.q and not filters.site_id:
        raise CatalogueError("q_requires_site", "q requires site_id")
    fingerprint = filters_fingerprint(filters)
    cursor = decode_cursor(filters.cursor, filters.sort, fingerprint) if filters.cursor else None
    if cursor is None and filters.page > MAX_OFFSET_PAGE:
        raise CatalogueError("page_requires_cursor",
                             f"page > {MAX_OFFSET_PAGE} requires cursor pagination")
    plan = _build_plan(conn, filters, taxonomy)
    size = filters.page_size
    offset = 0 if cursor else (filters.page - 1) * size
    empty_scope = plan.scope_sites == []

    # Totals first: a known-empty or known-small result changes how rows are read.
    counts = None if empty_scope else _counts_rows(conn, filters, plan.scope_sites)
    counts_as_of = min((row["refreshed_at"] for row in counts), default=None) if counts else None
    needs_live_count = bool(filters.q or filters.published_from or filters.published_to
                            or filters.collected_from or filters.collected_to)
    mode, estimate = None, 0  # read strategy; only q / date-range requests choose one
    if empty_scope:
        total = {"value": 0, "exact": True, "source": "empty_scope"}
    elif counts is not None and not needs_live_count:
        total = {"value": sum(int(row["n"]) for row in counts), "exact": True, "source": "counts",
                 "as_of": counts_as_of}
    elif needs_live_count:
        # Many estimated matches: counting even 10,001 of them costs as much as
        # the walk itself (and a plain scan when the planner gives up on the
        # index), so the estimate is reported instead. Few: count via bitmap.
        estimate = _estimate_rows(conn, plan)
        # Rows in scope without q/dates (counts table) give the selectivity: a
        # term in a quarter of a 40k-row site is walked (~page_size*4 rows),
        # not answered from a GIN bitmap that spans the whole table.
        scope_rows = sum(int(row["n"]) for row in counts) if counts else None
        dense = bool(scope_rows) and estimate >= WALK_MIN_SELECTIVITY * scope_rows
        mode = "walk" if estimate > STRATEGY_ESTIMATE_SPLIT or dense else "bitmap"
        # A date range on a column other than the sort key is usually
        # correlated with position (a 2020 publication range sits in one
        # stretch of the collected order), so a walk from the top can read far
        # before its first hit. Read the range from its own index and sort.
        cross_range = ((filters.sort == "collected_desc" and (filters.published_from or filters.published_to))
                       or (filters.sort == "published_desc" and (filters.collected_from or filters.collected_to)))
        if cross_range and not filters.q and estimate <= CROSS_RANGE_BITMAP_MAX:
            mode = "bitmap"
        counted = None
        if estimate <= STRATEGY_ESTIMATE_SPLIT:
            rows = _query(conn, capped_count_sql(plan), {**plan.params, **_scope_params(plan), "cap": COUNT_CAP + 1},
                          "bitmap" if mode == "bitmap" else None, COUNT_BUDGET_MS)
            counted = rows[0]["count"] if rows is not None else None
        if counted is None:
            total = {"value": max(estimate, COUNT_CAP), "exact": False, "source": "estimate"}
        else:
            total = {"value": min(counted, COUNT_CAP), "exact": counted <= COUNT_CAP,
                     "source": "capped_count"}
    else:
        counted = conn.execute(capped_count_sql(plan),
                               {**plan.params, **_scope_params(plan), "cap": COUNT_CAP + 1},
                               prepare=False).fetchone()["count"]
        total = {"value": min(counted, COUNT_CAP), "exact": counted <= COUNT_CAP,
                 "source": "capped_count"}
        if counted > COUNT_CAP and plan.scope_sites is None and plan.where == "TRUE":
            # Unfiltered and no counts table: the planner's row estimate.
            estimate = conn.execute("SELECT GREATEST(reltuples, 0)::bigint AS n FROM pg_class "
                                    "WHERE oid = 'public.documents'::regclass").fetchone()["n"]
            total = {"value": max(int(estimate), COUNT_CAP), "exact": False, "source": "estimate"}

    # A zero from the counts table is trusted only for sites it has counted
    # (a site crawled after the last refresh must still list its documents).
    # A live count of 0 is final. A counts-table 0 may be stale (PDFs or a new
    # language since the last refresh), so it is confirmed by a LIMIT 1 probe;
    # only a probe that cannot finish within its budget falls back to trusting
    # the table, and then the total is reported as not exact.
    known_empty = total["value"] == 0 and total["source"] == "capped_count"
    if total["source"] == "counts" and total["value"] == 0:
        probe = _query(conn, f"SELECT 1 AS found FROM documents d WHERE {_scope_sql(plan)} AND {plan.where} LIMIT 1",
                       {**plan.params, **_scope_params(plan)}, None, PROBE_BUDGET_MS)
        known_empty = not probe
        if probe is None:
            total.update(exact=False, verified=False)
    # Few matches (a rare search term, a rare language in a big site) are read
    # in one pass and ordered in Python; an ordered index walk with a filter
    # would visit rows/selectivity entries before finding a page. The read
    # itself verifies the size: more than MATERIALIZE_MAX rows (stale counts)
    # switches to the ordinary keyset read.
    strategy = _Strategy(mode if needs_live_count and not empty_scope else None,
                         None if mode == "walk" and estimate > BITMAP_FALLBACK_MAX else STRATEGY_BUDGET_MS)
    state = {"small": (total["source"] in ("capped_count", "counts") and total["exact"]
                       and total["value"] <= MATERIALIZE_MAX and filters.sort != "relevance"),
             "overflow": False}

    def read(c, n):
        if state["small"]:
            rows = _read_materialized(conn, plan, filters.sort, c, n, "bitmap" if needs_live_count else None)
            if rows is not None:
                return rows
            state.update(small=False, overflow=True)
        return _read(conn, plan, filters.sort, c, n, strategy)

    if empty_scope or known_empty:
        keyed: list[tuple] = []
        has_next = has_prev = False
    elif cursor is not None and cursor.direction == "before":
        keyed = read(cursor, size + 1)
        has_prev = len(keyed) > size
        keyed = list(reversed(keyed[:size]))
        has_next = True
        if len(keyed) < size:  # reached the start: show a full first page
            cursor = None
            keyed = read(None, size + 1)
            has_next, has_prev = len(keyed) > size, False
            keyed = keyed[:size]
    else:
        keyed = read(cursor, offset + size + 1)
        has_next = len(keyed) > offset + size
        keyed = keyed[offset:offset + size]
        has_prev = cursor is not None or offset > 0

    if state["overflow"]:
        total.update(value=max(total["value"], MATERIALIZE_MAX + 1), exact=False)
    seen = offset + len(keyed) + (1 if has_next else 0)
    if cursor is None and total["source"] == "counts" and seen > total["value"]:
        total.update(value=seen, exact=False)  # counts are older than the data
    elif cursor is None and not total["exact"]:
        if not has_next:  # the whole result is on this page: the estimate was wrong
            total = {"value": seen, "exact": True, "source": "listed"}
        elif seen > total["value"]:
            total["value"] = seen

    items = _hydrate(conn, [seq for _, seq in keyed], taxonomy)
    next_cursor = (encode_cursor(filters.sort, "after", keyed[-1][0], keyed[-1][1], fingerprint)
                   if has_next and keyed else None)
    prev_cursor = (encode_cursor(filters.sort, "before", keyed[0][0], keyed[0][1], fingerprint)
                   if has_prev and keyed else None)

    if empty_scope:
        facets = {"countries": [], "doc_types": [], "sites": [], "languages": []}
        facets_meta = {"source": "empty_scope", "exact": True}
    elif counts is not None:
        facets = _facets_from_counts(counts, taxonomy)
        facets_meta = {"source": "counts", "exact": not needs_live_count, "as_of": counts_as_of,
                       "ignores": [name for name in ("q", "published_from", "published_to",
                                                     "collected_from", "collected_to")
                                   if getattr(filters, name)]}
    else:
        facets = _fallback_facets(conn, taxonomy)
        facets_meta = {"source": "fallback", "exact": False}

    return {
        "items": items,
        "pagination": {
            "page": None if filters.cursor else filters.page,
            "page_size": size,
            "total": total["value"],
            "total_exact": total["exact"],
            "pages": math.ceil(total["value"] / size) if total["exact"] else None,
            "has_next": has_next,
            "has_prev": has_prev,
            "next_cursor": next_cursor,
            "prev_cursor": prev_cursor,
            "max_offset_page": MAX_OFFSET_PAGE,
        },
        "total": total,
        "facets": facets,
        "facets_meta": facets_meta,
        "query": {"q": filters.q, "sort": filters.sort,
                  "date_policy": "complete_iso_dates_only; unresolved_dates_excluded_from_date_ranges"},
        "measured_at": datetime.now(timezone.utc),
    }

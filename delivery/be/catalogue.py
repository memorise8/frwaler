"""Read-only document catalogue queries for the delivery API."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

TAXONOMY_PATH = Path(__file__).resolve().parents[1] / "data" / "site-taxonomy.json"


def _safe_date(column: str) -> str:
    """PostgreSQL 16 expression that never casts an invalid free-form date."""
    value = f"substring({column}, 1, 10)"
    return f"CASE WHEN pg_input_is_valid({value}, 'date') THEN {value}::date END"


def load_taxonomy(path: Path = TAXONOMY_PATH) -> dict[str, dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["sites"]


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


def _taxonomy_params(taxonomy: dict[str, dict[str, str]]) -> tuple[list[str], list[str], list[str]]:
    site_ids = sorted(taxonomy)
    return (
        site_ids,
        [taxonomy[key]["country"] for key in site_ids],
        [taxonomy[key]["doc_type"] for key in site_ids],
    )


def _filtered_sql(filters: CatalogueFilters, taxonomy: dict[str, dict[str, str]]) -> tuple[str, dict]:
    site_ids, countries, doc_types = _taxonomy_params(taxonomy)
    params: dict = {"taxonomy_sites": site_ids, "taxonomy_countries": countries,
                    "taxonomy_doc_types": doc_types}
    clauses: list[str] = []
    if filters.q:
        params["q"] = filters.q
        params["q_like"] = f"%{filters.q}%"
        clauses.append("(d.fts @@ plainto_tsquery('simple', %(q)s) OR d.title ILIKE %(q_like)s)")
    if filters.site_id:
        params["site_id"] = filters.site_id
        clauses.append("d.site_id = %(site_id)s")
    if filters.country:
        params["country"] = filters.country
        clauses.append("COALESCE(t.country, '기타') = %(country)s")
    if filters.doc_type:
        params["doc_type"] = filters.doc_type
        clauses.append("COALESCE(t.doc_type, '기타') = %(doc_type)s")
    if filters.lang:
        params["lang"] = filters.lang
        clauses.append("COALESCE(dl.lang, 'unknown') = %(lang)s")
    for name, operator, value in (
        ("published_from", ">=", filters.published_from),
        ("published_to", "<=", filters.published_to),
    ):
        if value is not None:
            params[name] = value
            clauses.append(f"({_safe_date('d.published_date')} {operator} %({name})s)")
    if filters.collected_from is not None:
        params["collected_from"] = filters.collected_from
        clauses.append("d.collected_at >= %(collected_from)s::date")
    if filters.collected_to is not None:
        params["collected_to"] = filters.collected_to
        clauses.append("d.collected_at < (%(collected_to)s::date + interval '1 day')")
    if filters.has_pdf is not None:
        params["has_pdf"] = 1 if filters.has_pdf else 0
        clauses.append("(d.pdf_downloaded <> 0) = (%(has_pdf)s = 1)")
    if filters.has_text is not None:
        params["has_text"] = 1 if filters.has_text else 0
        clauses.append("(d.text_extracted <> 0) = (%(has_text)s = 1)")
    where = " AND ".join(clauses) if clauses else "TRUE"
    sql = f"""
        WITH taxonomy(site_id, country, doc_type) AS (
          SELECT * FROM unnest(%(taxonomy_sites)s::text[], %(taxonomy_countries)s::text[],
                               %(taxonomy_doc_types)s::text[])
        ), filtered AS (
          SELECT d.*, s.site_name, COALESCE(t.country, '기타') AS country,
                 COALESCE(t.doc_type, '기타') AS doc_type,
                 COALESCE(dl.lang, 'unknown') AS lang
          FROM documents d
          JOIN sites s USING (site_id)
          LEFT JOIN taxonomy t USING (site_id)
          LEFT JOIN document_lang dl USING (seq_id)
          WHERE {where}
        )
    """
    return sql, params


def collect_catalogue(conn, filters: CatalogueFilters,
                      taxonomy: dict[str, dict[str, str]] | None = None) -> dict:
    taxonomy = taxonomy if taxonomy is not None else load_taxonomy()
    base, params = _filtered_sql(filters, taxonomy)
    total = conn.execute(base + " SELECT count(*) AS count FROM filtered", params).fetchone()["count"]
    offset = (filters.page - 1) * filters.page_size
    params.update({"limit": filters.page_size, "offset": offset})
    if filters.sort == "relevance":
        order = "ts_rank_cd(fts, plainto_tsquery('simple', %(q)s)) DESC, similarity(title, %(q)s) DESC, seq_id DESC"
    elif filters.sort == "published_desc":
        order = f"{_safe_date('published_date')} DESC NULLS LAST, seq_id DESC"
    elif filters.sort == "seq_desc":
        order = "seq_id DESC"
    else:
        order = "collected_at DESC NULLS LAST, seq_id DESC"
    items = conn.execute(base + f"""
        SELECT seq_id, site_id, site_name, country, doc_type, title, published_date,
               collected_at, authors, publisher, journal, lang,
               pdf_downloaded <> 0 AS has_pdf, text_extracted <> 0 AS has_text,
               EXISTS (SELECT 1 FROM document_translations tr
                       WHERE tr.seq_id=filtered.seq_id AND tr.state='completed') AS has_translation,
               meta_url
        FROM filtered ORDER BY {order} LIMIT %(limit)s OFFSET %(offset)s
    """, params).fetchall()

    facets: dict[str, list[dict]] = {}
    for key, query in {
        "countries": "SELECT country AS value, count(*) AS count FROM filtered GROUP BY country ORDER BY count DESC, value",
        "doc_types": "SELECT doc_type AS value, count(*) AS count FROM filtered GROUP BY doc_type ORDER BY count DESC, value",
        "sites": "SELECT site_id AS value, max(site_name) AS label, count(*) AS count FROM filtered GROUP BY site_id ORDER BY count DESC, value",
        "languages": "SELECT lang AS value, count(*) AS count FROM filtered GROUP BY lang ORDER BY count DESC, value",
    }.items():
        facets[key] = [dict(row) for row in conn.execute(base + " " + query, params).fetchall()]

    return {
        "items": [dict(row) for row in items],
        "pagination": {
            "page": filters.page, "page_size": filters.page_size, "total": total,
            "pages": math.ceil(total / filters.page_size) if total else 0,
        },
        "facets": facets,
        "query": {"q": filters.q, "sort": filters.sort},
        "measured_at": datetime.now(timezone.utc),
    }

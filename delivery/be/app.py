# -*- coding: utf-8 -*-
"""최소 납품 BE (Phase 0): health, 문서 조회, 작업 enqueue/조회.

DB 접근은 crawler.db_pg 단일 경로. 요청마다 짧은 커넥션을 연다(Phase 0 단순화;
커넥션 풀은 Phase 1 에서 도입).
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from crawler import db_pg
from delivery.be.catalogue import CatalogueFilters, collect_catalogue, load_taxonomy
from delivery.be.freshness import collect_freshness
from delivery.be.stats import collect_stats
from delivery.worker import jobs


class JobIn(BaseModel):
    site_id: str
    mode: str = "incremental"
    limit_n: int | None = None
    requested_by: str | None = None


class RetryIn(BaseModel):
    requested_by: str | None = None


def create_app(dsn: str) -> FastAPI:
    app = FastAPI(title="Libertree Delivery BE (Phase 0)")
    taxonomy = load_taxonomy()

    def _conn():
        return db_pg.open_db(dsn)

    @app.get("/health")
    def health():
        try:
            conn = _conn()
            try:
                conn.execute("SELECT 1").fetchone()
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(status_code=503, content={"status": "error", "detail": str(exc)[:200]})
        return {"status": "ok"}

    @app.get("/documents/{seq_id}")
    def get_document(seq_id: int):
        conn = _conn()
        try:
            row = db_pg.get_document(conn, seq_id)
        finally:
            conn.close()
        if row is None:
            raise HTTPException(status_code=404, detail="document not found")
        return dict(row)

    @app.get("/documents")
    def get_documents(
        q: str | None = Query(default=None, max_length=200),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        site_id: str | None = None,
        country: str | None = None,
        doc_type: str | None = None,
        lang: str | None = None,
        published_from: date | None = None,
        published_to: date | None = None,
        collected_from: date | None = None,
        collected_to: date | None = None,
        has_pdf: bool | None = None,
        has_text: bool | None = None,
        sort: Literal["relevance", "published_desc", "collected_desc", "seq_desc"] | None = None,
    ):
        normalized_q = q.strip() if q else None
        normalized_q = normalized_q or None
        effective_sort = sort or ("relevance" if normalized_q else "collected_desc")
        if effective_sort == "relevance" and not normalized_q:
            raise HTTPException(status_code=422, detail="relevance sort requires q")
        if published_from and published_to and published_from > published_to:
            raise HTTPException(status_code=422, detail="invalid published date range")
        if collected_from and collected_to and collected_from > collected_to:
            raise HTTPException(status_code=422, detail="invalid collected date range")
        countries = {item["country"] for item in taxonomy.values()} | {"기타"}
        doc_types = {item["doc_type"] for item in taxonomy.values()} | {"기타"}
        if country and country not in countries:
            raise HTTPException(status_code=422, detail="unknown country")
        if doc_type and doc_type not in doc_types:
            raise HTTPException(status_code=422, detail="unknown document type")
        conn = _conn()
        try:
            return collect_catalogue(conn, CatalogueFilters(
                q=normalized_q, page=page, page_size=page_size, site_id=site_id,
                country=country, doc_type=doc_type, lang=lang,
                published_from=published_from, published_to=published_to,
                collected_from=collected_from, collected_to=collected_to,
                has_pdf=has_pdf, has_text=has_text, sort=effective_sort,
            ), taxonomy)
        finally:
            conn.close()

    @app.get("/stats")
    def get_stats():
        conn = _conn()
        try:
            return collect_stats(conn)
        finally:
            conn.close()

    @app.get("/freshness")
    def get_freshness():
        conn = _conn()
        try:
            return collect_freshness(conn)
        finally:
            conn.close()

    @app.post("/jobs")
    def post_job(body: JobIn):
        conn = _conn()
        try:
            jid = jobs.enqueue_job(conn, body.site_id, mode=body.mode,
                                   limit_n=body.limit_n, requested_by=body.requested_by)
        finally:
            conn.close()
        return {"id": jid}

    @app.get("/jobs/{job_id}")
    def get_job(job_id: int):
        conn = _conn()
        try:
            row = conn.execute("SELECT * FROM crawl_jobs WHERE id=%s", (job_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise HTTPException(status_code=404, detail="job not found")
        return dict(row)

    @app.get("/jobs")
    def get_jobs(status: str | None = None, limit: int = 50, offset: int = 0):
        if status not in (None, "queued", "running", "done", "failed", "cancelled"):
            raise HTTPException(status_code=422, detail="invalid job status")
        if not 1 <= limit <= 200 or offset < 0:
            raise HTTPException(status_code=422, detail="invalid pagination")
        conn = _conn()
        try:
            rows = jobs.list_jobs(conn, status=status, limit=limit, offset=offset)
        finally:
            conn.close()
        return {"jobs": rows, "limit": limit, "offset": offset}

    @app.post("/jobs/{job_id}/cancel")
    def post_cancel_job(job_id: int):
        conn = _conn()
        try:
            row = jobs.cancel_job(conn, job_id)
            if row is not None:
                return row
            current = conn.execute(
                "SELECT status FROM crawl_jobs WHERE id=%s", (job_id,)
            ).fetchone()
        finally:
            conn.close()
        if current is None:
            raise HTTPException(status_code=404, detail="job not found")
        raise HTTPException(status_code=409, detail=f"cannot cancel job in {current['status']} status")

    @app.post("/jobs/{job_id}/retry")
    def post_retry_job(job_id: int, body: RetryIn | None = None):
        conn = _conn()
        try:
            row = jobs.retry_job(
                conn, job_id, requested_by=body.requested_by if body else None
            )
            if row is not None:
                return row
            current = conn.execute(
                "SELECT status, retried_by FROM crawl_jobs WHERE id=%s", (job_id,)
            ).fetchone()
        finally:
            conn.close()
        if current is None:
            raise HTTPException(status_code=404, detail="job not found")
        if current.get("retried_by") is not None:
            raise HTTPException(status_code=409, detail="job was already retried")
        raise HTTPException(status_code=409, detail=f"cannot retry job in {current['status']} status")

    return app

# -*- coding: utf-8 -*-
"""최소 납품 BE (Phase 0): health, 문서 조회, 작업 enqueue/조회.

DB 접근은 crawler.db_pg 단일 경로. 요청마다 짧은 커넥션을 연다(Phase 0 단순화;
커넥션 풀은 Phase 1 에서 도입).
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from crawler import db_pg
from delivery.be.stats import collect_stats
from delivery.worker import jobs


class JobIn(BaseModel):
    site_id: str
    mode: str = "incremental"
    limit_n: int | None = None
    requested_by: str | None = None


def create_app(dsn: str) -> FastAPI:
    app = FastAPI(title="Libertree Delivery BE (Phase 0)")

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

    @app.get("/stats")
    def get_stats():
        conn = _conn()
        try:
            return collect_stats(conn)
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

    return app

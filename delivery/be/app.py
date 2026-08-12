# -*- coding: utf-8 -*-
"""최소 납품 BE (Phase 0): health, 문서 조회, 작업 enqueue/조회.

DB 접근은 crawler.db_pg 단일 경로. 요청마다 짧은 커넥션을 연다(Phase 0 단순화;
커넥션 풀은 Phase 1 에서 도입).
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict

from crawler import db_pg
from delivery.be.catalogue import CatalogueFilters, collect_catalogue, load_taxonomy
from delivery.be.document_detail import collect_document_detail
from delivery.be.freshness import collect_freshness
from delivery.be.stats import collect_stats
from delivery.be.translation_quality import collect_translation_quality
from delivery.be.batch_operations import batch_detail, list_batches, operations
from delivery.be.access import require_operator
from delivery.be.blob_delivery import resolve_blob
from delivery.worker import jobs
from delivery.worker import schedules
from delivery.translation import jobs as translation_jobs


class JobIn(BaseModel):
    site_id: str
    mode: str = "incremental"
    limit_n: int | None = None
    requested_by: str | None = None


class RetryIn(BaseModel):
    requested_by: str | None = None


class ScheduleIn(BaseModel):
    interval_hours:int
    mode:Literal["incremental","full"]="incremental"
    limit_n:int|None=100
    enabled:bool=True


class ObservationIn(BaseModel):
    model_config=ConfigDict(extra="forbid")
    worker_id:str="gpu0-observer"
    endpoint_healthy:bool
    gpu_memory_free_bytes:int|None=None
    gpu_utilization_percent:float|None=None
    disk_free_bytes:int|None=None


class TranslationSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[Literal["title_translation", "abstract_summary"]] = ["title_translation", "abstract_summary"]
    lang: str | None = None
    site_id: str | None = None


class TranslationPreviewIn(TranslationSelection):
    provider: Literal["external", "internal"] = "internal"
    model_version: str = "unknown"
    prompt_version: str = "title-summary-ko-v1"
    limit: int = 100


class TranslationJobIn(TranslationSelection):
    provider: Literal["external", "internal"]
    model_version: str
    prompt_version: str = "title-summary-ko-v1"
    target_locale: str = "ko-KR"
    limit: int = 100
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
    def get_document(seq_id: int = Path(gt=0)):
        conn = _conn()
        try:
            row = collect_document_detail(conn, seq_id, taxonomy)
        finally:
            conn.close()
        if row is None:
            raise HTTPException(status_code=404, detail="document not found")
        return row

    @app.get("/documents/{seq_id}/pdf")
    def get_document_pdf(seq_id:int=Path(gt=0)):
        conn=_conn()
        try:
            try:resolved=resolve_blob(conn,seq_id,"pdf")
            except FileNotFoundError as exc:raise HTTPException(status_code=404,detail=str(exc)) from exc
        finally:conn.close()
        if resolved is None:raise HTTPException(status_code=404,detail="document not found")
        path,media,filename=resolved
        return FileResponse(path,media_type=media,filename=filename,content_disposition_type="inline")

    @app.get("/documents/{seq_id}/text")
    def get_document_text(seq_id:int=Path(gt=0)):
        conn=_conn()
        try:
            try:resolved=resolve_blob(conn,seq_id,"text")
            except FileNotFoundError as exc:raise HTTPException(status_code=404,detail=str(exc)) from exc
        finally:conn.close()
        if resolved is None:raise HTTPException(status_code=404,detail="document not found")
        path,media,filename=resolved
        return FileResponse(path,media_type=media,filename=filename,content_disposition_type="inline")

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

    @app.post("/translation/preview")
    def post_translation_preview(body: TranslationPreviewIn):
        if not 1 <= body.limit <= 1000:
            raise HTTPException(status_code=422, detail="invalid preview limit")
        conn = _conn()
        try:
            return translation_jobs.preview_targets(conn, tasks=body.tasks, lang=body.lang,
                site_id=body.site_id,provider=body.provider,model_version=body.model_version,
                prompt_version=body.prompt_version,limit=body.limit)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            if str(exc).startswith("circuit_open:"):
                raise HTTPException(status_code=409,detail={"code":"circuit_open","reason_codes":str(exc).split(":",1)[1].split(",")}) from exc
            raise
        finally:
            conn.close()

    @app.get("/translation/batches")
    def get_translation_batches(limit: int = 50, offset: int = 0):
        if not 1 <= limit <= 200 or offset < 0: raise HTTPException(status_code=422,detail="invalid pagination")
        conn=_conn()
        try:return list_batches(conn,limit=limit,offset=offset)
        finally:conn.close()

    @app.get("/translation/batches/{batch_id}")
    def get_translation_batch(batch_id: int = Path(gt=0)):
        conn=_conn()
        try:result=batch_detail(conn,batch_id)
        finally:conn.close()
        if result is None:raise HTTPException(status_code=404,detail="translation batch not found")
        return result

    @app.get("/translation/operations")
    def get_translation_operations(provider: Literal["external","internal"]="internal",
                                   model_version: str="unknown",prompt_version: str="title-summary-ko-v1"):
        conn=_conn()
        try:return operations(conn,provider=provider,model_version=model_version,prompt_version=prompt_version)
        finally:conn.close()

    @app.post("/translation/operations/observations")
    def post_translation_observations(body:ObservationIn,operator:str=Depends(require_operator)):
        if not 0<=len(body.worker_id)<=100 or body.gpu_utilization_percent is not None and not 0<=body.gpu_utilization_percent<=100:
            raise HTTPException(status_code=422,detail="invalid observation")
        numeric={"gpu_memory_free_bytes":body.gpu_memory_free_bytes,"gpu_utilization_percent":body.gpu_utilization_percent,"disk_free_bytes":body.disk_free_bytes}
        if any(value is not None and value<0 for value in numeric.values()):raise HTTPException(status_code=422,detail="invalid observation")
        conn=_conn()
        try:
            conn.execute("INSERT INTO translation_system_observations(worker_id,metric,value_boolean) VALUES(%s,'endpoint_healthy',%s)",(body.worker_id,body.endpoint_healthy))
            for metric,value in numeric.items():
                if value is not None:conn.execute("INSERT INTO translation_system_observations(worker_id,metric,value_numeric) VALUES(%s,%s,%s)",(body.worker_id,metric,value))
            conn.commit()
        finally:conn.close()
        return {"accepted":True}

    @app.post("/translation/jobs")
    def post_translation_jobs(body: TranslationJobIn,operator:str=Depends(require_operator)):
        if not 1 <= body.limit <= 1000 or not body.model_version.strip() or not body.prompt_version.strip():
            raise HTTPException(status_code=422, detail="invalid translation job configuration")
        conn = _conn()
        try:
            return translation_jobs.enqueue_targets(
                conn, tasks=body.tasks, provider=body.provider,
                model_version=body.model_version.strip(), prompt_version=body.prompt_version.strip(),
                target_locale=body.target_locale, lang=body.lang, site_id=body.site_id,
                limit=body.limit, requested_by=operator,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            if str(exc).startswith("circuit_open:"):
                raise HTTPException(status_code=409,detail={"code":"circuit_open","reason_codes":str(exc).split(":",1)[1].split(",")}) from exc
            raise
        finally:
            conn.close()

    @app.get("/translation/jobs")
    def get_translation_jobs(status: str | None = None, limit: int = 50, offset: int = 0):
        allowed = {"pending", "running", "completed", "failed", "skipped", "cancelled"}
        if status is not None and status not in allowed:
            raise HTTPException(status_code=422, detail="invalid translation job status")
        if not 1 <= limit <= 200 or offset < 0:
            raise HTTPException(status_code=422, detail="invalid pagination")
        conn = _conn()
        try:
            rows = translation_jobs.list_jobs(conn, status=status, limit=limit, offset=offset)
            summary = {row["status"]: row["count"] for row in conn.execute(
                "SELECT status,count(*) AS count FROM translation_jobs GROUP BY status"
            ).fetchall()}
            return {"jobs": rows, "summary": summary, "limit": limit, "offset": offset}
        finally:
            conn.close()

    @app.get("/translation/quality")
    def get_translation_quality(decision: str | None = None, limit: int = 50, offset: int = 0):
        allowed = {"auto_approved", "review_recommended", "rejected"}
        if decision is not None and decision not in allowed:
            raise HTTPException(status_code=422, detail="invalid quality decision")
        if not 1 <= limit <= 200 or offset < 0:
            raise HTTPException(status_code=422, detail="invalid pagination")
        conn = _conn()
        try:
            return collect_translation_quality(conn, decision=decision, limit=limit, offset=offset)
        finally:
            conn.close()

    @app.post("/translation/jobs/{job_id}/cancel")
    def post_translation_cancel(job_id: int = Path(gt=0),operator:str=Depends(require_operator)):
        conn = _conn()
        try:
            row = translation_jobs.cancel(conn, job_id)
            current = row or conn.execute("SELECT status FROM translation_jobs WHERE id=%s", (job_id,)).fetchone()
        finally:
            conn.close()
        if row: return row
        if current is None: raise HTTPException(status_code=404, detail="translation job not found")
        raise HTTPException(status_code=409, detail=f"cannot cancel translation job in {current['status']} status")

    @app.post("/translation/jobs/{job_id}/retry")
    def post_translation_retry(job_id: int = Path(gt=0),operator:str=Depends(require_operator)):
        conn = _conn()
        try:
            row = translation_jobs.retry(conn, job_id)
            current = row or conn.execute("SELECT status,attempts,max_attempts FROM translation_jobs WHERE id=%s", (job_id,)).fetchone()
        finally:
            conn.close()
        if row: return row
        if current is None: raise HTTPException(status_code=404, detail="translation job not found")
        raise HTTPException(status_code=409, detail="translation job cannot be retried")

    @app.post("/jobs")
    def post_job(body: JobIn,operator:str=Depends(require_operator)):
        conn = _conn()
        try:
            jid = jobs.enqueue_job(conn, body.site_id, mode=body.mode,
                                   limit_n=body.limit_n, requested_by=operator)
        finally:
            conn.close()
        return {"id": jid}

    @app.get("/jobs/{job_id}")
    def get_job(job_id: int):
        conn = _conn()
        try:
            row = jobs.job_detail(conn,job_id)
        finally:
            conn.close()
        if row is None:
            raise HTTPException(status_code=404, detail="job not found")
        return {**row["job"],"logs":row["logs"]}

    @app.get("/schedules")
    def get_schedules():
        conn=_conn()
        try:return {"schedules":schedules.list_schedules(conn)}
        finally:conn.close()

    @app.put("/schedules/{site_id}")
    def put_schedule(body:ScheduleIn,site_id:str,operator:str=Depends(require_operator)):
        conn=_conn()
        try:
            if conn.execute("SELECT 1 FROM sites WHERE site_id=%s",(site_id,)).fetchone() is None:
                raise HTTPException(status_code=404,detail="site not found")
            try:return schedules.upsert(conn,site_id=site_id,interval_hours=body.interval_hours,
              mode=body.mode,limit_n=body.limit_n,enabled=body.enabled,created_by=operator)
            except ValueError as exc:raise HTTPException(status_code=422,detail=str(exc)) from exc
        finally:conn.close()

    @app.get("/jobs")
    def get_jobs(status: str | None = None, limit: int = 50, offset: int = 0):
        if status not in (None, "queued", "running", "cancelling", "done", "failed", "cancelled"):
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
    def post_cancel_job(job_id: int,operator:str=Depends(require_operator)):
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
    def post_retry_job(job_id: int, body: RetryIn | None = None,operator:str=Depends(require_operator)):
        conn = _conn()
        try:
            row = jobs.retry_job(
                conn, job_id, requested_by=operator
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

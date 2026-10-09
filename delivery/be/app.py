# -*- coding: utf-8 -*-
"""Libertree Delivery HTTP API."""
from __future__ import annotations

from datetime import date
import os
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from delivery.be.catalogue import CatalogueFilters, collect_catalogue, load_taxonomy
from delivery.be.document_detail import collect_document_detail
from delivery.be.freshness import collect_freshness
from delivery.be.verification import collect_verification
from delivery.be.stats import collect_stats
from delivery.be.translation_quality import collect_translation_quality
from delivery.be.batch_operations import batch_detail, list_batches, operations
from delivery.be.access import require_operator
from delivery.be.blob_delivery import resolve_blob
from delivery.worker import jobs
from delivery.worker import schedules
from delivery.files import jobs as file_jobs
from delivery.translation import jobs as translation_jobs
from delivery.db.schema import verify_required_schema
from delivery.translation.observations import prune as prune_observations, record as record_observation
from delivery.be.database import get_database
from delivery.be.cache import TTLCache


class JobIn(BaseModel):
    model_config=ConfigDict(extra="forbid")
    site_id: str = Field(min_length=1,max_length=200,pattern=r"^[A-Za-z0-9._-]+$")
    mode: Literal["incremental","full","backfill"] = "incremental"
    process_files: bool = False
    trial: bool = False
    limit_n: int | None = Field(default=None,ge=1,le=1000)
    requested_by: str | None = Field(default=None,max_length=100)


class RetryIn(BaseModel):
    model_config=ConfigDict(extra="forbid")
    requested_by: str | None = Field(default=None,max_length=100)
    trial: bool = False


class DateRangeIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    lower: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    upper: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    expected_count: int = Field(strict=True, ge=0, lt=1500)


class DatePlanIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    site_id: Literal['federalregister-gov-documents']
    lower: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    upper: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    ranges: list[DateRangeIn] = Field(min_length=1, max_length=10000)
    observation_id: str = Field(min_length=1, max_length=200)
    limit_n: int | None = Field(default=100, strict=True, ge=1, le=1000)
    process_files: bool = False
    trial: bool = False


class FileBackfillIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    site_id: str = Field(min_length=1,max_length=200,pattern=r'^[A-Za-z0-9._-]+$')
    after_seq: int = Field(default=0, ge=0)
    limit_n: int = Field(default=100, ge=1, le=1000)
    include_existing: bool = False
    dry_run: bool = True
    collected_from: date | None = None
    collected_to: date | None = None


class ScheduleIn(BaseModel):
    model_config=ConfigDict(extra="forbid")
    interval_hours:int=Field(ge=1,le=8760)
    mode:Literal["incremental","full"]="incremental"
    limit_n:int|None=Field(default=100,ge=1,le=1000)
    enabled:bool=True
    trial:bool=False


class ObservationIn(BaseModel):
    model_config=ConfigDict(extra="forbid")
    worker_id:str=Field(default="gpu0-observer",min_length=1,max_length=100)
    provider:Literal["internal","external"]="internal"
    model_version:str|None=Field(default=None,max_length=128)
    endpoint_healthy:bool
    gpu_memory_free_bytes:int|None=None
    gpu_utilization_percent:float|None=None
    disk_free_bytes:int|None=None


class TranslationSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[Literal["title_translation", "abstract_summary"]] = ["title_translation", "abstract_summary"]
    lang: str | None = Field(default=None,max_length=32)
    site_id: str | None = Field(default=None,max_length=200,pattern=r"^[A-Za-z0-9._-]+$")


class TranslationPreviewIn(TranslationSelection):
    provider: Literal["external", "internal"] = "internal"
    model_version: str = Field(default="unknown",min_length=1,max_length=128)
    prompt_version: str = Field(default="title-summary-ko-v1",min_length=1,max_length=128)
    limit: int = 100


class TranslationJobIn(TranslationSelection):
    provider: Literal["external", "internal"]
    model_version: str = Field(min_length=1,max_length=128)
    prompt_version: str = Field(default="title-summary-ko-v1",min_length=1,max_length=128)
    target_locale: str = Field(default="ko-KR",pattern=r"^[a-z][a-z]-[A-Z][A-Z]$")
    limit: int = 100
    requested_by: str | None = None


def create_app(dsn: str) -> FastAPI:
    app = FastAPI(title="Libertree Delivery BE (Phase 0)")
    taxonomy = load_taxonomy()
    database = get_database(dsn)
    aggregates = TTLCache(
        float(os.environ.get("DELIVERY_AGGREGATE_CACHE_SECONDS", "30"))
    )

    def _conn():
        return database.connection()

    @app.get("/health")
    def health():
        try:
            conn = _conn()
            try:
                conn.execute("SELECT 1").fetchone()
            finally:
                conn.close()
        except Exception:  # database coordinates and errors are not public health data
            return JSONResponse(status_code=503, content={"status": "error"})
        return {"status": "ok"}

    @app.get("/health/crawlers", dependencies=[Depends(require_operator)])
    def get_crawler_health():
        from delivery.health.overview import crawler_overview
        return aggregates.get_or_create("crawler_health", crawler_overview)

    @app.get("/health/system", dependencies=[Depends(require_operator)])
    def get_system_health():
        from delivery.health.overview import system_overview
        try:
            conn = _conn()
            try:
                return system_overview(conn)
            finally:
                conn.close()
        except Exception:
            return JSONResponse(status_code=503, content={"database": "unavailable"})

    @app.get("/ready")
    def ready():
        try:
            conn = _conn()
            try:
                verify_required_schema(conn)
            finally:
                conn.close()
        except Exception:  # schema details belong in migration logs, not public responses
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        return {"status": "ready"}

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
        def collect():
            conn = _conn()
            try:
                return collect_stats(conn)
            finally:
                conn.close()

        return aggregates.get_or_create("stats", collect)

    @app.get("/freshness")
    def get_freshness():
        def collect():
            conn = _conn()
            try:
                return collect_freshness(conn)
            finally:
                conn.close()

        return aggregates.get_or_create("freshness", collect)

    @app.get("/sites/verification")
    def get_site_verification():
        def collect():
            conn = _conn()
            try:
                return collect_verification(conn)
            finally:
                conn.close()

        return aggregates.get_or_create("site_verification", collect)

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
    def get_translation_operations(
        provider: Literal["external", "internal"] = "internal",
        model_version: str | None = None,
        prompt_version: str = "title-summary-ko-v1",
    ):
        effective_model = model_version or os.environ.get(
            "TRANSLATION_INTERNAL_MODEL" if provider == "internal" else "TRANSLATION_EXTERNAL_MODEL"
        )
        conn = _conn()
        try:
            return operations(
                conn,
                provider=provider,
                model_version=effective_model or "unknown",
                prompt_version=prompt_version,
            )
        finally:
            conn.close()

    @app.post("/translation/operations/observations")
    def post_translation_observations(body:ObservationIn,operator:str=Depends(require_operator)):
        if not body.model_version or not body.model_version.strip() or body.gpu_utilization_percent is not None and not 0<=body.gpu_utilization_percent<=100:
            raise HTTPException(status_code=422,detail="invalid observation")
        numeric={"gpu_memory_free_bytes":body.gpu_memory_free_bytes,"gpu_utilization_percent":body.gpu_utilization_percent,"disk_free_bytes":body.disk_free_bytes}
        if any(value is not None and value<0 for value in numeric.values()):raise HTTPException(status_code=422,detail="invalid observation")
        conn=_conn()
        try:
            record_observation(conn,worker_id=body.worker_id,provider=body.provider,
              model_version=body.model_version.strip(),metric="endpoint_healthy",value=body.endpoint_healthy)
            for metric,value in numeric.items():
                if value is not None:record_observation(conn,worker_id=body.worker_id,provider=body.provider,
                  model_version=body.model_version.strip(),metric=metric,value=value)
            prune_observations(conn)
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
            if conn.execute("SELECT 1 FROM sites WHERE site_id=%s",(body.site_id,)).fetchone() is None:
                raise HTTPException(status_code=404,detail="site not found")
            from delivery.capabilities import validate_request, CapabilityError
            try:
                validate_request(body.site_id, body.mode, trial=body.trial)
            except CapabilityError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            try:
                jid = jobs.enqueue_job(conn, body.site_id, mode=body.mode,
                                       limit_n=body.limit_n, requested_by=operator, process_files=body.process_files)
            except jobs.ActiveJobError as exc:
                raise HTTPException(status_code=409,detail="site already has an active job") from exc
        finally:
            conn.close()
        return {"id": jid}

    @app.get('/sites/{site_id}/capabilities', dependencies=[Depends(require_operator)])
    def get_capabilities(site_id: str):
        from delivery.capabilities import capabilities, CapabilityError
        try:
            return capabilities(site_id)
        except CapabilityError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post('/jobs/date-plan')
    def post_date_plan(body: DatePlanIn, operator: str = Depends(require_operator)):
        from delivery.worker import scopes
        if not body.trial:
            raise HTTPException(status_code=422, detail='검증 전 날짜 범위 기능입니다. 시험 실행 확인이 필요합니다.')
        conn = _conn()
        try:
            if conn.execute('SELECT 1 FROM sites WHERE site_id=%s', (body.site_id,)).fetchone() is None:
                raise HTTPException(status_code=404, detail='site not found')
            try:
                jid = scopes.enqueue_date_plan(conn, site_id=body.site_id, lower=body.lower, upper=body.upper,
                    ranges=[r.model_dump() for r in body.ranges], observation_id=body.observation_id,
                    limit_n=body.limit_n, requested_by=operator, process_files=body.process_files)
            except jobs.ActiveJobError as exc:
                raise HTTPException(status_code=409, detail='site already has an active job') from exc
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise HTTPException(status_code=422, detail='날짜 범위·총량 관측 계획이 올바르지 않습니다.') from exc
            return {'id': jid, 'scope': jobs.job_detail(conn, jid)['scope']}
        finally:
            conn.close()

    @app.post('/files/backfill')
    def post_file_backfill(body: FileBackfillIn, operator: str = Depends(require_operator)):
        if body.collected_from and body.collected_to and body.collected_from > body.collected_to:
            raise HTTPException(status_code=422, detail='수집 기간의 시작일이 종료일보다 늦습니다.')
        conn = _conn()
        try:
            if conn.execute('SELECT 1 FROM sites WHERE site_id=%s', (body.site_id,)).fetchone() is None:
                raise HTTPException(status_code=404, detail='site not found')
            return file_jobs.backfill(conn, body.site_id, after_seq=body.after_seq,
                                      limit=body.limit_n, include_existing=body.include_existing,
                                      dry_run=body.dry_run, requested_by=operator,
                                      collected_from=body.collected_from, collected_to=body.collected_to)
        finally:
            conn.close()

    # Declared before /jobs/{job_id}: FastAPI matches in declaration order, and
    # the literal path must win over the parameterised one.
    #
    # Not behind the aggregate TTL cache, unlike /stats and /freshness. Its
    # 30-second window would leave the queue count unchanged for half a minute
    # after a bulk stop, and an operator who cannot see their cancellation land
    # presses it again. Three queries run here: the status GROUP BY can use
    # idx_jobs_status_created(status, created_at), but the finished_24h count
    # filters on finished_at and the estimate does ORDER BY finished_at DESC --
    # neither is served by that index. A crawl_jobs(finished_at DESC) index is
    # the deliberate follow-up if job history grows large, not part of this
    # wave.
    @app.get("/jobs/summary")
    def get_job_summary():
        conn = _conn()
        try:
            return jobs.summarize_queue(conn)
        finally:
            conn.close()

    @app.get("/progress")
    def get_backfill_progress():
        conn = _conn()
        try:
            rows = conn.execute(
                """
                WITH tracked AS (
                    SELECT site_id FROM crawl_site_progress
                    UNION
                    SELECT site_id FROM crawl_jobs WHERE mode='backfill'
                      AND status IN ('queued','running','cancelling')
                )
                SELECT tracked.site_id, p.cursor, COALESCE(p.items_done, 0) AS items_done, p.total_estimate,
                       COALESCE(p.updated_at, active.created_at) AS updated_at, p.completed_at,
                       active.status AS active_status, active.id AS active_job_id,
                       (SELECT count(*) FROM documents d WHERE d.site_id = tracked.site_id) AS docs_in_db
                  FROM tracked
                  LEFT JOIN crawl_site_progress p ON p.site_id = tracked.site_id
                  LEFT JOIN LATERAL (
                    SELECT j.id, j.status, j.created_at FROM crawl_jobs j
                     WHERE j.site_id = tracked.site_id AND j.mode = 'backfill'
                       AND j.status IN ('queued','running','cancelling')
                     ORDER BY CASE j.status WHEN 'cancelling' THEN 0
                                            WHEN 'running' THEN 1 ELSE 2 END, j.id DESC
                     LIMIT 1
                  ) active ON true
                 ORDER BY p.completed_at NULLS FIRST, p.updated_at DESC
                """).fetchall()
            return {"sites": [dict(r) for r in rows]}
        finally:
            conn.close()

    @app.get("/jobs/{job_id}")
    def get_job(job_id: int):
        conn = _conn()
        try:
            row = jobs.job_detail(conn,job_id)
            if row is not None and row["job"].get("process_files"):
                row["job"]["files"] = file_jobs.summary(conn, job_id)
        finally:
            conn.close()
        if row is None:
            raise HTTPException(status_code=404, detail="job not found")
        return {**row["job"],"logs":row["logs"], **({'scope': row['scope']} if 'scope' in row else {})}

    @app.post("/jobs/{job_id}/files/{action}")
    def manage_job_files(job_id: int, action: Literal["retry","cancel"], operator: str = Depends(require_operator)):
        conn = _conn()
        try:
            job = conn.execute("SELECT process_files,status FROM crawl_jobs WHERE id=%s", (job_id,)).fetchone()
            if job is None:
                raise HTTPException(status_code=404, detail="job not found")
            if not job['process_files'] or job['status'] not in ('done','failed'):
                raise HTTPException(status_code=409, detail="file actions require a finished metadata job")
            changed = file_jobs.retry_failed(conn, job_id) if action == "retry" else file_jobs.cancel(conn, job_id)
            return {"changed": changed, "files": file_jobs.summary(conn, job_id)}
        finally:
            conn.close()

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
            from delivery.capabilities import validate_request, CapabilityError
            if body.enabled:
                try:
                    validate_request(site_id, body.mode, trial=body.trial)
                except CapabilityError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
            try:return schedules.upsert(conn,site_id=site_id,interval_hours=body.interval_hours,
              mode=body.mode,limit_n=body.limit_n,enabled=body.enabled,created_by=operator)
            except ValueError as exc:raise HTTPException(status_code=422,detail=str(exc)) from exc
        finally:conn.close()

    @app.delete("/schedules/{site_id}")
    def delete_schedule(site_id: str, operator: str = Depends(require_operator)):
        conn = _conn()
        try:
            if not schedules.delete(conn, site_id):
                raise HTTPException(status_code=404, detail="schedule not found")
            return {"deleted": True, "site_id": site_id}
        finally:
            conn.close()

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
            source = conn.execute('SELECT site_id,mode,scope_id FROM crawl_jobs WHERE id=%s', (job_id,)).fetchone()
            if source:
                from delivery.capabilities import validate_request, CapabilityError
                try:
                    if source.get('scope_id') is not None:
                        from crawler.sites import CRAWLERS
                        from delivery.worker import scopes
                        if not body or not body.trial or source['mode'] != 'backfill':
                            raise CapabilityError('검증 전 날짜 범위 기능입니다. 시험 실행 확인이 필요합니다.')
                        scopes.load(conn, source['scope_id'], source['site_id'], CRAWLERS[source['site_id']])
                    else:
                        validate_request(source['site_id'], source['mode'], trial=bool(body and body.trial))
                except (CapabilityError, ValueError, KeyError, TypeError, OverflowError) as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
            try:
                row = jobs.retry_job(conn, job_id, requested_by=operator)
            except jobs.ActiveJobError as exc:
                raise HTTPException(status_code=409, detail='site already has an active job') from exc
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

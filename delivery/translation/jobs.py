"""Persistent translation jobs and worker transitions."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from typing import Iterable

from .providers import ProviderError, SummaryRequest, TranslationRequest, TranslationProvider
from .quality import GATE_VERSION, evaluate_summary
from .safety import estimate, evaluate_circuit, refresh_batch
from .observations import record as record_observation

FIELDS = {"title": "title", "description": "abstract"}
TASKS = {
    "title_translation": ("title", "title", "translate"),
    "abstract_summary": ("description", "abstract", "summarize"),
}


def _event(name: str, job: dict, **values) -> None:
    payload={"event":name,"worker_id":job.get("worker_id"),"batch_id":job.get("batch_id"),
             "job_id":job.get("id"),"attempt_id":job.get("attempt_id"),**values}
    print(json.dumps(payload,separators=(",",":"),ensure_ascii=True),file=sys.stdout,flush=True)


def _endpoint_observation(conn, job: dict, healthy: bool) -> None:
    record_observation(conn,worker_id=job.get("worker_id") or "worker-default",
      provider=job["provider"],model_version=job["model_version"],metric="endpoint_healthy",value=healthy)


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _chunks(text: str, limit: int) -> list[str]:
    """Split without dropping input; prefer paragraph/line boundaries."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for part in re.split(r"(\n+)", text):
        if len(part) > limit:
            if current: chunks.append(current); current = ""
            chunks.extend(part[index:index + limit] for index in range(0, len(part), limit))
        elif len(current) + len(part) > limit:
            chunks.append(current); current = part
        else:
            current += part
    if current: chunks.append(current)
    return [chunk for chunk in chunks if chunk]


def _assert_preserved(source: str, translated: str) -> None:
    urls = re.findall(r"https?://[^\s)\]}]+", source)
    numbers = re.findall(r"(?<!\w)\d[\d,.:/%+-]*", source)
    missing = [token for token in (*urls, *numbers) if token not in translated]
    if missing:
        raise ProviderError("invalid_response", "translation omitted protected tokens", retryable=True)


def source_facts(text: str, meta_url: str | None = None) -> dict:
    urls = list(dict.fromkeys(re.findall(r"https?://[^\s)\]}]+", text)))
    if meta_url and meta_url not in urls: urls.append(meta_url)
    dates = list(dict.fromkeys(re.findall(r"(?<!\w)(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})(?!\w)", text)))
    numbers = list(dict.fromkeys(re.findall(r"(?<!\w)\d[\d,.:%+-]*(?!\w)", text)))
    return {"urls": urls[:20], "dates": dates[:30], "numbers": numbers[:100]}


def _selected_tasks(*, tasks: Iterable[str] | None = None,
                    fields: Iterable[str] | None = None) -> tuple[str, ...]:
    if tasks is not None:
        selected = tuple(dict.fromkeys(tasks))
        if not selected or any(task not in TASKS for task in selected): raise ValueError("invalid tasks")
        return selected
    selected_fields = tuple(dict.fromkeys(fields or ("title", "description")))
    if not selected_fields or any(field not in FIELDS for field in selected_fields):
        raise ValueError("invalid source fields")
    return tuple("title_translation" if field == "title" else "abstract_summary" for field in selected_fields)


def preview_targets(conn, *, fields: Iterable[str] | None = None, tasks: Iterable[str] | None = None, lang: str | None = None,
                    site_id: str | None = None, provider: str = "internal", model_version: str = "unknown",
                    prompt_version: str = "title-summary-ko-v1", limit: int = 100) -> dict:
    selected = _selected_tasks(tasks=tasks, fields=fields)
    clauses, params = [], []
    if lang:
        clauses.append("COALESCE(dl.lang, 'unknown')=%s"); params.append(lang)
    if site_id:
        clauses.append("d.site_id=%s"); params.append(site_id)
    where = " AND ".join(clauses) if clauses else "TRUE"
    counts, input_chars = {}, 0
    for task in selected:
        _field, column, _task_type = TASKS[task]
        row = conn.execute(f"""SELECT count(*) AS n,COALESCE(sum(length(d.{column})),0) AS chars FROM documents d
            LEFT JOIN document_lang dl USING(seq_id)
            WHERE {where} AND length(trim(COALESCE(d.{column}, ''))) > 0""", params).fetchone()
        counts[task] = row["n"]
        input_chars += row["chars"]
    total = sum(counts.values())
    bounded = min(total, limit)
    bounded_chars = round(input_chars * bounded / total) if total else 0
    return {"counts": counts, "total": total, **estimate(conn,input_chars=bounded_chars,jobs=bounded,
        provider=provider,model_version=model_version,prompt_version=prompt_version),
        "safety": evaluate_circuit(conn,provider=provider,model_version=model_version,
                                   prompt_version=prompt_version,requested_limit=limit)}


def enqueue_targets(conn, *, fields: Iterable[str] | None = None, tasks: Iterable[str] | None = None, provider: str, model_version: str,
                    prompt_version: str, target_locale: str = "ko-KR", lang: str | None = None,
                    site_id: str | None = None, limit: int = 100, requested_by: str | None = None) -> dict:
    if provider not in ("external", "internal") or not model_version or not prompt_version:
        raise ValueError("invalid provider configuration")
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    selected = _selected_tasks(tasks=tasks, fields=fields)
    safety = evaluate_circuit(conn,provider=provider,model_version=model_version,
                              prompt_version=prompt_version,requested_limit=limit)
    if safety["open"]:
        raise RuntimeError("circuit_open:" + ",".join(safety["reason_codes"]))
    batch = conn.execute("""INSERT INTO translation_batches
      (provider,model_version,prompt_version,tasks,filters,requested_limit,target_count,requested_by)
      VALUES(%s,%s,%s,%s::jsonb,%s::jsonb,%s,0,%s) RETURNING id""",
      (provider,model_version,prompt_version,json.dumps(selected),json.dumps({"lang":lang,"site_id":site_id}),
       limit,requested_by)).fetchone()
    batch_id = batch["id"]
    created, existing = [], 0
    candidates: dict[str, list[dict]] = {}
    for task in selected:
        field, column, task_type = TASKS[task]
        clauses, params = [f"length(trim(COALESCE(d.{column}, ''))) > 0"], []
        if lang: clauses.append("COALESCE(dl.lang, 'unknown')=%s"); params.append(lang)
        if site_id: clauses.append("d.site_id=%s"); params.append(site_id)
        rows = conn.execute(f"""SELECT d.seq_id, d.{column} AS source_text,
                                      COALESCE(dl.lang, 'unknown') AS source_lang
              FROM documents d LEFT JOIN document_lang dl USING(seq_id)
             WHERE {' AND '.join(clauses)} ORDER BY d.seq_id LIMIT %s""", (*params, limit)).fetchall()
        candidates[task] = [dict(row) for row in rows]
    # Round-robin by task so selecting title+summary cannot consume the whole
    # batch with titles before the first summary is queued.
    for index in range(max((len(rows) for rows in candidates.values()), default=0)):
        for task in selected:
            rows = candidates[task]
            if index >= len(rows):
                continue
            field, _column, task_type = TASKS[task]
            row = rows[index]
            source_fp = fingerprint(row["source_text"])
            inserted = conn.execute("""
                INSERT INTO translation_jobs
                  (seq_id, source_field, task_type, source_fingerprint, source_lang, target_locale,
                   provider, model_version, prompt_version, requested_by,batch_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING RETURNING id
            """, (row["seq_id"], field, task_type, source_fp, row["source_lang"], target_locale,
                  provider, model_version, prompt_version, requested_by,batch_id)).fetchone()
            if inserted: created.append(inserted["id"])
            else: existing += 1
            if len(created) >= limit:
                break
        if len(created) >= limit:
            break
    conn.execute("""UPDATE translation_batches SET target_count=%s,created_count=%s,existing_count=%s,
      status=CASE WHEN %s=0 THEN 'completed' ELSE 'queued' END,
      finished_at=CASE WHEN %s=0 THEN now() ELSE NULL END,updated_at=now() WHERE id=%s""",
      (len(created)+existing,len(created),existing,len(created),len(created),batch_id))
    conn.commit()
    return {"batch_id":batch_id,"created": len(created), "existing": existing, "job_ids": created,"safety":safety}


def claim_next(conn, worker_id: str = "worker-default", lease_seconds: int = 600) -> dict | None:
    row = conn.execute("""
      UPDATE translation_jobs SET status='running', claimed_at=now(), updated_at=now(), attempts=attempts+1,
        worker_id=%s,lease_expires_at=now()+make_interval(secs=>%s)
       WHERE id=(SELECT id FROM translation_jobs WHERE status='pending' AND next_attempt_at<=now()
                  ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1)
      RETURNING *
    """,(worker_id,lease_seconds)).fetchone()
    if row:
        attempt = conn.execute("""INSERT INTO translation_job_attempts
          (job_id,batch_id,attempt_no,worker_id,status) VALUES(%s,%s,%s,%s,'running') RETURNING id""",
          (row["id"],row["batch_id"],row["attempts"],worker_id)).fetchone()
        row = dict(row); row["attempt_id"] = attempt["id"]
        conn.execute("""INSERT INTO translation_worker_heartbeats(worker_id,status,last_seen_at,current_job_id)
          VALUES(%s,'running',now(),%s) ON CONFLICT(worker_id) DO UPDATE SET status='running',
          last_seen_at=now(),current_job_id=EXCLUDED.current_job_id""",(worker_id,row["id"]))
        refresh_batch(conn,row["batch_id"])
    conn.commit()
    return dict(row) if row else None


def run_job(conn, job: dict, provider: TranslationProvider) -> bool:
    column = FIELDS[job["source_field"]]
    row = conn.execute(f"SELECT title, meta_url, {column} AS source_text FROM documents WHERE seq_id=%s", (job["seq_id"],)).fetchone()
    if not row or not row["source_text"] or fingerprint(row["source_text"]) != job["source_fingerprint"]:
        conn.execute("UPDATE translation_jobs SET status='skipped', error_code='source_changed', finished_at=now(), updated_at=now() WHERE id=%s AND status='running'", (job["id"],))
        conn.execute("""UPDATE translation_job_attempts SET status='skipped',error_code='source_changed',
          retryable=false,finished_at=now() WHERE id=%s""",(job.get("attempt_id"),))
        refresh_batch(conn,job.get("batch_id"))
        _event("translation_attempt_finished",job,status="skipped",error_code="source_changed")
        conn.commit(); return False
    try:
        if (provider.name != job["provider"] or provider.model != job["model_version"]
                or provider.prompt_version != job["prompt_version"]):
            raise ProviderError("configuration", "provider provenance does not match queued job", retryable=False)
        if job.get("task_type", "translate") == "summarize":
            result = provider.summarize(SummaryRequest(row["title"] or "", row["source_text"],
                                        job["source_lang"], job["target_locale"]))
            facts = source_facts(row["source_text"], row["meta_url"])
            saved = conn.execute("""
              INSERT INTO document_summaries
                (seq_id,target_locale,source_fingerprint,model_version,prompt_version,state,
                 summary_text,key_points,institutions,source_facts,completed_at,updated_at)
              VALUES (%s,%s,%s,%s,%s,'completed',%s,%s::jsonb,%s::jsonb,%s::jsonb,now(),now())
              ON CONFLICT (seq_id,target_locale,source_fingerprint,model_version,prompt_version)
              DO UPDATE SET state='completed',summary_text=EXCLUDED.summary_text,
                key_points=EXCLUDED.key_points,institutions=EXCLUDED.institutions,
                source_facts=EXCLUDED.source_facts,completed_at=now(),updated_at=now()
              RETURNING summary_id
            """, (job["seq_id"], job["target_locale"], job["source_fingerprint"],
                  result.model_version, result.prompt_version, result.summary_text,
                  json.dumps(result.key_points, ensure_ascii=False),
                  json.dumps(result.institutions, ensure_ascii=False),
                  json.dumps(facts, ensure_ascii=False))).fetchone()
            quality = evaluate_summary(title=row["title"] or "", source_text=row["source_text"],
                                      summary_text=result.summary_text, key_points=result.key_points,
                                      institutions=result.institutions)
            conn.execute("""
              INSERT INTO document_summary_quality
                (summary_id,gate_version,decision,score,reason_codes,checks,evidence,evaluated_at)
              VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,now())
              ON CONFLICT (summary_id) DO UPDATE SET gate_version=EXCLUDED.gate_version,
                decision=EXCLUDED.decision,score=EXCLUDED.score,reason_codes=EXCLUDED.reason_codes,
                checks=EXCLUDED.checks,evidence=EXCLUDED.evidence,evaluated_at=now()
            """, (saved["summary_id"], GATE_VERSION, quality.decision, quality.score,
                  json.dumps(quality.reason_codes), json.dumps(quality.checks),
                  json.dumps(quality.evidence)))
            conn.execute("""UPDATE translation_jobs SET status='completed',error_code=NULL,error_message=NULL,
              input_chars=%s,output_chars=%s,latency_ms=%s,finished_at=now(),updated_at=now()
              WHERE id=%s AND status='running'""",
              (result.input_chars, result.output_chars, result.latency_ms, job["id"]))
            conn.execute("""UPDATE translation_job_attempts SET status='completed',prompt_tokens=%s,
              completion_tokens=%s,finish_reason=%s,input_chars=%s,output_chars=%s,latency_ms=%s,
              finished_at=now() WHERE id=%s""",(result.prompt_tokens,result.completion_tokens,
              result.finish_reason,result.input_chars,result.output_chars,result.latency_ms,job.get("attempt_id")))
            refresh_batch(conn,job.get("batch_id"))
            _endpoint_observation(conn,job,True)
            _event("translation_attempt_finished",job,status="completed",latency_ms=result.latency_ms,
                   prompt_tokens=result.prompt_tokens,completion_tokens=result.completion_tokens)
            conn.commit(); return True
        results = [provider.translate(TranslationRequest(chunk, job["source_lang"],
                                      job["target_locale"], job["source_field"]))
                   for chunk in _chunks(row["source_text"], provider.max_chars)]
        translated_text = "".join(result.text for result in results)
        _assert_preserved(row["source_text"], translated_text)
    except ProviderError as exc:
        retry = exc.retryable and job["attempts"] < job["max_attempts"]
        conn.execute("""UPDATE translation_jobs SET status=%s, error_code=%s, error_message=%s,
            next_attempt_at=CASE WHEN %s THEN now() + make_interval(secs => LEAST(3600, 30 * power(2, attempts-1))::int) ELSE next_attempt_at END,
            finished_at=CASE WHEN %s THEN NULL ELSE now() END, updated_at=now() WHERE id=%s AND status='running'""",
            ("pending" if retry else "failed", exc.code, str(exc)[:500], retry, retry, job["id"]))
        conn.execute("""UPDATE translation_job_attempts SET status='failed',error_code=%s,retryable=%s,
          finished_at=now() WHERE id=%s""",(exc.code,exc.retryable,job.get("attempt_id")))
        refresh_batch(conn,job.get("batch_id"))
        if exc.code != "configuration":
            _endpoint_observation(conn,job,False)
        _event("translation_attempt_finished",job,status="failed",error_code=exc.code)
        conn.commit(); return False
    conn.execute("""
      INSERT INTO document_translations
        (seq_id, source_field, target_locale, source_fingerprint, model_version, prompt_version,
         state, translation_text, attempts, completed_at, updated_at)
      VALUES (%s,%s,%s,%s,%s,%s,'completed',%s,%s,now(),now())
      ON CONFLICT (seq_id, source_field, target_locale, source_fingerprint, model_version, prompt_version)
      DO UPDATE SET state='completed', translation_text=EXCLUDED.translation_text,
                    attempts=EXCLUDED.attempts, last_error_code=NULL, completed_at=now(), updated_at=now()
    """, (job["seq_id"], job["source_field"], job["target_locale"], job["source_fingerprint"],
          results[0].model_version, results[0].prompt_version, translated_text, job["attempts"]))
    conn.execute("""UPDATE translation_jobs SET status='completed', error_code=NULL, error_message=NULL,
      input_chars=%s, output_chars=%s, latency_ms=%s, finished_at=now(), updated_at=now()
      WHERE id=%s AND status='running'""", (sum(r.input_chars for r in results),
          sum(r.output_chars for r in results), sum(r.latency_ms for r in results), job["id"]))
    conn.execute("""UPDATE translation_job_attempts SET status='completed',prompt_tokens=%s,
      completion_tokens=%s,finish_reason=%s,input_chars=%s,output_chars=%s,latency_ms=%s,
      finished_at=now() WHERE id=%s""",(sum(r.prompt_tokens or 0 for r in results) or None,
      sum(r.completion_tokens or 0 for r in results) or None,results[-1].finish_reason,
      sum(r.input_chars for r in results),sum(r.output_chars for r in results),
      sum(r.latency_ms for r in results),job.get("attempt_id")))
    refresh_batch(conn,job.get("batch_id"))
    _endpoint_observation(conn,job,True)
    _event("translation_attempt_finished",job,status="completed",latency_ms=sum(r.latency_ms for r in results),
           prompt_tokens=sum(r.prompt_tokens or 0 for r in results) or None,
           completion_tokens=sum(r.completion_tokens or 0 for r in results) or None)
    conn.commit(); return True


def run_once(conn, factory, worker_id: str = "worker-default") -> bool:
    job = claim_next(conn,worker_id=worker_id)
    if not job: return False
    try:
        provider = factory(job["provider"], model_version=job["model_version"],
                           prompt_version=job["prompt_version"])
    except ProviderError as exc:
        conn.execute("UPDATE translation_jobs SET status='failed', error_code=%s, error_message=%s, finished_at=now(), updated_at=now() WHERE id=%s", (exc.code, str(exc)[:500], job["id"]))
        conn.execute("UPDATE translation_job_attempts SET status='failed',error_code=%s,retryable=%s,finished_at=now() WHERE id=%s",(exc.code,exc.retryable,job.get("attempt_id")))
        refresh_batch(conn,job.get("batch_id"))
        _event("translation_attempt_finished",job,status="failed",error_code=exc.code)
        conn.execute("""UPDATE translation_worker_heartbeats SET status='idle',last_seen_at=now(),
          current_job_id=NULL WHERE worker_id=%s""",(worker_id,))
        conn.commit(); return True
    try:
        run_job(conn, job, provider)
    except Exception:  # noqa: BLE001 - do not expose source/provider payload in logs
        conn.rollback()
        conn.execute("""UPDATE translation_jobs SET status='failed',error_code='internal',
          error_message='unexpected worker failure',finished_at=now(),updated_at=now() WHERE id=%s""",(job["id"],))
        conn.execute("""UPDATE translation_job_attempts SET status='failed',error_code='internal',
          retryable=true,finished_at=now() WHERE id=%s""",(job.get("attempt_id"),))
        refresh_batch(conn,job.get("batch_id"));conn.commit()
        _event("translation_attempt_finished",job,status="failed",error_code="internal")
    conn.execute("""UPDATE translation_worker_heartbeats SET status='idle',last_seen_at=now(),
      current_job_id=NULL WHERE worker_id=%s""",(worker_id,));conn.commit()
    return True


def recover_stale(conn) -> int:
    rows=conn.execute("""SELECT id,batch_id,attempts,max_attempts FROM translation_jobs
      WHERE status='running' AND lease_expires_at<now() FOR UPDATE SKIP LOCKED""").fetchall()
    for row in rows:
        retry=row["attempts"]<row["max_attempts"]
        conn.execute("""UPDATE translation_job_attempts SET status='stale_lease',error_code='worker_lease_expired',
          retryable=%s,finished_at=now() WHERE job_id=%s AND attempt_no=%s AND status='running'""",
          (retry,row["id"],row["attempts"]))
        conn.execute("""UPDATE translation_jobs SET status=%s,error_code='worker_lease_expired',
          worker_id=NULL,lease_expires_at=NULL,next_attempt_at=now(),finished_at=CASE WHEN %s THEN NULL ELSE now() END,
          updated_at=now() WHERE id=%s""",("pending" if retry else "failed",retry,row["id"]))
        refresh_batch(conn,row["batch_id"])
    conn.commit();return len(rows)


def list_jobs(conn, *, status=None, limit=50, offset=0) -> list[dict]:
    params = []
    where = ""
    if status: where="WHERE status=%s"; params.append(status)
    params.extend((limit, offset))
    return [dict(row) for row in conn.execute(f"SELECT * FROM translation_jobs {where} ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s", params).fetchall()]


def cancel(conn, job_id: int) -> dict | None:
    row=conn.execute("UPDATE translation_jobs SET status='cancelled',finished_at=now(),updated_at=now() WHERE id=%s AND status='pending' RETURNING *",(job_id,)).fetchone()
    if row:refresh_batch(conn,row["batch_id"])
    conn.commit()
    return dict(row) if row else None


def retry(conn, job_id: int) -> dict | None:
    row=conn.execute("UPDATE translation_jobs SET status='pending',next_attempt_at=now(),finished_at=NULL,error_code=NULL,error_message=NULL,updated_at=now() WHERE id=%s AND status='failed' AND attempts<max_attempts RETURNING *",(job_id,)).fetchone()
    if row:refresh_batch(conn,row["batch_id"])
    conn.commit()
    return dict(row) if row else None

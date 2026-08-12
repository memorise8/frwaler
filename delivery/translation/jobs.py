"""Persistent translation jobs and worker transitions."""
from __future__ import annotations

import hashlib
from typing import Iterable

from .providers import ProviderError, TranslationRequest, TranslationProvider

FIELDS = {"title": "title", "description": "abstract"}


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def preview_targets(conn, *, fields: Iterable[str], lang: str | None = None,
                    site_id: str | None = None) -> dict:
    selected = tuple(dict.fromkeys(fields))
    if not selected or any(field not in FIELDS for field in selected):
        raise ValueError("invalid source fields")
    clauses, params = [], []
    if lang:
        clauses.append("COALESCE(dl.lang, 'unknown')=%s"); params.append(lang)
    if site_id:
        clauses.append("d.site_id=%s"); params.append(site_id)
    where = " AND ".join(clauses) if clauses else "TRUE"
    counts = {}
    for field in selected:
        column = FIELDS[field]
        row = conn.execute(f"""SELECT count(*) AS n FROM documents d
            LEFT JOIN document_lang dl USING(seq_id)
            WHERE {where} AND length(trim(COALESCE(d.{column}, ''))) > 0""", params).fetchone()
        counts[field] = row["n"]
    return {"counts": counts, "total": sum(counts.values())}


def enqueue_targets(conn, *, fields: Iterable[str], provider: str, model_version: str,
                    prompt_version: str, target_locale: str = "ko-KR", lang: str | None = None,
                    site_id: str | None = None, limit: int = 100, requested_by: str | None = None) -> dict:
    if provider not in ("external", "internal") or not model_version or not prompt_version:
        raise ValueError("invalid provider configuration")
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    selected = tuple(dict.fromkeys(fields))
    if not selected or any(field not in FIELDS for field in selected):
        raise ValueError("invalid source fields")
    created, existing = [], 0
    for field in selected:
        column = FIELDS[field]
        clauses, params = [f"length(trim(COALESCE(d.{column}, ''))) > 0"], []
        if lang: clauses.append("COALESCE(dl.lang, 'unknown')=%s"); params.append(lang)
        if site_id: clauses.append("d.site_id=%s"); params.append(site_id)
        rows = conn.execute(f"""SELECT d.seq_id, d.{column} AS source_text,
                                      COALESCE(dl.lang, 'unknown') AS source_lang
              FROM documents d LEFT JOIN document_lang dl USING(seq_id)
             WHERE {' AND '.join(clauses)} ORDER BY d.seq_id LIMIT %s""", (*params, limit - len(created))).fetchall()
        for row in rows:
            source_fp = fingerprint(row["source_text"])
            inserted = conn.execute("""
                INSERT INTO translation_jobs
                  (seq_id, source_field, source_fingerprint, source_lang, target_locale,
                   provider, model_version, prompt_version, requested_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING RETURNING id
            """, (row["seq_id"], field, source_fp, row["source_lang"], target_locale,
                  provider, model_version, prompt_version, requested_by)).fetchone()
            if inserted: created.append(inserted["id"])
            else: existing += 1
            if len(created) >= limit: break
        if len(created) >= limit: break
    conn.commit()
    return {"created": len(created), "existing": existing, "job_ids": created}


def claim_next(conn) -> dict | None:
    row = conn.execute("""
      UPDATE translation_jobs SET status='running', claimed_at=now(), updated_at=now(), attempts=attempts+1
       WHERE id=(SELECT id FROM translation_jobs WHERE status='pending' AND next_attempt_at<=now()
                  ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1)
      RETURNING *
    """).fetchone()
    conn.commit()
    return dict(row) if row else None


def run_job(conn, job: dict, provider: TranslationProvider) -> bool:
    column = FIELDS[job["source_field"]]
    row = conn.execute(f"SELECT {column} AS source_text FROM documents WHERE seq_id=%s", (job["seq_id"],)).fetchone()
    if not row or not row["source_text"] or fingerprint(row["source_text"]) != job["source_fingerprint"]:
        conn.execute("UPDATE translation_jobs SET status='skipped', error_code='source_changed', finished_at=now(), updated_at=now() WHERE id=%s AND status='running'", (job["id"],))
        conn.commit(); return False
    try:
        result = provider.translate(TranslationRequest(row["source_text"], job["source_lang"],
                                                       job["target_locale"], job["source_field"]))
    except ProviderError as exc:
        retry = exc.retryable and job["attempts"] < job["max_attempts"]
        conn.execute("""UPDATE translation_jobs SET status=%s, error_code=%s, error_message=%s,
            next_attempt_at=CASE WHEN %s THEN now() + make_interval(secs => LEAST(3600, 30 * power(2, attempts-1))::int) ELSE next_attempt_at END,
            finished_at=CASE WHEN %s THEN NULL ELSE now() END, updated_at=now() WHERE id=%s AND status='running'""",
            ("pending" if retry else "failed", exc.code, str(exc)[:500], retry, retry, job["id"]))
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
          result.model_version, result.prompt_version, result.text, job["attempts"]))
    conn.execute("""UPDATE translation_jobs SET status='completed', error_code=NULL, error_message=NULL,
      input_chars=%s, output_chars=%s, latency_ms=%s, finished_at=now(), updated_at=now()
      WHERE id=%s AND status='running'""", (result.input_chars, result.output_chars, result.latency_ms, job["id"]))
    conn.commit(); return True


def run_once(conn, factory) -> bool:
    job = claim_next(conn)
    if not job: return False
    try: provider = factory(job["provider"])
    except ProviderError as exc:
        conn.execute("UPDATE translation_jobs SET status='failed', error_code=%s, error_message=%s, finished_at=now(), updated_at=now() WHERE id=%s", (exc.code, str(exc)[:500], job["id"]))
        conn.commit(); return True
    run_job(conn, job, provider)
    return True


def list_jobs(conn, *, status=None, limit=50, offset=0) -> list[dict]:
    params = []
    where = ""
    if status: where="WHERE status=%s"; params.append(status)
    params.extend((limit, offset))
    return [dict(row) for row in conn.execute(f"SELECT * FROM translation_jobs {where} ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s", params).fetchall()]


def cancel(conn, job_id: int) -> dict | None:
    row=conn.execute("UPDATE translation_jobs SET status='cancelled',finished_at=now(),updated_at=now() WHERE id=%s AND status='pending' RETURNING *",(job_id,)).fetchone(); conn.commit()
    return dict(row) if row else None


def retry(conn, job_id: int) -> dict | None:
    row=conn.execute("UPDATE translation_jobs SET status='pending',next_attempt_at=now(),finished_at=NULL,error_code=NULL,error_message=NULL,updated_at=now() WHERE id=%s AND status='failed' AND attempts<max_attempts RETURNING *",(job_id,)).fetchone(); conn.commit()
    return dict(row) if row else None

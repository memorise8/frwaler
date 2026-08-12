"""Read-only batch and worker operations projections."""
from __future__ import annotations

from datetime import datetime, timezone

from delivery.translation.safety import evaluate_circuit


def list_batches(conn, *, limit: int, offset: int) -> dict:
    rows=conn.execute("""SELECT b.*,
      count(j.id) FILTER(WHERE j.status='completed') AS completed,
      count(j.id) FILTER(WHERE j.status='failed') AS failed,
      count(j.id) FILTER(WHERE j.status IN ('pending','running')) AS active
      FROM translation_batches b LEFT JOIN translation_jobs j ON j.batch_id=b.id
      GROUP BY b.id ORDER BY b.created_at DESC,b.id DESC LIMIT %s OFFSET %s""",(limit,offset)).fetchall()
    return {"batches":[dict(row) for row in rows],"limit":limit,"offset":offset}


def batch_detail(conn, batch_id: int) -> dict | None:
    batch=conn.execute("SELECT * FROM translation_batches WHERE id=%s",(batch_id,)).fetchone()
    if not batch:return None
    metrics=conn.execute("""SELECT count(*) AS attempts,
      count(*) FILTER(WHERE a.status='completed') AS completed,
      count(*) FILTER(WHERE a.status IN ('failed','stale_lease')) AS failed,
      round(avg(a.latency_ms),1) AS latency_average_ms,
      percentile_disc(.95) WITHIN GROUP(ORDER BY a.latency_ms) AS latency_p95_ms,
      sum(a.prompt_tokens) AS prompt_tokens,sum(a.completion_tokens) AS completion_tokens
      FROM translation_job_attempts a WHERE a.batch_id=%s""",(batch_id,)).fetchone()
    quality=conn.execute("""SELECT q.decision,count(*) AS count FROM document_summary_quality q
      JOIN document_summaries s USING(summary_id) JOIN translation_jobs j
      ON j.batch_id=%s AND j.seq_id=s.seq_id AND j.source_fingerprint=s.source_fingerprint
      AND j.model_version=s.model_version AND j.prompt_version=s.prompt_version
      GROUP BY q.decision""",(batch_id,)).fetchall()
    reasons=conn.execute("""SELECT a.error_code,count(*) AS count FROM translation_job_attempts a
      WHERE a.batch_id=%s AND a.error_code IS NOT NULL GROUP BY a.error_code ORDER BY count(*) DESC""",(batch_id,)).fetchall()
    return {"batch":dict(batch),"metrics":dict(metrics),"quality":{r["decision"]:r["count"] for r in quality},
            "errors":[dict(r) for r in reasons]}


def operations(conn, *, provider: str, model_version: str, prompt_version: str) -> dict:
    workers=conn.execute("""SELECT worker_id,status,last_seen_at,current_job_id
      FROM translation_worker_heartbeats ORDER BY last_seen_at DESC""").fetchall()
    observations=conn.execute("""SELECT DISTINCT ON(metric) metric,value_numeric,value_boolean,observed_at
      FROM translation_system_observations ORDER BY metric,observed_at DESC""").fetchall()
    return {"measured_at":datetime.now(timezone.utc),"workers":[dict(r) for r in workers],
            "observations":[dict(r) for r in observations],
            "safety":evaluate_circuit(conn,provider=provider,model_version=model_version,
                                      prompt_version=prompt_version,requested_limit=100)}

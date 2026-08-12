"""Batch safety projections and circuit-breaker decisions."""
from __future__ import annotations

from datetime import datetime, timezone

MIN_FAILURE_SAMPLE = 10
FAILURE_RATE_LIMIT = 0.10
LATENCY_P95_LIMIT_MS = 60_000
DISK_FREE_LIMIT = 20 * 1024**3
GPU_FREE_LIMIT = 1024**3


def evaluate_circuit(conn, *, provider: str, model_version: str, prompt_version: str,
                     requested_limit: int) -> dict:
    recent = conn.execute("""
      SELECT a.status,a.latency_ms FROM translation_job_attempts a
      JOIN translation_jobs j ON j.id=a.job_id
      WHERE j.provider=%s AND j.model_version=%s AND j.prompt_version=%s
        AND a.finished_at IS NOT NULL ORDER BY a.finished_at DESC LIMIT 50
    """, (provider, model_version, prompt_version)).fetchall()
    sample = len(recent)
    failures = sum(row["status"] in ("failed", "stale_lease") for row in recent)
    failure_rate = failures / sample if sample else None
    latencies = sorted(row["latency_ms"] for row in recent if row["latency_ms"] is not None)
    p95 = latencies[min(len(latencies) - 1, max(0, round(.95 * len(latencies) + .5) - 1))] if latencies else None
    observations = {}
    baseline = conn.execute("""SELECT count(*) FILTER(WHERE j.status='completed') AS completed,
      count(*) FILTER(WHERE j.status='failed') AS failed FROM translation_jobs j
      WHERE j.provider=%s AND j.model_version=%s AND j.prompt_version=%s""",
      (provider,model_version,prompt_version)).fetchone()
    quality = conn.execute("""SELECT count(*) AS total,
      count(*) FILTER(WHERE q.decision='rejected') AS rejected
      FROM document_summary_quality q JOIN document_summaries s USING(summary_id)
      WHERE s.model_version=%s AND s.prompt_version=%s""",(model_version,prompt_version)).fetchone()
    for metric in ("endpoint_healthy", "disk_free_bytes", "gpu_memory_free_bytes", "gpu_utilization_percent"):
        row = conn.execute("""SELECT value_numeric,value_boolean,observed_at
          FROM translation_system_observations WHERE metric=%s
            AND observed_at >= now()-interval '15 minutes' ORDER BY observed_at DESC LIMIT 1""", (metric,)).fetchone()
        if row:
            observations[metric] = {"value": row["value_boolean"] if row["value_boolean"] is not None else row["value_numeric"],
                                    "observed_at": row["observed_at"]}
    reasons = []
    if sample >= MIN_FAILURE_SAMPLE and failure_rate is not None and failure_rate > FAILURE_RATE_LIMIT:
        reasons.append("provider_failure_rate")
    if sample >= MIN_FAILURE_SAMPLE and p95 is not None and p95 > LATENCY_P95_LIMIT_MS:
        reasons.append("provider_latency_p95")
    if observations.get("endpoint_healthy", {}).get("value") is False:
        reasons.append("endpoint_unhealthy")
    if observations.get("disk_free_bytes", {}).get("value", DISK_FREE_LIMIT) < DISK_FREE_LIMIT:
        reasons.append("disk_free_low")
    if observations.get("gpu_memory_free_bytes", {}).get("value", GPU_FREE_LIMIT) < GPU_FREE_LIMIT:
        reasons.append("gpu_memory_low")
    baseline_total=(baseline["completed"] or 0)+(baseline["failed"] or 0)
    baseline_failure=(baseline["failed"] or 0)/baseline_total if baseline_total else None
    rejected_rate=(quality["rejected"] or 0)/quality["total"] if quality["total"] else None
    if requested_limit > 100 and (baseline_total < 100 or baseline_failure is None or baseline_failure > .01
                                  or rejected_rate is None or rejected_rate > .001):
        reasons.append("insufficient_baseline")
    return {"open": bool(reasons), "reason_codes": reasons, "recent_sample": sample,
            "failure_rate": round(failure_rate, 4) if failure_rate is not None else None,
            "baseline_sample":baseline_total,
            "baseline_failure_rate":round(baseline_failure,4) if baseline_failure is not None else None,
            "rejected_rate":round(rejected_rate,4) if rejected_rate is not None else None,
            "latency_p95_ms": p95, "observations": observations,
            "evaluated_at": datetime.now(timezone.utc)}


def estimate(conn, *, input_chars: int, jobs: int, provider: str,
             model_version: str, prompt_version: str) -> dict:
    row = conn.execute("""SELECT avg(a.latency_ms) AS latency,avg(a.prompt_tokens) AS prompt_tokens
      FROM translation_job_attempts a JOIN translation_jobs j ON j.id=a.job_id
      WHERE a.status='completed' AND j.provider=%s AND j.model_version=%s AND j.prompt_version=%s""",
      (provider, model_version, prompt_version)).fetchone()
    tokens_per_job = float(row["prompt_tokens"]) if row and row["prompt_tokens"] is not None else input_chars / max(jobs, 1) / 4
    latency_ms = float(row["latency"]) if row and row["latency"] is not None else 10_000
    return {"estimated_input_chars": input_chars,
            "estimated_prompt_tokens": round(tokens_per_job * jobs),
            "estimated_seconds": round(latency_ms * jobs / 1000)}


def refresh_batch(conn, batch_id: int | None) -> None:
    if batch_id is None:
        return
    row = conn.execute("""SELECT count(*) AS total,
      count(*) FILTER(WHERE status IN ('completed','failed','skipped','cancelled')) AS terminal,
      count(*) FILTER(WHERE status='running') AS running FROM translation_jobs WHERE batch_id=%s""",
      (batch_id,)).fetchone()
    status = "completed" if row["total"] and row["terminal"] == row["total"] else ("running" if row["running"] else "queued")
    conn.execute("""UPDATE translation_batches SET status=%s,
      started_at=CASE WHEN %s='running' THEN COALESCE(started_at,now()) ELSE started_at END,
      finished_at=CASE WHEN %s='completed' THEN now() ELSE NULL END,updated_at=now() WHERE id=%s""",
      (status, status, status, batch_id))

"""Read-only quality dashboard projection without source or generated content."""
from __future__ import annotations

from datetime import datetime, timezone

from delivery.translation.quality import GATE_VERSION


def collect_translation_quality(conn, *, decision: str | None = None,
                                limit: int = 50, offset: int = 0) -> dict:
    counts = {row["decision"]: row["count"] for row in conn.execute(
        "SELECT decision,count(*) AS count FROM document_summary_quality GROUP BY decision"
    ).fetchall()}
    aggregate = conn.execute("""
      SELECT count(*) AS total,round(avg(q.score),1) AS average,
             round(avg((q.checks->>'evidence_average')::numeric),1) AS evidence_average
      FROM document_summary_quality q
    """).fetchone()
    reasons = conn.execute("""
      SELECT reason.value AS code,count(*) AS count
      FROM document_summary_quality q
      CROSS JOIN LATERAL jsonb_array_elements_text(q.reason_codes) reason(value)
      GROUP BY reason.value ORDER BY count(*) DESC,reason.value LIMIT 10
    """).fetchall()
    languages = conn.execute("""
      SELECT j.source_lang,count(*) AS total,
        count(*) FILTER (WHERE q.decision='auto_approved') AS auto_approved,
        count(*) FILTER (WHERE q.decision='review_recommended') AS review_recommended,
        count(*) FILTER (WHERE q.decision='rejected') AS rejected
      FROM document_summary_quality q JOIN document_summaries s USING(summary_id)
      JOIN LATERAL (SELECT source_lang FROM translation_jobs j
        WHERE j.seq_id=s.seq_id AND j.source_fingerprint=s.source_fingerprint
          AND j.model_version=s.model_version AND j.prompt_version=s.prompt_version
          AND j.task_type='summarize' ORDER BY j.id DESC LIMIT 1) j ON TRUE
      GROUP BY j.source_lang ORDER BY total DESC,j.source_lang
    """).fetchall()
    where, params = ("WHERE q.decision=%s", [decision]) if decision else ("", [])
    params.extend((limit, offset))
    items = conn.execute(f"""
      SELECT q.summary_id,s.seq_id,j.source_lang,d.site_id,q.decision,q.score,q.reason_codes,
             s.model_version,s.prompt_version,q.evaluated_at
      FROM document_summary_quality q JOIN document_summaries s USING(summary_id)
      JOIN documents d USING(seq_id)
      JOIN LATERAL (SELECT source_lang FROM translation_jobs j
        WHERE j.seq_id=s.seq_id AND j.source_fingerprint=s.source_fingerprint
          AND j.model_version=s.model_version AND j.prompt_version=s.prompt_version
          AND j.task_type='summarize' ORDER BY j.id DESC LIMIT 1) j ON TRUE
      {where} ORDER BY q.evaluated_at DESC,q.summary_id DESC LIMIT %s OFFSET %s
    """, params).fetchall()
    return {
        "measured_at": datetime.now(timezone.utc), "gate_version": GATE_VERSION,
        "summary": {"total": aggregate["total"], **{key: counts.get(key, 0) for key in
                    ("auto_approved", "review_recommended", "rejected")}},
        "score": {"average": aggregate["average"], "evidence_average": aggregate["evidence_average"]},
        "top_reasons": [dict(row) for row in reasons],
        "by_language": [dict(row) for row in languages],
        "items": [dict(row) for row in items], "limit": limit, "offset": offset,
    }

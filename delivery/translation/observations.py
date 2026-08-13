"""Scoped, content-free operational observations with bounded retention."""
from __future__ import annotations


RETENTION_DAYS = 7


def record(conn, *, worker_id: str, metric: str, value, provider: str | None = None,
           model_version: str | None = None) -> None:
    column = "value_boolean" if isinstance(value, bool) else "value_numeric"
    conn.execute(f"""INSERT INTO translation_system_observations
      (worker_id,provider,model_version,metric,{column}) VALUES(%s,%s,%s,%s,%s)""",
      (worker_id,provider,model_version,metric,value))


def prune(conn, *, retention_days: int = RETENTION_DAYS) -> int:
    row=conn.execute("""WITH removed AS (DELETE FROM translation_system_observations
      WHERE observed_at < now()-make_interval(days=>%s) RETURNING 1)
      SELECT count(*) AS count FROM removed""",(retention_days,)).fetchone()
    return row["count"]

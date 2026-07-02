from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from .corpora import CORPORA, REPO_ROOT, corpus_stats
from .db import DEFAULT_OPS_DB, any_running, connect_ops, finish_run, init_ops_schema, start_run

DEFAULT_LOG_DIR = Path("data/ops_logs")


class BusyError(RuntimeError):
    pass


def refresh_corpus(
    key: str, *, ops_db: Path = DEFAULT_OPS_DB, log_dir: Path = DEFAULT_LOG_DIR,
    argv: tuple | None = None, cwd: Path | None = None, timeout: int = 7200,
) -> int:
    c = CORPORA[key]
    conn = connect_ops(ops_db)
    init_ops_schema(conn)
    if any_running(conn):
        conn.close()
        raise BusyError("다른 수집이 실행 중입니다")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{key}_{time.strftime('%Y%m%d_%H%M%S')}.log"
    before = corpus_stats(c)["total"] or 0
    run_id = start_run(conn, key, str(log_path))
    try:
        with log_path.open("w", encoding="utf-8") as fh:
            proc = subprocess.run(
                list(argv or c.argv), stdout=fh, stderr=subprocess.STDOUT,
                env={**os.environ, **c.env}, cwd=str(cwd or REPO_ROOT), timeout=timeout,
            )
        after = corpus_stats(c)["total"] or 0
        if proc.returncode == 0:
            finish_run(conn, run_id, status="ok", new_count=after - before, total_after=after)
        else:
            finish_run(conn, run_id, status="error", new_count=after - before,
                       total_after=after, error=f"exit code {proc.returncode}")
    except Exception as exc:  # timeout 등 — 기록만 하고 raise하지 않음(백그라운드 안전)
        finish_run(conn, run_id, status="error", error=str(exc))
    conn.close()
    return run_id

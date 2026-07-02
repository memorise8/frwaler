from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

from . import runner
from .corpora import CORPORA, corpus_stats
from .db import (DEFAULT_OPS_DB, any_running, connect_ops, init_ops_schema,
                 last_run, mark_stale_running, recent_runs)

DEFAULT_EXPORT_DIR = Path("data/export")


def create_app(ops_db: Path = DEFAULT_OPS_DB,
               export_dir: Path = DEFAULT_EXPORT_DIR) -> FastAPI:

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        conn = connect_ops(ops_db)
        init_ops_schema(conn)
        n = mark_stale_running(conn)
        conn.close()
        if n:
            print(f"[fino_ops] stale running {n}건 error 처리")
        yield

    app = FastAPI(title="fino-ops", lifespan=lifespan)

    @app.get("/api/corpora")
    def corpora_list() -> list[dict]:
        conn = connect_ops(ops_db)
        init_ops_schema(conn)
        busy = any_running(conn)
        out = []
        for key, c in CORPORA.items():
            out.append({"key": key, "label": c.label, **corpus_stats(c),
                        "last_run": last_run(conn, key), "busy": busy})
        conn.close()
        return out

    @app.post("/api/corpora/{key}/refresh", status_code=202)
    def corpus_refresh(key: str, bg: BackgroundTasks) -> dict:
        if key not in CORPORA:
            raise HTTPException(404, f"알 수 없는 코퍼스: {key}")
        conn = connect_ops(ops_db)
        init_ops_schema(conn)
        busy = any_running(conn)
        conn.close()
        if busy:
            raise HTTPException(409, "다른 수집이 실행 중입니다")

        def _task() -> None:
            try:
                runner.refresh_corpus(key, ops_db=ops_db)
            except runner.BusyError:   # 동시 POST 경합 — runner가 최종 방어
                pass

        bg.add_task(_task)
        return {"started": True, "corpus": key}

    @app.get("/api/runs")
    def runs_list(limit: int = 50) -> list[dict]:
        conn = connect_ops(ops_db)
        init_ops_schema(conn)
        rows = recent_runs(conn, limit=limit)
        conn.close()
        return rows

    @app.get("/api/runs/{run_id}/log", response_class=PlainTextResponse)
    def run_log(run_id: int, tail: int = 200) -> str:
        conn = connect_ops(ops_db)
        init_ops_schema(conn)
        row = conn.execute("SELECT log_path FROM runs WHERE id = ?", (run_id,)).fetchone()
        conn.close()
        if row is None:
            raise HTTPException(404, "run 없음")
        p = Path(row["log_path"])
        if not p.exists():
            raise HTTPException(404, "로그 파일 없음")
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-tail:])

    @app.get("/api/export/{key}")
    def export_file(key: str) -> FileResponse:
        path = export_dir / f"{key}.ndjson"
        if not path.exists():
            raise HTTPException(404, f"export 없음: {path.name} (먼저 export 모듈로 생성)")
        return FileResponse(path, media_type="application/x-ndjson", filename=path.name)

    return app


app = create_app()

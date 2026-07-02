from __future__ import annotations

from pathlib import Path
import sqlite3

DEFAULT_OPS_DB = Path("data/fino_ops.db")


def connect_ops(db_path: Path = DEFAULT_OPS_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_ops_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            corpus TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','ok','error')),
            new_count INTEGER,
            total_after INTEGER,
            log_path TEXT NOT NULL DEFAULT '',
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_runs_corpus_id ON runs(corpus, id);
        """
    )
    conn.commit()


def start_run(conn: sqlite3.Connection, corpus: str, log_path: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs (corpus, log_path) VALUES (?, ?)", (corpus, log_path)
    )
    conn.commit()
    return int(cur.lastrowid)


def try_start_run(conn: sqlite3.Connection, corpus: str, log_path: str) -> int | None:
    """원자적 락 획득: running 행이 없을 때만 run을 생성한다(단일 INSERT라 레이스 없음)."""
    cur = conn.execute(
        """
        INSERT INTO runs (corpus, log_path)
        SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM runs WHERE status = 'running')
        """,
        (corpus, log_path),
    )
    conn.commit()
    return int(cur.lastrowid) if cur.rowcount else None


def finish_run(
    conn: sqlite3.Connection, run_id: int, *, status: str,
    new_count: int | None = None, total_after: int | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE runs SET finished_at = datetime('now', 'localtime'),
            status = ?, new_count = ?, total_after = ?, error = ?
        WHERE id = ?
        """,
        (status, new_count, total_after, error, run_id),
    )
    conn.commit()


def any_running(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT 1 FROM runs WHERE status = 'running' LIMIT 1").fetchone()
    return row is not None


def last_run(conn: sqlite3.Connection, corpus: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM runs WHERE corpus = ? ORDER BY id DESC LIMIT 1", (corpus,)
    ).fetchone()
    return dict(row) if row else None


def recent_runs(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def mark_stale_running(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        """
        UPDATE runs SET status = 'error', error = 'stale: 서버 재시작으로 중단 처리',
            finished_at = datetime('now', 'localtime')
        WHERE status = 'running'
        """
    )
    conn.commit()
    return cur.rowcount

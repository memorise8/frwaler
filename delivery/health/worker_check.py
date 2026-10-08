"""Container liveness: recent heartbeat or unexpired active crawl lease."""
import os


def main() -> int:
    try:
        from crawler import db_pg
        from .overview import worker_rows
        conn = db_pg.open_db(os.environ["LIBERTREE_PG_DSN"])
        try:
            conn.execute("SET statement_timeout='5s'")
            worker_id = os.environ.get("TRANSLATION_WORKER_ID", "delivery-worker-1")
            alive = any(r["alive"] for r in worker_rows(conn) if r["worker_id"] == worker_id)
        finally:
            conn.close()
        return 0 if alive else 1
    except Exception:
        return 1  # Docker logs must never contain the DSN or credentials.


if __name__ == "__main__":
    raise SystemExit(main())

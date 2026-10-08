"""Container heartbeat check; never print connection credentials."""
import os


def main():
    try:
        from crawler import db_pg
        with db_pg.open_db(os.environ['LIBERTREE_PG_DSN']) as conn:
            conn.execute("SET statement_timeout='5s'")
            row = conn.execute("SELECT last_seen_at>now()-interval '120 seconds' AS alive FROM file_worker_heartbeat WHERE id=1").fetchone()
            return 0 if row and row['alive'] else 1
    except Exception:
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

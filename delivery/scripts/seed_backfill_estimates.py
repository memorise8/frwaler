# -*- coding: utf-8 -*-
"""capacity_corrected.csv 의 corrected_max 를 crawl_site_progress.total_estimate 로 시드.

10만 건 이상(백필 대상)만 넣는다. 재실행 무해(업서트). 네트워크 접근 없음.

Usage:
    python3 delivery/scripts/seed_backfill_estimates.py   # LIBERTREE_PG_DSN 사용
"""
from __future__ import annotations

import csv
import os
from pathlib import Path

THRESHOLD = 100_000


def seed(conn, csv_path="scripts/audit/capacity_corrected.csv") -> int:
    n = 0
    with Path(csv_path).open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            raw = row.get("corrected_max") or ""
            if not raw:
                continue
            estimate = int(raw)
            if estimate < THRESHOLD:
                continue
            conn.execute(
                """
                INSERT INTO crawl_site_progress (site_id, total_estimate, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (site_id) DO UPDATE
                   SET total_estimate = EXCLUDED.total_estimate, updated_at = now()
                """,
                (row["site_id"], estimate),
            )
            n += 1
    conn.commit()
    return n


if __name__ == "__main__":
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(os.environ["LIBERTREE_PG_DSN"], row_factory=dict_row) as conn:
        print(f"seeded {seed(conn)} sites")

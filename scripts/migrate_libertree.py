#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""libertree migration helper.

기존 SQLite DB 위에 libertree 의 ``documents`` 테이블/인덱스를 추가한다.
``init_db`` 가 idempotent 하므로 사실상 ``init_db`` 를 호출하기만 하면
충분하지만, 운영 환경에서 안전하게 적용하려고 다음을 묶어서 수행한다.

1. ``data/papers.db`` 의 백업 (``papers.db.bak-YYYYMMDD-HHMM``)
2. ``init_db(conn)`` 호출 — 신규 ``documents`` 테이블 + 인덱스 4종 생성
3. 결과 출력 (테이블/인덱스 목록 + 행 수)

Usage:
    python -m scripts.migrate_libertree [--db PATH]
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crawler import db as db_module  # noqa: E402


def _backup(db_path: Path) -> Path | None:
    if not db_path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    backup = db_path.with_name(db_path.name + f".bak-{stamp}")
    shutil.copy2(db_path, backup)
    return backup


def _summary(conn) -> None:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()]
    print("Tables:", tables)
    if "documents" in tables:
        n = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        print(f"  documents row count: {n}")
        idxs = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='documents'"
        ).fetchall()]
        print("  documents indexes:", idxs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=str(ROOT / "data" / "papers.db"),
        help="Path to the SQLite DB (default: data/papers.db)",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip the .bak-YYYYMMDD-HHMM snapshot",
    )
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    print(f"DB: {db_path}")

    if not args.no_backup:
        backup = _backup(db_path)
        if backup:
            print(f"Backup: {backup}")
        else:
            print("Backup: (DB does not exist yet — skipping)")

    os.makedirs(db_path.parent, exist_ok=True)
    conn = db_module.get_db(str(db_path))
    db_module.init_db(conn)

    # Idempotent column adds for DBs that were created before the followup
    # patch (Codex review): documents.metadata holds site-specific raw fields.
    for col in ("metadata TEXT",):
        try:
            conn.execute(f"ALTER TABLE documents ADD COLUMN {col}")
            conn.commit()
            print(f"Added column: documents.{col.split()[0]}")
        except sqlite3.OperationalError:
            pass  # already present

    _summary(conn)
    conn.close()
    print("\nlibertree migration: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

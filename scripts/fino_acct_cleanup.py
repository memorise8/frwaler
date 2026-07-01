"""백업 후 회계 질의회신 재크롤 대상(priority 1~6) 행 삭제."""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path


def cleanup(db_path: Path) -> tuple[int, int]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup = db_path.with_name(f"{db_path.name}.{stamp}.bak")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    # Use SQLite backup API so WAL journal is included in the backup
    dst = sqlite3.connect(backup)
    conn.backup(dst)
    dst.close()
    before = conn.execute("SELECT COUNT(*) FROM acct_documents").fetchone()[0]
    conn.execute("DELETE FROM acct_documents WHERE source_priority BETWEEN 1 AND 6")
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM acct_documents").fetchone()[0]
    conn.close()
    print(f"backup={backup} deleted={before - after} remaining={after}")
    return before, after


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    _ = p.add_argument("--db-path", type=Path, default=Path("data/fino_acct.db"))
    cleanup(p.parse_args().db_path)

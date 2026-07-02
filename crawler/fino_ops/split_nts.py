from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

NTS_SITE_IDS = ("nts-taxlaw-pd", "nts-taxlaw-qt")
DEFAULT_SRC = Path("/data_raid/ruci_workspace/crawler-poc/data/papers.db")
DEFAULT_DEST = Path("data/fino_nts.db")
_TABLES = ("sites", "papers", "doc_index")


def split_nts(src: Path = DEFAULT_SRC, dest: Path = DEFAULT_DEST) -> dict[str, int]:
    if dest.exists():
        raise SystemExit(f"대상이 이미 존재합니다(덮어쓰기 금지): {dest}")
    if not src.exists():
        raise SystemExit(f"소스가 없습니다: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(dest)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("ATTACH DATABASE ? AS src", (str(src),))

    # 1) 소스 DDL 복제(테이블·인덱스만) — 스키마 동일성 보장
    ddl = conn.execute(
        "SELECT sql FROM src.sqlite_master WHERE type IN ('table','index') "
        "AND sql IS NOT NULL AND tbl_name IN (?,?,?) AND name NOT LIKE 'sqlite_%'",
        _TABLES,
    ).fetchall()
    for (sql,) in ddl:
        conn.execute(sql)

    # 2) NTS 행만 복사 (소스는 SELECT만)
    marks = ",".join("?" for _ in NTS_SITE_IDS)
    with conn:
        conn.execute(f"INSERT INTO main.sites SELECT * FROM src.sites WHERE id IN ({marks})",
                     NTS_SITE_IDS)
        conn.execute(f"INSERT INTO main.papers SELECT * FROM src.papers WHERE site_id IN ({marks})",
                     NTS_SITE_IDS)
        conn.execute(f"INSERT INTO main.doc_index SELECT * FROM src.doc_index WHERE site_id IN ({marks})",
                     NTS_SITE_IDS)

    # 3) FTS 생성·rebuild → 4) 트리거는 적재 후 생성(적재 중 이중 색인 방지)
    fts_row = conn.execute("SELECT sql FROM src.sqlite_master WHERE name = 'papers_fts'").fetchone()
    if fts_row:
        conn.execute(fts_row[0])
        conn.execute("INSERT INTO papers_fts(papers_fts) VALUES('rebuild')")
        for (sql,) in conn.execute(
            "SELECT sql FROM src.sqlite_master WHERE type = 'trigger' AND tbl_name = 'papers'"
        ).fetchall():
            conn.execute(sql)
        conn.commit()

    # 5) 검증: site_id별 행수 일치
    counts: dict[str, int] = {}
    for site in NTS_SITE_IDS:
        s = conn.execute("SELECT COUNT(*) FROM src.papers WHERE site_id = ?", (site,)).fetchone()[0]
        d = conn.execute("SELECT COUNT(*) FROM main.papers WHERE site_id = ?", (site,)).fetchone()[0]
        if s != d:
            raise SystemExit(f"검증 실패 {site}: src {s} != dest {d}")
        counts[site] = d
    conn.execute("DETACH DATABASE src")
    conn.close()
    return counts


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m crawler.fino_ops.split_nts")
    _ = p.add_argument("--src", type=Path, default=DEFAULT_SRC)
    _ = p.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    args = p.parse_args()
    counts = split_nts(args.src, args.dest)
    print(f"done: {counts} → {args.dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

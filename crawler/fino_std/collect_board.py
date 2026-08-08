"""공표 게시판 판본 대장 수집 CLI.

    python -m crawler.fino_std.collect_board                 # 전체 게시판
    python -m crawler.fino_std.collect_board --board gaap    # 하나만
    python -m crawler.fino_std.collect_board --diff          # 보유분과 대조만

본문은 받지 않는다. 파일명에 판본이 들어 있어 목록만으로 최신 여부가 갈린다.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

from .board import BOARD_SOURCES, PublishedFile, fetch_board
from .db import connect_db, init_schema, replace_published_files

DEFAULT_DB_PATH = Path("data/fino_std.db")


def collect_boards(
    *, db_path: Path, codes: set[str] | None = None, delay: float = 0.5
) -> dict[str, int]:
    conn = connect_db(db_path)
    init_schema(conn)
    counts: dict[str, int] = {}
    try:
        with httpx.Client(follow_redirects=True) as client:
            for source in BOARD_SOURCES:
                if codes and source.code not in codes:
                    continue
                if delay > 0:
                    time.sleep(delay)
                items = fetch_board(client, source)
                counts[source.code] = replace_published_files(conn, source.code, items)
    finally:
        conn.close()
    return counts


def load_registry(db_path: Path) -> list[PublishedFile]:
    conn = connect_db(db_path)
    init_schema(conn)
    try:
        rows = conn.execute(
            "SELECT board, entry_title, file_name, file_no, file_seq FROM published_files"
            " ORDER BY board, entry_title"
        ).fetchall()
    finally:
        conn.close()
    return [
        PublishedFile(
            board=r["board"],
            entry_title=r["entry_title"],
            file_name=r["file_name"],
            file_no=r["file_no"],
            file_seq=r["file_seq"],
        )
        for r in rows
    ]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m crawler.fino_std.collect_board")
    _ = p.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    _ = p.add_argument(
        "--board",
        action="append",
        choices=[s.code for s in BOARD_SOURCES],
        help="게시판 코드(여러 번 지정 가능). 생략하면 전체",
    )
    _ = p.add_argument("--delay-seconds", type=float, default=0.5)
    _ = p.add_argument("--list", action="store_true", help="수집 없이 대장을 출력한다")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list:
        for item in load_registry(args.db_path):
            print(f"{item.board:<10} {item.ext:<4} {item.file_name}")
        return 0
    counts = collect_boards(
        db_path=args.db_path,
        codes=set(args.board) if args.board else None,
        delay=args.delay_seconds,
    )
    for code, n in counts.items():
        print(f"{code:<10} {n}건")
    print(f"done board: files={sum(counts.values())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

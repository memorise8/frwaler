from __future__ import annotations

import argparse
from pathlib import Path

import httpx

from .db import connect_db, init_schema, replace_paragraphs, upsert_document
from .fetch import fetch_content, fetch_titles, is_unavailable
from .parsers import parse_content, pick_big_sections
from .sources import STD_SEEDS, std_source_url

DEFAULT_DB_PATH = Path("data/fino_std.db")
ALL_TYPES = ("kifrs", "kifrs_interp", "kifrs_etc", "gaap")


def collect_standards(
    *, db_path: Path, types: set[str], std_num: int | None, delay: float
) -> tuple[int, int]:
    docs = 0
    paras = 0
    targets = [t for t in STD_SEEDS if t.std_type in types
               and (std_num is None or t.std_num == std_num)]
    with connect_db(db_path) as conn, httpx.Client(timeout=30) as client:
        init_schema(conn)
        for t in targets:
            titles = fetch_titles(client, t.std_num, delay)
            if is_unavailable(titles):
                print(f"[skip] API 미지원: {t.std_num} {t.title}", flush=True)
                continue
            bigs = pick_big_sections(titles)
            if not bigs:
                print(f"[skip] 빈 목차: {t.std_num} {t.title}", flush=True)
                continue
            records = []
            for big in bigs:
                content = fetch_content(client, t.std_num, big.document_id, delay)
                if is_unavailable(content):
                    print(f"[warn] 섹션 실패: {t.std_num} {big.title}", flush=True)
                    continue
                records.extend(parse_content(content, std_num=t.std_num, start_seq=len(records)))
            doc_id = upsert_document(
                conn, std_num=t.std_num, std_type=t.std_type,
                title=t.title, source_url=std_source_url(t.std_num),
            )
            replace_paragraphs(conn, doc_id, records)
            docs += 1
            paras += len(records)
            print(f"[{t.std_type}] {t.std_num} {t.title}: 섹션 {len(bigs)} 문단 {len(records)}",
                  flush=True)
    return docs, paras


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m crawler.fino_std.collect")
    _ = p.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    _ = p.add_argument("--types", type=str, default=",".join(ALL_TYPES),
                       help="쉼표구분: kifrs,kifrs_interp,kifrs_etc,gaap")
    _ = p.add_argument("--std", type=int, default=None, help="단일 기준서 번호만")
    _ = p.add_argument("--delay-seconds", type=float, default=0.4)
    return p


def main() -> int:
    args = build_parser().parse_args()
    types = {s.strip() for s in args.types.split(",") if s.strip()}
    unknown = types - set(ALL_TYPES)
    if unknown:
        raise SystemExit(f"알 수 없는 타입: {sorted(unknown)}")
    d, a = collect_standards(db_path=args.db_path, types=types,
                             std_num=args.std, delay=args.delay_seconds)
    print(f"done std: documents={d} paragraphs={a}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

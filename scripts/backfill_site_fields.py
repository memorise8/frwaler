#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""커스텀 크롤러를 스크래치 DB에 재실행한 뒤, dedup 키로 본 DB의 지정
필드만 UPDATE 하는 백필 도구.

insert_document 는 dedup 히트 시 UPDATE 하지 않으므로(2026-07-11 확인)
단순 재크롤로는 기존 행이 갱신되지 않는다 — 이 도구가 그 간극을 메운다.

사용:
  .venv/bin/python scripts/backfill_site_fields.py \
      --site mindev-gov-gr-category --fields published_date --apply
"""
import argparse
import importlib.util
import json
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from crawler.db_libertree import find_by_dedup_key, init_db, open_db, upsert_site  # noqa: E402

ALLOWED_FIELDS = {"published_date", "title", "listed_date", "authors", "keywords"}


def load_crawler_class(site_id: str):
    py_path = _PROJECT_ROOT / "crawler" / "sites" / "custom" / f"{site_id}.py"
    if not py_path.exists():
        raise FileNotFoundError(py_path)
    spec = importlib.util.spec_from_file_location(f"backfill_{site_id}", py_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from crawler.base_crawler import BaseCrawler
    for obj in vars(mod).values():
        if (isinstance(obj, type) and issubclass(obj, BaseCrawler)
                and obj is not BaseCrawler):
            return obj
    raise RuntimeError(f"no BaseCrawler subclass in {py_path}")


def _should_update(field: str, old, new) -> bool:
    if not new or str(new).strip() == "":
        return False
    if str(new) == str(old or ""):
        return False
    if field == "title":
        return "�" in (old or "")  # 깨진 제목만 교체
    return not (old or "").strip()      # 그 외: 비어있는 것만 채움


def backfill(main_conn, scratch_conn, site_id, fields, apply: bool) -> dict:
    bad = set(fields) - ALLOWED_FIELDS
    if bad:
        raise ValueError(f"not allowed fields: {sorted(bad)}")
    rep = {"scratch_docs": 0, "matched": 0, "unmatched": 0,
           "updated": {f: 0 for f in fields}}
    rows = scratch_conn.execute(
        f"SELECT post_number, meta_url, {', '.join(fields)} FROM documents"
        " WHERE site_id = ?", (site_id,)).fetchall()
    for row in rows:
        rep["scratch_docs"] += 1
        post_number, meta_url = row[0], row[1]
        seq_id = find_by_dedup_key(main_conn, site_id, post_number, meta_url)
        if seq_id is None:
            rep["unmatched"] += 1
            continue
        rep["matched"] += 1
        current = main_conn.execute(
            f"SELECT {', '.join(fields)} FROM documents WHERE seq_id=?",
            (seq_id,)).fetchone()
        for i, field in enumerate(fields):
            new = row[2 + i]
            if _should_update(field, current[i], new):
                rep["updated"][field] += 1
                if apply:
                    main_conn.execute(
                        f"UPDATE documents SET {field}=? WHERE seq_id=?",
                        (new, seq_id))
    if apply:
        main_conn.commit()
    return rep


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--site", required=True)
    ap.add_argument("--fields", required=True,
                    help="콤마 구분 (예: published_date,title)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--db", default="data/libertree.db")
    args = ap.parse_args()

    fields = [f.strip() for f in args.fields.split(",") if f.strip()]
    bad = set(fields) - ALLOWED_FIELDS
    if bad:
        raise SystemExit(f"not allowed fields: {bad}")

    scratch_path = Path(tempfile.mkstemp(prefix=f"backfill_{args.site}_",
                                         suffix=".db")[1])
    scratch = open_db(scratch_path)
    init_db(scratch)
    cls = load_crawler_class(args.site)
    # documents.site_id has a FOREIGN KEY to sites(site_id) and open_db()
    # turns PRAGMA foreign_keys=ON, so the scratch DB needs its own sites
    # row registered before crawl() can INSERT any documents (2026-07-11
    # discovered: fresh scratch DB -> every insert failed with
    # "FOREIGN KEY constraint failed", 0 scratch_docs, tool silently no-op).
    upsert_site(scratch, args.site, cls.site_name, cls.base_url)
    crawler = cls(db_conn=scratch, delay=1.0)
    print(f"[backfill] crawling {args.site} into scratch {scratch_path} ...")
    crawler.crawl(limit=args.limit)

    main_conn = open_db(Path(args.db))
    rep = backfill(main_conn, scratch, args.site, fields, apply=args.apply)
    out = Path(f"data/audit/backfill_{args.site}_{datetime.now():%Y%m%d_%H%M%S}"
               f"{'_apply' if args.apply else '_dryrun'}.json")
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    print(f"report -> {out}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""libertree 크롤러 독립 실행 러너.

사용법::

    python run.py --list                     # 등록된 모든 site_id 출력
    python run.py <site_id>                   # 해당 사이트 전량 수집
    python run.py <site_id> --limit 20        # 20건만 수집 (테스트)
    python run.py <site_id> --db out.db       # 저장할 SQLite 경로 지정

수집 결과는 SQLite(`libertree.db` 기본)의 ``documents`` 테이블에 적재됩니다.
스키마는 처음 실행 시 자동 생성됩니다. 사이트별 API 키가 필요한 경우
``.env.example`` 을 참고해 ``.env`` 를 만들어 두면 자동 로드됩니다.

수집 상한(옵션, 환경변수)::

    LIBERTREE_MAX_PAGES   페이지 상한   (기본 200)
    LIBERTREE_MAX_WALL_S  사이트당 초   (기본 1500 = 25분)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# .env 자동 로드 (있으면). 없어도 대부분 사이트는 정상 동작.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass

from crawler.sites import CRAWLERS
from crawler import db_libertree as ldb


def _list_sites():
    for sid in sorted(CRAWLERS):
        print(sid)
    print(f"\n총 {len(CRAWLERS)}개 크롤러", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(
        description="libertree 크롤러 독립 실행 러너",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("site_id", nargs="?", help="수집할 사이트 ID (--list 로 목록 확인)")
    ap.add_argument("--limit", type=int, default=None, help="저장할 최대 문서 수")
    ap.add_argument("--db", default="libertree.db", help="SQLite 저장 경로 (기본 libertree.db)")
    ap.add_argument("--delay", type=float, default=1.0, help="요청 간 지연(초), 기본 1.0")
    ap.add_argument("--list", action="store_true", help="등록된 모든 site_id 출력 후 종료")
    args = ap.parse_args()

    if args.list or not args.site_id:
        _list_sites()
        return 0

    if args.site_id not in CRAWLERS:
        print(f"알 수 없는 site_id: {args.site_id!r}", file=sys.stderr)
        print("→ `python run.py --list` 로 목록을 확인하세요.", file=sys.stderr)
        return 2

    cls = CRAWLERS[args.site_id]
    conn = ldb.open_db(args.db)
    ldb.init_db(conn)
    ldb.upsert_site(
        conn,
        getattr(cls, "site_id", args.site_id),
        getattr(cls, "site_name", args.site_id),
        getattr(cls, "base_url", "") or "",
    )

    crawler = cls(db_conn=conn, delay=args.delay)
    print(f"[run] {args.site_id} 수집 시작 "
          f"(limit={args.limit}, db={args.db})", file=sys.stderr)
    saved = crawler.crawl(limit=args.limit)

    print(f"\n[run] 완료: {saved}건 저장 → {Path(args.db).resolve()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""등록된 모든 크롤러의 카탈로그를 TSV 로 출력.

    python list_sites.py               # site_id  site_name  base_url
    python list_sites.py > sites.tsv   # 파일로 저장 (엑셀에서 열람 가능)
"""
from __future__ import annotations

import sys

from crawler.sites import CRAWLERS


def main():
    print("site_id\tsite_name\tbase_url")
    for sid in sorted(CRAWLERS):
        cls = CRAWLERS[sid]
        name = (getattr(cls, "site_name", "") or "").replace("\t", " ")
        url = getattr(cls, "base_url", "") or ""
        print(f"{sid}\t{name}\t{url}")
    print(f"# 총 {len(CRAWLERS)}개", file=sys.stderr)


if __name__ == "__main__":
    main()

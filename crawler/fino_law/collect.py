from __future__ import annotations

import argparse
from pathlib import Path

import httpx

from .db import connect_db, init_schema, upsert_article, upsert_document
from .fetch_exec import download_pdf, fetch_article_list, latest_publication
from .fetch_law import fetch_law_service, search_law
from .parsers_exec import parse_exec
from .parsers_law import parse_law_service, parse_search_for_current
from .pdf_exec import extract_bodies, pdf_to_text
from .sources import LAW_TARGETS, law_search_name
from .sources_exec import EXEC_TARGETS

DEFAULT_DB_PATH = Path("data/fino_law.db")
DEFAULT_PDF_DIR = Path("data/fino_law_exec_pdf")


def collect_law(*, db_path: Path, delay: float) -> tuple[int, int]:
    docs = 0
    arts = 0
    with connect_db(db_path) as conn, httpx.Client(timeout=30) as client:
        init_schema(conn)
        for target in LAW_TARGETS:
            full_name = law_search_name(target)  # e.g., "법인세법 시행령"
            search = search_law(client, full_name, delay)
            hit = parse_search_for_current(search, full_name)
            if hit is None:
                print(f"[skip] 현행 미매칭: {full_name}", flush=True)
                continue
            service = fetch_law_service(client, hit["법령일련번호"], delay)
            doc = parse_law_service(
                service, name=full_name, category=target.category,
                external_id=str(hit["법령ID"]),
            )
            doc_id = upsert_document(
                conn, source_kind=doc.source_kind, external_id=doc.external_id,
                title=doc.title, category=doc.category, org=doc.org,
                promulgated_at=doc.promulgated_at, effective_at=doc.effective_at,
                version_code=doc.version_code, source_url=doc.source_url,
            )
            for a in doc.articles:
                upsert_article(
                    conn, document_id=doc_id, article_no=a.article_no,
                    article_title=a.article_title, body_text=a.body_text,
                    clause_json=a.clause_json, source_url=a.source_url, seq=a.seq,
                )
            docs += 1
            arts += len(doc.articles)
            print(f"[law] {full_name}: 조문 {len(doc.articles)}", flush=True)
    return docs, arts


def collect_exec(*, db_path: Path, pdf_dir: Path, delay: float) -> tuple[int, int]:
    docs = 0
    arts = 0
    with connect_db(db_path) as conn:
        init_schema(conn)
        for t in EXEC_TARGETS:
            pub = latest_publication(t.ntst_bsc_id, t.ntst_plcn_bk_id, delay)
            if pub is None:
                print(f"[skip] 발간본 없음: {t.name}", flush=True)
                continue
            listing = fetch_article_list(t.ntst_bsc_id, pub["rgt_year"], delay)
            pdf_path = pdf_dir / f"{t.name}_{pub['rgt_year']}.pdf"
            try:
                download_pdf(pub["fle_id"], pub["fle_sn"], pdf_path, delay)
                bodies = extract_bodies(pdf_to_text(pdf_path), page_headers=(t.name, "국세청"))
            except Exception as exc:  # PDF 실패해도 구조는 저장
                print(f"[warn] PDF 실패 {t.name}: {exc}", flush=True)
                bodies = {}
            doc = parse_exec(listing, name=t.name, ntst_bsc_id=t.ntst_bsc_id, bodies=bodies)
            doc_id = upsert_document(
                conn, source_kind=doc.source_kind, external_id=doc.external_id,
                title=doc.title, category=doc.category, org=doc.org,
                promulgated_at=doc.promulgated_at, effective_at=doc.effective_at,
                version_code=doc.version_code, source_url=doc.source_url,
            )
            filled = 0
            for a in doc.articles:
                upsert_article(
                    conn, document_id=doc_id, article_no=a.article_no,
                    article_title=a.article_title, body_text=a.body_text,
                    clause_json=a.clause_json, source_url=a.source_url, seq=a.seq,
                )
                if a.body_text:
                    filled += 1
            docs += 1
            arts += len(doc.articles)
            print(f"[exec] {t.name}({pub['rgt_year']}): 조문 {len(doc.articles)} 본문 {filled}", flush=True)
    return docs, arts


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m crawler.fino_law.collect")
    _ = p.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    _ = p.add_argument("--law", action="store_true", help="법령 수집(law.go.kr)")
    _ = p.add_argument("--exec", action="store_true", help="조문형 집행기준 수집(taxlaw)")
    _ = p.add_argument("--all", action="store_true", help="법령+집행기준 모두")
    _ = p.add_argument("--delay-seconds", type=float, default=0.3)
    _ = p.add_argument("--pdf-dir", type=Path, default=DEFAULT_PDF_DIR)
    return p


def main() -> int:
    args = build_parser().parse_args()
    run_law = args.law or args.all
    if run_law:
        d, a = collect_law(db_path=args.db_path, delay=args.delay_seconds)
        print(f"done law: documents={d} articles={a}")
    if args.exec or args.all:
        d, a = collect_exec(db_path=args.db_path, pdf_dir=args.pdf_dir, delay=args.delay_seconds)
        print(f"done exec: documents={d} articles={a}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

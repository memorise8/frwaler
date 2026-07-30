# -*- coding: utf-8 -*-
"""libertree.db 자료 품질 감사 (읽기 전용, 재실행 가능).

세션(2026-07-30)에서 수행한 품질 검증을 하나의 스크립트로 통합한 것.
DB/블롭을 절대 수정하지 않으며, 언제든 다시 돌려 수치를 재현/갱신할 수 있다.

Usage:
    python scripts/audit/data_quality_audit.py [--db PATH] [--blob-root PATH] [--samples]

기본 경로:
    DB   = <repo>/libertree-app/data/libertree.db  (= data/libertree.db 심볼릭)
    BLOB = <repo>/libertree
"""
from __future__ import annotations

import argparse
import random
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent


def h(title):
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(REPO / "libertree-app" / "data" / "libertree.db"))
    ap.add_argument("--blob-root", default=str(REPO / "libertree"))
    ap.add_argument("--samples", action="store_true", help="print example rows for each anomaly class")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    q = con.cursor().execute

    def c(sql):
        return q(sql).fetchone()[0]

    blob = Path(args.blob_root)

    def bpath(seq, ext):
        s = str(seq).zfill(12)
        return blob / s[:4] / s[4:8] / f"{s}.{ext}"

    tot = c("SELECT COUNT(*) FROM documents")

    # ---- 1. 수집 규모 ----------------------------------------------------
    h("1. 수집 규모")
    pdf = c("SELECT COUNT(*) FROM documents WHERE pdf_downloaded=1")
    txt = c("SELECT COUNT(*) FROM documents WHERE text_extracted=1")
    summ = c("SELECT COUNT(*) FROM documents WHERE COALESCE(summary,'')<>''")
    abst = c("SELECT COUNT(*) FROM documents WHERE COALESCE(abstract,'')<>''")
    sites = c("SELECT COUNT(DISTINCT site_id) FROM documents")
    pdfgb = c("SELECT COALESCE(SUM(pdf_size_bytes),0) FROM documents WHERE pdf_downloaded=1") / 1e9
    dmin, dmax = q("SELECT MIN(collected_at), MAX(collected_at) FROM documents").fetchone()
    print(f"  총 문서            : {tot:>10,}")
    print(f"  PDF 다운로드        : {pdf:>10,}  ({pdf/tot*100:.1f}%)")
    print(f"  텍스트 추출         : {txt:>10,}  ({txt/tot*100:.1f}%)")
    print(f"  초록 보유          : {abst:>10,}  ({abst/tot*100:.1f}%)")
    print(f"  요약 완료          : {summ:>10,}  ({summ/tot*100:.1f}%)")
    print(f"  PDF 총 용량         : {pdfgb:>10.1f} GB")
    print(f"  수집 사이트         : {sites:>10,}")
    print(f"  수집 기간          : {dmin}  ~  {dmax}")

    # ---- 2. 확정 garbage: PDF가 실제로는 HTML/에러 --------------------------
    h("2. PDF garbage (블롭 첫 바이트가 %PDF- 아님 = HTML/에러 저장)")
    print(f"  PDF <=100B         : {c('SELECT COUNT(*) FROM documents WHERE pdf_downloaded=1 AND pdf_size_bytes<=100'):>8,}")
    print(f"  PDF <=1KB          : {c('SELECT COUNT(*) FROM documents WHERE pdf_downloaded=1 AND pdf_size_bytes<=1024'):>8,}")
    print(f"  PDF <=10KB         : {c('SELECT COUNT(*) FROM documents WHERE pdf_downloaded=1 AND pdf_size_bytes<=10240'):>8,}")
    # 실제 매직바이트 검사(있는 블롭만)
    rows = q("SELECT seq_id FROM documents WHERE pdf_downloaded=1 AND pdf_size_bytes<=10240").fetchall()
    not_pdf = checked = 0
    for (s,) in rows:
        p = bpath(s, "pdf")
        if p.exists():
            checked += 1
            if p.read_bytes()[:5] != b"%PDF-":
                not_pdf += 1
    print(f"  <=10KB 중 실제로 %PDF- 아님(확정 garbage): {not_pdf:,} / 검사 {checked:,}")

    # ---- 3. 대량 동일 PDF (오배정) ---------------------------------------
    h("3. 동일 PDF를 5+ 문서가 공유 (오배정 의심)")
    md = q("""SELECT COUNT(*), COALESCE(SUM(c),0), COALESCE(SUM(c-1),0) FROM
             (SELECT COUNT(*) c FROM documents WHERE pdf_downloaded=1 AND COALESCE(pdf_sha256,'')<>''
              GROUP BY pdf_sha256 HAVING c>=5)""").fetchone()
    print(f"  그룹 {md[0]:,} / 문서 {md[1]:,} / 잉여 {md[2]:,}")

    # ---- 4. 중복 ---------------------------------------------------------
    h("4. 중복 (엄격=meta_url+title+abstract 전부 동일이 진짜 중복)")
    strict = c("""SELECT COALESCE(SUM(n-1),0) FROM
                 (SELECT COUNT(*) n FROM documents WHERE COALESCE(meta_url,'')<>''
                  GROUP BY meta_url,title,abstract HAVING n>1)""")
    ta = c("SELECT COALESCE(SUM(n-1),0) FROM (SELECT COUNT(*) n FROM documents GROUP BY title,abstract HAVING n>1)")
    print(f"  title+abstract 동일 초과행       : {ta:>8,}")
    print(f"  [엄격] +meta_url 동일(진짜중복)   : {strict:>8,}  → 제거 후 고유 {tot-strict:,}")

    # ---- 5. 콘텐츠 품질 계층 ---------------------------------------------
    h("5. 콘텐츠 품질 계층 (boilerplate = 동일 초록 2+ 공유)")
    q("DROP TABLE IF EXISTS temp.bp")
    q("""CREATE TEMP TABLE bp AS SELECT abstract FROM documents
         WHERE LENGTH(TRIM(abstract))>0 GROUP BY abstract HAVING COUNT(*)>1""")
    q("CREATE INDEX temp.idx_bp ON bp(abstract)")
    isbp = "abstract IN (SELECT abstract FROM bp)"
    tierC = c(f"SELECT COUNT(*) FROM documents WHERE {isbp} AND text_extracted=0")
    tierB = c(f"SELECT COUNT(*) FROM documents WHERE {isbp} AND text_extracted=1")
    tierA = tot - tierB - tierC
    print(f"  A. 고유 초록 (양호)                 : {tierA:>8,} ({tierA/tot*100:.1f}%)")
    print(f"  B. boilerplate 초록 + PDF본문 있음  : {tierB:>8,} ({tierB/tot*100:.1f}%)")
    print(f"  C. boilerplate 초록 + PDF본문 없음  : {tierC:>8,} ({tierC/tot*100:.1f}%)  <- 실질 취약")

    # ---- 6. 기타 이상 ----------------------------------------------------
    h("6. 기타 이상")
    sql_css = ("SELECT COUNT(*) FROM documents WHERE abstract LIKE '%gform_wrapper%' "
               "OR abstract LIKE '%data-form-index%' OR abstract LIKE '%<style%' OR abstract LIKE '%<div%'")
    sql_moji = "SELECT COUNT(*) FROM documents WHERE abstract LIKE '%'||char(65533)||'%'"
    sql_shortsumm = "SELECT COUNT(*) FROM documents WHERE COALESCE(summary,'')<>'' AND LENGTH(TRIM(summary))<30"
    sql_nodate = "SELECT COUNT(*) FROM documents WHERE COALESCE(published_date,'')=''"
    n_css, n_moji, n_ss, n_nd = c(sql_css), c(sql_moji), c(sql_shortsumm), c(sql_nodate)
    print(f"  초록에 CSS/HTML 마크업 의심        : {n_css:>8,}")
    print(f"  초록 mojibake(U+FFFD)             : {n_moji:>8,}")
    print(f"  요약 30자 미만                    : {n_ss:>8,}")
    print(f"  published_date NULL/빈            : {n_nd:>8,}")

    # ---- 7. 블롭 디스크 정합성 (샘플) -------------------------------------
    h("7. 블롭 디스크 정합성 (랜덤 샘플)")
    allpdf = q("SELECT seq_id, text_extracted FROM documents WHERE pdf_downloaded=1").fetchall()
    sample = random.Random(42).sample(allpdf, min(3000, len(allpdf)))
    miss_pdf = sum(1 for s, te in sample if not bpath(s, "pdf").exists())
    te1 = [s for s, te in sample if te == 1]
    miss_txt = sum(1 for s in te1 if not bpath(s, "txt").exists())
    print(f"  pdf_downloaded=1 샘플 {len(sample)} 중 pdf 파일 없음: {miss_pdf} ({miss_pdf/len(sample)*100:.2f}%)")
    print(f"  text_extracted=1 샘플 {len(te1)} 중 txt 파일 없음: {miss_txt} ({(miss_txt/max(1,len(te1))*100):.2f}%)")

    # ---- 8. 문제 집중 사이트 ---------------------------------------------
    h("8. 문제 집중 사이트 TOP15 (tiny PDF + 추출실패)")
    for sid, tiny, noext in q("""
        SELECT site_id,
               SUM(CASE WHEN pdf_downloaded=1 AND pdf_size_bytes<=10240 THEN 1 ELSE 0 END) tiny,
               SUM(CASE WHEN pdf_downloaded=1 AND text_extracted=0 THEN 1 ELSE 0 END) noext
        FROM documents GROUP BY site_id
        ORDER BY (tiny+noext) DESC LIMIT 15"""):
        print(f"  {str(sid)[:42]:42s} tiny={tiny:>6,}  추출실패={noext:>6,}")

    con.close()
    print("\n(감사 완료 — 읽기 전용, DB/블롭 변경 없음)")


if __name__ == "__main__":
    main()

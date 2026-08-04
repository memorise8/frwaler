# -*- coding: utf-8 -*-
"""Build the client-facing "Total Data Report" (held vs. available, per site).

Reads:
  libertree-app/data/libertree.db      (read-only; held/collected counts + PDF bytes)
  scripts/audit/coverage_report.csv    (per-site source_total measured by coverage_audit.py)
  crawler/sites/configs/*.json         (for the 7 config-paginatable HTML sites)

Fixes applied on top of coverage_report.csv:
  1. HAL rescope — sites probed via method=hal_solr that got the GLOBAL HAL total
     (numFound close to the whole-corpus figure) are re-queried against the correct
     per-portal Solr core (or `fq=collCode_s:{CODE}` for portals without a distinct
     lowercase-subdomain core, e.g. hal.science/AFD, hal.science/IGN-ENSG, inria).
  2. 7 config-paginatable "unknown" HTML sites are measured by binary-searching the
     last valid list page (or offset) and counting real items on it.

Writes:
  scripts/audit/total_data_report.xlsx  (요약 / 사이트별 / 국가별_롤업)

Usage:
    source .venv/bin/activate
    export PYTHONPATH=/data_raid/ruci_workspace/frwaler_job
    python3 scripts/audit/total_data_report.py
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
from urllib.parse import urlparse, parse_qs, unquote

import requests
import urllib3
from bs4 import BeautifulSoup
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill
from openpyxl.utils import get_column_letter

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REPO = "/data_raid/ruci_workspace/frwaler_job"
DB = f"{REPO}/libertree-app/data/libertree.db"
COVERAGE_CSV = f"{REPO}/scripts/audit/coverage_report.csv"
CONFIGS_DIR = f"{REPO}/crawler/sites/configs"
OUT_XLSX = f"{REPO}/scripts/audit/total_data_report.xlsx"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Reference values supplied by the operator (actual disk usage; not derivable from DB alone)
DISK_BLOB_TB = 1.7
DISK_DATA_GB = 32
DISK_DB_GB = 5.6

_session = requests.Session()
_session.headers.update({"User-Agent": UA})
_session.verify = False


# ---------------------------------------------------------------------------
# 1. HAL rescope
# ---------------------------------------------------------------------------

def hal_query(portal_or_path: str, use_fq_collcode: bool = False, timeout: int = 20):
    """Query HAL's Solr API for a numFound total.

    use_fq_collcode=False -> GET /search/{portal_or_path}/  (dedicated per-portal core)
    use_fq_collcode=True  -> GET /search/ with fq=collCode_s:{portal_or_path}
    """
    try:
        if use_fq_collcode:
            r = _session.get("https://api.archives-ouvertes.fr/search/",
                              params={"q": "*:*", "wt": "json", "rows": 0,
                                      "fq": f"collCode_s:{portal_or_path}"},
                              timeout=timeout)
        else:
            r = _session.get(f"https://api.archives-ouvertes.fr/search/{portal_or_path}/",
                              params={"q": "*:*", "wt": "json", "rows": 0}, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json().get("response", {}).get("numFound")
    except Exception:
        return None


def hal_global_total():
    return hal_query("", use_fq_collcode=False) or hal_query("", use_fq_collcode=True)


def _hal_subdomain_portal(host: str):
    parts = host.split(".")
    for marker in ("hal", "archives-ouvertes"):
        if marker in parts:
            idx = parts.index(marker)
            if idx > 0:
                return parts[0]
    return None


def rescope_hal(site_id: str, site_url: str, old_total, global_total: int):
    """Return (new_total, method, note) for one hal_solr site."""
    p = urlparse(site_url)
    host = p.netloc.lower()

    def suspect(total):
        return total is None or (global_total and total >= 0.9 * global_total)

    # Case A: real portal subdomain (e.g. amu.hal.science, inria.hal.science)
    portal = _hal_subdomain_portal(host)
    if portal:
        total = hal_query(portal)
        if not suspect(total):
            return total, "hal_solr", f"portal={portal} (dedicated core, unchanged/confirmed)"
        # dedicated core is unscoped (HAL quirk, e.g. inria) -> fall back to the
        # collCode_s the site's own search URL already filters by
        qs = parse_qs(p.query)
        q_val = unquote(qs.get("q", [""])[0])
        code = None
        if "collCode_s:" in q_val:
            code = q_val.split("collCode_s:", 1)[1].split()[0].strip()
        if not code:
            code = portal.upper()
        total2 = hal_query(code, use_fq_collcode=True)
        if not suspect(total2):
            return total2, "hal_solr_collcode", f"portal core unscoped; used collCode_s={code}"
        # last resort: try the uppercase portal as a dedicated core name too
        total3 = hal_query(portal.upper())
        if not suspect(total3):
            return total3, "hal_solr", f"portal={portal.upper()} (uppercase core)"
        return None, "hal_unresolved", f"portal={portal} candidates all >=90% of global ({global_total:,})"

    # Case B: generic hal.science / archives-ouvertes.fr host -> derive code from URL path
    if host in ("hal.science", "archives-ouvertes.fr"):
        parts = [x for x in p.path.split("/") if x]
        path_code = parts[0] if parts else None
        if path_code:
            total = hal_query(path_code)
            if not suspect(total):
                return total, "hal_solr", f"portal={path_code} (derived from URL path)"
            total2 = hal_query(path_code, use_fq_collcode=True)
            if not suspect(total2):
                return total2, "hal_solr_collcode", f"collCode_s={path_code} (derived from URL path)"
        return None, "hal_unresolved", "generic hal.science host, no confident path-derived portal"

    return None, "hal_unresolved", "could not derive portal from site_url"


# ---------------------------------------------------------------------------
# 2. The 7 config-paginatable HTML sites
# ---------------------------------------------------------------------------

def _count(html, sel):
    if not html:
        return 0
    return len(BeautifulSoup(html, "html.parser").select(sel))


def _fetch(url, params, retries=3):
    last_exc = None
    for attempt in range(retries):
        try:
            r = _session.get(url, params=params, timeout=30)
            return r.text
        except requests.RequestException as e:
            last_exc = e
    raise last_exc


def _binary_search_pagenum(fetch_fn, sel, start, step, base_params, param_name, filt=None):
    def cnt(pg):
        html = fetch_fn({**base_params, param_name: pg})
        if filt is None:
            return _count(html, sel)
        soup = BeautifulSoup(html, "html.parser") if html else None
        if soup is None:
            return 0
        return len([i for i in soup.select(sel) if i.select_one(filt)])

    n0 = cnt(start)
    if n0 == 0:
        return 0, {}
    per_page = n0
    lo, lo_n = start, n0
    mult = 1
    while True:
        cand = start + step * mult
        n = cnt(cand)
        if n == 0:
            hi = cand
            break
        lo, lo_n = cand, n
        mult *= 2
        if mult > 20000:
            return None, {"error": "runaway"}
    while (hi - lo) // step > 1:
        mid = lo + (((hi - lo) // step) // 2) * step
        n = cnt(mid)
        if n == 0:
            hi = mid
        else:
            lo, lo_n = mid, n
    total = (lo - start) // step * per_page + lo_n
    return total, {"per_page": per_page, "last_page": lo, "items_on_last": lo_n}


def _binary_search_offset(fetch_fn, sel, param_name):
    n0 = _count(fetch_fn({param_name: 0}), sel)
    if n0 == 0:
        return 0, {}
    per_page = n0
    lo, lo_n = 0, n0
    mult = 1
    while True:
        cand = per_page * mult
        n = _count(fetch_fn({param_name: cand}), sel)
        if n == 0:
            hi = cand
            break
        lo, lo_n = cand, n
        mult *= 2
        if mult > 40000:
            return None, {"error": "runaway"}
    while (hi - lo) // per_page > 1:
        mid = lo + (((hi - lo) // per_page) // 2) * per_page
        n = _count(fetch_fn({param_name: mid}), sel)
        if n == 0:
            hi = mid
        else:
            lo, lo_n = mid, n
    total = lo + lo_n
    return total, {"per_page": per_page, "last_offset": lo, "items_on_last": lo_n}


def measure_html_paginate_sites():
    """Returns {site_id: (total, method, note)} for the 7 sites, medium confidence."""
    out = {}

    fn = lambda p: _fetch("https://pubs.usgs.gov/browse/Conference%20Paper/", p)
    total, info = _binary_search_pagenum(
        fn, "div.browse-page ul > li", start=1, step=1, base_params={},
        param_name="page", filt="a.usa-link[href^='/publication/']")
    out["pubs-usgs-gov"] = (total, "html_paginate", f"medium confidence; {info}")

    fn = lambda p: _fetch("https://www.krivet.re.kr/kor/sub.do", p)
    total, info = _binary_search_pagenum(
        fn, "table.tb_base.tb_list tbody tr", start=1, step=1,
        base_params={"menuSn": "23"}, param_name="pageIndex")
    out["krivet-re-kr"] = (total, "html_paginate", f"medium confidence; {info}")

    fn = lambda p: _fetch("https://www.nypi.re.kr/board", p)
    total, info = _binary_search_pagenum(
        fn, "div.board_list table tbody tr", start=1, step=1,
        base_params={"menuKey": "uCjzEQTnJu", "bbsId": "BOARD00019", "rowCnt": 10},
        param_name="pageNum", filt="td.txt_left a[href]")
    out["nypi-re-kr"] = (total, "html_paginate",
                          f"medium confidence; real pagination param is pageNum not page; {info}")

    fn = lambda p: _fetch("https://ir.arcnl.nl/", p)
    total, info = _binary_search_offset(fn, "div.listing ul.unstyled > li.row-fluid.underlined", "next")
    out["ir-arcnl-nl"] = (total, "html_paginate", f"medium confidence; {info}")

    fn = lambda p: _fetch("https://ir.cwi.nl/", p)
    total, info = _binary_search_offset(fn, "div.listing ul.unstyled > li.row-fluid.underlined", "next")
    out["ir-cwi-nl"] = (total, "html_paginate", f"medium confidence; {info}")

    fn = lambda p: _fetch("https://repository.naturalis.nl/", p)
    total, info = _binary_search_offset(fn, "div.listing ul.unstyled > li.row-fluid.underlined", "next")
    out["repository-naturalis-nl"] = (total, "html_paginate", f"medium confidence; {info}")

    fn = lambda p: _fetch("https://eprr.lanl.gov/", p)
    n0 = None
    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA)

        def pw_count(pg):
            url = f"https://eprr.lanl.gov/?f%5BdocType_f%5D%5B%5D=Full+Paper&page={pg}"
            page.goto(url, timeout=30000, wait_until="networkidle")
            page.wait_for_timeout(1200)
            soup = BeautifulSoup(page.content(), "html.parser")
            return len(soup.select("div.documents-list#documents article.document"))

        n0 = pw_count(1)
        if n0:
            per_page = n0
            lo, lo_n = 1, n0
            mult = 1
            while True:
                cand = 1 + mult
                n = pw_count(cand)
                if n == 0:
                    hi = cand
                    break
                lo, lo_n = cand, n
                mult *= 2
                if mult > 2000:
                    lo_n = None
                    break
            if lo_n is not None:
                while hi - lo > 1:
                    mid = (lo + hi) // 2
                    n = pw_count(mid)
                    if n == 0:
                        hi = mid
                    else:
                        lo, lo_n = mid, n
                total = (lo - 1) * per_page + lo_n
                out["eprr-lanl-gov"] = (total, "html_paginate",
                                         f"medium confidence; JS-rendered (playwright); "
                                         f"docType=Full Paper filter; per_page={per_page}, last_page={lo}")
        browser.close()
        pw.stop()
    except Exception as e:
        out["eprr-lanl-gov"] = (None, "unknown", f"playwright measurement failed: {e}")

    if "eprr-lanl-gov" not in out:
        out["eprr-lanl-gov"] = (None, "unknown", "measurement failed / no items")

    return out


# ---------------------------------------------------------------------------
# 3. Assemble
# ---------------------------------------------------------------------------

def load_coverage_rows():
    with open(COVERAGE_CSV, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_db_counts():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = conn.cursor()
    collected = dict(cur.execute("SELECT site_id, COUNT(*) FROM documents GROUP BY site_id").fetchall())
    held_bytes = dict(cur.execute(
        "SELECT site_id, COALESCE(SUM(pdf_size_bytes),0) FROM documents GROUP BY site_id").fetchall())
    site_names = dict(cur.execute("SELECT site_id, site_name FROM sites").fetchall())

    total_docs, total_pdf_downloaded, total_pdf_bytes = cur.execute(
        "SELECT COUNT(*), SUM(pdf_downloaded), COALESCE(SUM(pdf_size_bytes),0) FROM documents").fetchone()
    conn.close()
    return collected, held_bytes, site_names, total_docs, total_pdf_downloaded, total_pdf_bytes


def main():
    rows = load_coverage_rows()
    collected_db, held_bytes_db, site_names, total_docs, total_pdf_downloaded, total_pdf_bytes = load_db_counts()

    print(f"[1/4] Loaded {len(rows)} sites from {os.path.basename(COVERAGE_CSV)}")
    print(f"      DB grand totals: docs={total_docs:,} pdf_downloaded={total_pdf_downloaded:,} "
          f"pdf_bytes={total_pdf_bytes:,}")

    # --- HAL rescope ---
    print("[2/4] Rescoping HAL sites (method=hal_solr) ...")
    global_total = hal_global_total() or 4_628_498
    print(f"      HAL global corpus total (sanity threshold basis): {global_total:,}")

    hal_before_after = []  # for verification printout
    for r in rows:
        if r["method"] != "hal_solr":
            continue
        old_total = int(r["source_total"]) if r["source_total"] else None
        new_total, method, note = rescope_hal(r["site_id"], r["site_url"], old_total, global_total)
        changed = (new_total != old_total) or (method != "hal_solr")
        if changed:
            hal_before_after.append((r["site_id"], old_total, new_total, method, note))
        if new_total is not None:
            r["source_total"] = str(new_total)
            r["method"] = method
            r["status"] = "측정완료"
        else:
            r["source_total"] = ""
            r["method"] = method
            r["status"] = "미측정"
        r["_note"] = note

    print(f"      {len(hal_before_after)} HAL sites changed:")
    for sid, old, new, method, note in hal_before_after:
        old_s = f"{old:,}" if old is not None else "n/a"
        new_s = f"{new:,}" if new is not None else "n/a"
        print(f"        {sid}: OLD={old_s}  ->  NEW={new_s}  [{method}] {note}")

    # --- 7 config-paginatable HTML sites ---
    print("[3/4] Measuring the 7 config-paginatable HTML sites ...")
    html_results = measure_html_paginate_sites()
    by_id = {r["site_id"]: r for r in rows}
    for sid, (total, method, note) in html_results.items():
        r = by_id.get(sid)
        if r is None:
            continue
        print(f"        {sid}: total={total} [{method}] {note}")
        if total is not None:
            r["source_total"] = str(total)
            r["method"] = method
            r["status"] = "측정완료"
        else:
            r["source_total"] = ""
            r["method"] = method
            r["status"] = "미측정"
        r["_note"] = note

    # --- finalize per-site rows: pull fresh collected/held_bytes from DB, normalize status ---
    site_rows = []
    for r in rows:
        sid = r["site_id"]
        collected = collected_db.get(sid, 0)
        held_bytes = held_bytes_db.get(sid, 0)
        source_total = int(r["source_total"]) if r["source_total"] else None
        if source_total is not None:
            status = "측정완료"
            coverage_pct = round(100.0 * collected / source_total, 1) if source_total > 0 else (
                100.0 if collected == 0 else None)
        else:
            status = "미측정"
            coverage_pct = None
        site_rows.append({
            "site_id": sid,
            "sheet": r["sheet"],
            "site_name": site_names.get(sid, sid),
            "collected": collected,
            "held_pdf_bytes": held_bytes,
            "source_total": source_total,
            "coverage_pct": coverage_pct,
            "method": r["method"],
            "status": status,
        })

    assert len(site_rows) == len(rows), "site row count mismatch"
    print(f"[4/4] Assembled {len(site_rows)} site rows. Building workbook ...")

    build_workbook(site_rows, total_docs, total_pdf_downloaded, total_pdf_bytes)

    # --- verification printout ---
    measured = [r for r in site_rows if r["source_total"] is not None]
    unmeasured = [r for r in site_rows if r["source_total"] is None]
    coll_sum = sum(r["collected"] for r in measured)
    src_sum = sum(r["source_total"] for r in measured)
    overall_pct = round(100.0 * coll_sum / src_sum, 1) if src_sum else None
    print()
    print("=== VERIFY: 요약 grand totals ===")
    print(f"보유 총 문서 수: {total_docs:,}")
    print(f"PDF 다운로드 수: {total_pdf_downloaded:,}")
    print(f"held_pdf_bytes 합계: {total_pdf_bytes:,} bytes "
          f"({total_pdf_bytes/1e9:.1f} GB / {total_pdf_bytes/1e12:.2f} TB)")
    print(f"측정된 사이트 수: {len(measured)}/{len(site_rows)}")
    print(f"측정 사이트 collected 합: {coll_sum:,}  vs  source_total 합: {src_sum:,}  "
          f"coverage={overall_pct}%  shortfall={src_sum - coll_sum:,}")
    print(f"미측정 사이트 수: {len(unmeasured)}  (그 보유 건수 floor = "
          f"{sum(r['collected'] for r in unmeasured):,})")

    print(f"\nWrote {OUT_XLSX}")
    return site_rows


# ---------------------------------------------------------------------------
# Workbook
# ---------------------------------------------------------------------------

HEADER_FILL = PatternFill(start_color="DDEBF7", end_color="DDEBF7", fill_type="solid")
HEADER_FONT = Font(bold=True)


def _style_header(ws, row=1):
    for cell in ws[row]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _autosize(ws, widths):
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def build_workbook(site_rows, total_docs, total_pdf_downloaded, total_pdf_bytes):
    wb = Workbook()

    # ---------------- 요약 ----------------
    ws = wb.active
    ws.title = "요약"

    measured = [r for r in site_rows if r["source_total"] is not None]
    unmeasured = [r for r in site_rows if r["source_total"] is None]
    coll_sum = sum(r["collected"] for r in measured)
    src_sum = sum(r["source_total"] for r in measured)
    overall_pct = round(100.0 * coll_sum / src_sum, 1) if src_sum else 0.0
    shortfall = src_sum - coll_sum
    unmeasured_floor = sum(r["collected"] for r in unmeasured)

    ws.append(["Total Data Report — 요약 (보유 vs 가용)"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])

    ws.append(["■ 보유 (수집 확정분)"])
    ws["A3"].font = Font(bold=True, size=12)
    rows_summary_1 = [
        ("총 문서 수 (전체 사이트, DB 기준)", f"{total_docs:,} 건"),
        ("PDF 다운로드 완료 수", f"{total_pdf_downloaded:,} 건"),
        ("보유 PDF 용량 합계 (DB pdf_size_bytes)",
         f"{total_pdf_bytes/1e9:,.1f} GB ({total_pdf_bytes/1e12:.2f} TB)"),
        ("실제 디스크 사용량 (참고값)",
         f"blob libertree/={DISK_BLOB_TB} TB + data/={DISK_DATA_GB} GB + DB={DISK_DB_GB} GB "
         f"≈ 총 1.8 TB"),
    ]
    for label, val in rows_summary_1:
        ws.append([label, val])

    ws.append([])
    r0 = ws.max_row + 1
    ws.append(["■ 가용 (측정 가능분 기준, 측정된 사이트만)"])
    ws.cell(row=r0, column=1).font = Font(bold=True, size=12)
    rows_summary_2 = [
        ("측정된 사이트 수", f"{len(measured):,} / {len(site_rows):,}"),
        ("측정 사이트 보유(collected) 합", f"{coll_sum:,} 건"),
        ("측정 사이트 가용(source_total) 합", f"{src_sum:,} 건"),
        ("커버리지 %", f"{overall_pct}%"),
        ("부족분 (source_total - collected)", f"{shortfall:,} 건"),
    ]
    for label, val in rows_summary_2:
        ws.append([label, val])

    ws.append([])
    r1 = ws.max_row + 1
    ws.append(["■ 미측정"])
    ws.cell(row=r1, column=1).font = Font(bold=True, size=12)
    ws.append(["미측정 사이트 수", f"{len(unmeasured):,} 개"])
    ws.append(["미측정 사이트 보유 건수 (floor, 최소값)", f"{unmeasured_floor:,} 건"])

    ws.append([])
    r2 = ws.max_row + 1
    ws.append(["■ 결론"])
    ws.cell(row=r2, column=1).font = Font(bold=True, size=12)
    ws.append([f"측정 가능분 기준 커버리지 {overall_pct}%, 최소 부족분 {shortfall:,}건; "
               f"미측정 {len(unmeasured)}개 사이트 별도 (보유 {unmeasured_floor:,}건은 floor)."])
    ws.cell(row=ws.max_row, column=1).font = Font(italic=True)

    _autosize(ws, [45, 60])
    for row in ws.iter_rows():
        for cell in row:
            if cell.column == 2:
                cell.alignment = Alignment(horizontal="left", wrap_text=True)

    # ---------------- 사이트별 ----------------
    ws2 = wb.create_sheet("사이트별")
    headers = ["site_id", "sheet (국가/그룹)", "site_name (기관명)", "collected (보유 건수)",
               "held_pdf_bytes (보유 PDF 바이트)", "held_size_MB (보유 용량 MB)",
               "source_total (가용 건수)", "coverage_pct (커버리지 %)",
               "method (측정 방법)", "status (상태)"]
    ws2.append(headers)
    _style_header(ws2)

    def sort_key(r):
        # source_total desc; unmeasured (None) sink to bottom
        return (r["source_total"] is None, -(r["source_total"] or 0))

    for r in sorted(site_rows, key=sort_key):
        ws2.append([
            r["site_id"], r["sheet"], r["site_name"],
            r["collected"], r["held_pdf_bytes"],
            round(r["held_pdf_bytes"] / 1e6, 1),
            r["source_total"] if r["source_total"] is not None else None,
            r["coverage_pct"] if r["coverage_pct"] is not None else None,
            r["method"], r["status"],
        ])

    for row in ws2.iter_rows(min_row=2):
        row[3].number_format = "#,##0"
        row[4].number_format = "#,##0"
        row[5].number_format = "#,##0.0"
        row[6].number_format = "#,##0"
        row[7].number_format = "0.0"

    _autosize(ws2, [30, 22, 34, 14, 20, 16, 16, 14, 20, 12])
    ws2.freeze_panes = "A2"

    # ---------------- 국가별_롤업 ----------------
    ws3 = wb.create_sheet("국가별_롤업")
    headers3 = ["sheet (국가/그룹)", "사이트수", "측정된 사이트수", "collected 합 (보유)",
                "held_size_MB 합 (보유 용량)", "source_total 합 (가용, 측정분)", "coverage % (측정분 기준)"]
    ws3.append(headers3)
    _style_header(ws3)

    agg = {}
    for r in site_rows:
        s = agg.setdefault(r["sheet"], {"total": 0, "measured": 0, "collected": 0,
                                         "held_mb": 0.0, "source_total": 0})
        s["total"] += 1
        s["collected"] += r["collected"]
        s["held_mb"] += r["held_pdf_bytes"] / 1e6
        if r["source_total"] is not None:
            s["measured"] += 1
            s["source_total"] += r["source_total"]

    for sheet, s in sorted(agg.items(), key=lambda kv: -kv[1]["collected"]):
        pct = round(100.0 * s["collected"] / s["source_total"], 1) if s["source_total"] else None
        ws3.append([sheet, s["total"], s["measured"], s["collected"],
                    round(s["held_mb"], 1), s["source_total"] or None, pct])

    for row in ws3.iter_rows(min_row=2):
        row[1].number_format = "#,##0"
        row[2].number_format = "#,##0"
        row[3].number_format = "#,##0"
        row[4].number_format = "#,##0.0"
        row[5].number_format = "#,##0"
        row[6].number_format = "0.0"

    _autosize(ws3, [32, 12, 16, 18, 20, 20, 18])
    ws3.freeze_panes = "A2"

    wb.save(OUT_XLSX)


if __name__ == "__main__":
    main()

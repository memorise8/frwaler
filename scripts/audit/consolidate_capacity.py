# -*- coding: utf-8 -*-
"""Consolidate all capacity sources into one final per-crawler CSV (read-only).

Merges, per site_id, the best available TRUE total (`max_to_collect`) from
several measurement sources by priority, compares against what we have already
`collected`, and projects PDF count + storage.

max_to_collect priority (first hit wins):
  1. big_totals.csv        total (note=ok)     -> source=big_api        (exact)
       (fresh platform-API remeasure of big/repository sites; overrides the
        suspect html_count_regex values for e-stat/gov-scot/gov-uk etc.)
  2. hal_totals.csv        filtered_total      -> source=hal_api        (exact)
  3. coverage_report.csv   source_total        -> source=api_<method>   (exact)
       (hal_solr rows excluded -- HAL now comes from #2)
  4. custom_crawler_totals.SNAPSHOT.csv source_total>0 -> custom_api    (exact)
  5. full_survey_totals.csv
       ok    & counted<500  -> count_crawl    (exact)       finished below cap
       ok    & counted>=500 -> count_crawl    (lower_bound) hit ~500 cap
       site_timeout         -> count_timeout  (lower_bound) counted so far
       zero_parsed/fetch_fail/error/no_crawler -> unmeasured (health=outcome)
  5. else                                       -> unmeasured

Usage:
    source .venv/bin/activate
    export PYTHONPATH=/data_raid/ruci_workspace/frwaler_job
    python3 scripts/audit/consolidate_capacity.py

Outputs:
    scripts/audit/capacity_final.csv
    scripts/audit/capacity_final.md
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

REPO = Path("/data_raid/ruci_workspace/frwaler_job")
DB = REPO / "libertree-app/data/libertree.db"
AUDIT = REPO / "scripts/audit"

BIG_CSV = AUDIT / "big_totals.csv"
HAL_CSV = AUDIT / "hal_totals.csv"
COVERAGE_CSV = AUDIT / "coverage_report.csv"
CUSTOM_CSV = AUDIT / "custom_crawler_totals.SNAPSHOT.csv"
SURVEY_CSV = AUDIT / "full_survey_totals.csv"

OUT_CSV = AUDIT / "capacity_final.csv"
OUT_MD = AUDIT / "capacity_final.md"

ESTIMATE = 30_000_000
UNMEASURED_OUTCOMES = {"zero_parsed", "fetch_fail", "error", "no_crawler"}

CSV_FIELDS = [
    "site_id", "sheet", "collected", "max_to_collect", "source",
    "exact_or_lowerbound", "gap", "health",
    "observed_pdf_rate", "projected_pdf_count", "projected_storage_gb",
]


def _to_int(v):
    if v is None:
        return None
    v = str(v).strip()
    if not v:
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def load_big():
    """site_id -> total (note=ok rows only) from the fresh platform-API remeasure."""
    out = {}
    if not BIG_CSV.exists():
        return out
    for r in csv.DictReader(BIG_CSV.open(encoding="utf-8")):
        if r.get("note") != "ok":
            continue
        n = _to_int(r.get("total"))
        if n is not None and n > 0:
            out[r["site_id"]] = n
    return out


def load_hal():
    """site_id -> filtered_total (ok rows only)."""
    out = {}
    if not HAL_CSV.exists():
        return out
    for r in csv.DictReader(HAL_CSV.open(encoding="utf-8")):
        if r.get("note") != "ok":
            continue
        n = _to_int(r.get("filtered_total"))
        if n is not None:
            out[r["site_id"]] = n
    return out


def load_coverage():
    """site_id -> (source_total, method) for non-hal_solr rows with a number."""
    out = {}
    if not COVERAGE_CSV.exists():
        return out
    for r in csv.DictReader(COVERAGE_CSV.open(encoding="utf-8")):
        if r.get("method") == "hal_solr":  # HAL comes from hal_totals now
            continue
        n = _to_int(r.get("source_total"))
        if n is None:
            continue
        out[r["site_id"]] = (n, r.get("method") or "unknown")
    return out


def load_custom():
    """site_id -> source_total (>0 only)."""
    out = {}
    if not CUSTOM_CSV.exists():
        return out
    for r in csv.DictReader(CUSTOM_CSV.open(encoding="utf-8")):
        n = _to_int(r.get("source_total"))
        if n is not None and n > 0:
            out[r["site_id"]] = n
    return out


def load_survey():
    """site_id -> {counted, outcome}."""
    out = {}
    if not SURVEY_CSV.exists():
        return out
    for r in csv.DictReader(SURVEY_CSV.open(encoding="utf-8")):
        out[r["site_id"]] = {
            "counted": _to_int(r.get("counted")) or 0,
            "outcome": (r.get("outcome") or "").strip(),
        }
    return out


def load_db():
    """Return (sites list, per-site db stats, global_pdf_rate, global_avg_pdf_mb)."""
    con = sqlite3.connect(str(DB))
    sites = con.execute(
        "SELECT site_id, sheet FROM sites ORDER BY site_id"
    ).fetchall()
    stats = {}
    for site_id, collected, pdf_count, avg_pdf_bytes in con.execute(
        "SELECT site_id, COUNT(*), "
        "SUM(CASE WHEN pdf_size_bytes>0 THEN 1 ELSE 0 END), "
        "AVG(CASE WHEN pdf_size_bytes>0 THEN pdf_size_bytes END) "
        "FROM documents GROUP BY site_id"
    ).fetchall():
        stats[site_id] = {
            "collected": collected or 0,
            "pdf_count": pdf_count or 0,
            "avg_pdf_mb": (avg_pdf_bytes / 1048576) if avg_pdf_bytes else None,
        }
    g_collected = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] or 0
    g_pdf = con.execute(
        "SELECT COUNT(*) FROM documents WHERE pdf_size_bytes>0"
    ).fetchone()[0] or 0
    g_avg_bytes = con.execute(
        "SELECT AVG(pdf_size_bytes) FROM documents WHERE pdf_size_bytes>0"
    ).fetchone()[0] or 0
    con.close()
    global_pdf_rate = (g_pdf / g_collected) if g_collected else 0.0
    global_avg_pdf_mb = (g_avg_bytes / 1048576) if g_avg_bytes else 0.0
    return sites, stats, global_pdf_rate, global_avg_pdf_mb


def resolve_max(site_id, big, hal, coverage, custom, survey):
    """Return (max_to_collect, source, exact_flag, health)."""
    if site_id in big:
        return big[site_id], "big_api", "exact", ""
    if site_id in hal:
        return hal[site_id], "hal_api", "exact", ""
    if site_id in coverage:
        total, method = coverage[site_id]
        return total, f"api_{method}", "exact", ""
    if site_id in custom:
        return custom[site_id], "custom_api", "exact", ""
    if site_id in survey:
        s = survey[site_id]
        outcome, counted = s["outcome"], s["counted"]
        if outcome == "ok":
            if counted < 500:
                return counted, "count_crawl", "exact", ""
            return counted, "count_crawl", "lower_bound", ""
        if outcome == "site_timeout":
            return counted, "count_timeout", "lower_bound", ""
        if outcome in UNMEASURED_OUTCOMES:
            return None, "", "unmeasured", outcome
    return None, "", "unmeasured", ""


def main():
    big = load_big()
    hal = load_hal()
    coverage = load_coverage()
    custom = load_custom()
    survey = load_survey()
    sites, stats, g_rate, g_avg_mb = load_db()

    rows = []
    for site_id, sheet in sites:
        st = stats.get(site_id, {"collected": 0, "pdf_count": 0, "avg_pdf_mb": None})
        collected = st["collected"]
        max_tc, source, flag, health = resolve_max(
            site_id, big, hal, coverage, custom, survey
        )

        gap = "" if max_tc is None else max_tc - collected

        # observed pdf rate for this site
        observed_rate = (st["pdf_count"] / collected) if collected else None

        # projection inputs (fall back to global when sample is thin/missing)
        rate_used = observed_rate if collected >= 20 and observed_rate is not None else g_rate
        avg_mb_used = st["avg_pdf_mb"] if st["avg_pdf_mb"] is not None else g_avg_mb

        if max_tc is None:
            proj_pdf = ""
            proj_gb = ""
        else:
            proj_pdf = round(max_tc * rate_used)
            proj_gb = round(proj_pdf * avg_mb_used / 1024, 2)

        rows.append({
            "site_id": site_id,
            "sheet": sheet or "",
            "collected": collected,
            "max_to_collect": "" if max_tc is None else max_tc,
            "source": source,
            "exact_or_lowerbound": flag,
            "gap": gap,
            "health": health,
            "observed_pdf_rate": "" if observed_rate is None else round(observed_rate, 4),
            "projected_pdf_count": proj_pdf,
            "projected_storage_gb": proj_gb,
        })

    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)

    write_md(rows, g_rate, g_avg_mb)
    print(f"Wrote {OUT_CSV}")
    print(f"Wrote {OUT_MD}\n")
    print(OUT_MD.read_text(encoding="utf-8"))


def write_md(rows, g_rate, g_avg_mb):
    total_sites = len(rows)
    total_collected = sum(r["collected"] for r in rows)

    exact = [r for r in rows if r["exact_or_lowerbound"] == "exact"]
    lower = [r for r in rows if r["exact_or_lowerbound"] == "lower_bound"]
    unmeasured = [r for r in rows if r["exact_or_lowerbound"] == "unmeasured"]

    sum_exact = sum(r["max_to_collect"] for r in exact)
    sum_lower = sum(r["max_to_collect"] for r in lower)
    confirmed = sum_exact
    minimum = sum_exact + sum_lower

    def pct(x):
        return f"{x / ESTIMATE * 100:.1f}%"

    # storage projections
    def sum_proj(subset):
        pdf = sum(r["projected_pdf_count"] for r in subset if r["projected_pdf_count"] != "")
        gb = sum(r["projected_storage_gb"] for r in subset if r["projected_storage_gb"] != "")
        return pdf, gb

    ex_pdf, ex_gb = sum_proj(exact)
    minset = exact + lower
    min_pdf, min_gb = sum_proj(minset)

    top_cap = sorted(
        [r for r in rows if r["max_to_collect"] != ""],
        key=lambda r: r["max_to_collect"], reverse=True
    )[:25]

    unmeasured_by_collected = sorted(
        unmeasured, key=lambda r: r["collected"], reverse=True
    )
    top_storage = sorted(
        [r for r in rows if r["projected_storage_gb"] != ""],
        key=lambda r: r["projected_storage_gb"], reverse=True
    )[:15]

    L = []
    L.append("# 크롤러 케파 최종 집계 (capacity_final)\n")
    L.append(f"- 총 사이트: **{total_sites}**")
    L.append(f"- 총 수집완료(collected): **{total_collected:,}**")
    L.append(f"- exact 사이트 max_to_collect 합계: **{sum_exact:,}** ({len(exact)}개)")
    L.append(f"- lower_bound 사이트 max_to_collect 합계: **{sum_lower:,}** ({len(lower)}개)")
    L.append(f"- unmeasured 사이트: **{len(unmeasured)}개**\n")

    L.append("## 헤드라인 케파\n")
    L.append(f"- **CONFIRMED capacity (exact only): {confirmed:,}**")
    L.append(f"- **MINIMUM capacity (exact + lower_bound): {minimum:,}**\n")

    L.append("## 30,000,000 추정치 대비\n")
    L.append(f"- CONFIRMED {confirmed:,} = 30M의 **{pct(confirmed)}**")
    L.append(f"- MINIMUM {minimum:,} = 30M의 **{pct(minimum)}**")
    L.append("- (전체 서베이가 아직 진행 중이므로 수치는 부분값이며, "
             "unmeasured 사이트에 숨은 케파가 남아 있음)\n")

    L.append("## 스토리지 추정\n")
    L.append(f"- 사용된 GLOBAL fallback pdf_rate: **{g_rate:.4f}** "
             f"(수집 문서 중 PDF 보유 비율)")
    L.append(f"- 사용된 GLOBAL fallback 평균 PDF 크기: **{g_avg_mb:.2f} MB**")
    L.append("- 시나리오별 예상 PDF 개수 / 예상 스토리지:")
    L.append(f"  - **CONFIRMED (exact)**: {ex_pdf:,} PDF, "
             f"**{ex_gb / 1024:.2f} TB** ({ex_gb:,.0f} GB)")
    L.append(f"  - **MINIMUM (exact+lower_bound)**: {min_pdf:,} PDF, "
             f"**{min_gb / 1024:.2f} TB** ({min_gb:,.0f} GB)")
    L.append("- 참고: 텍스트 전용(비-PDF) 문서는 스토리지 기여가 ~0 이므로, "
             "스토리지는 전체 문서 수가 아니라 PDF 개수에 비례함.\n")

    L.append("### 예상 스토리지 상위 15개 사이트\n")
    L.append("| site_id | max_to_collect | proj_pdf | proj_GB |")
    L.append("|---|--:|--:|--:|")
    for r in top_storage:
        L.append(f"| {r['site_id']} | {r['max_to_collect']:,} | "
                 f"{r['projected_pdf_count']:,} | {r['projected_storage_gb']:,.1f} |")
    L.append("")

    L.append("## max_to_collect 상위 25개 사이트\n")
    L.append("| site_id | max_to_collect | collected | source | 구분 |")
    L.append("|---|--:|--:|---|---|")
    for r in top_cap:
        L.append(f"| {r['site_id']} | {r['max_to_collect']:,} | "
                 f"{r['collected']:,} | {r['source']} | {r['exact_or_lowerbound']} |")
    L.append("")

    L.append(f"## unmeasured 사이트 ({len(unmeasured)}개) — 숨은 케파 후보\n")
    L.append("수집완료(collected) 내림차순 상위 20개:\n")
    L.append("| site_id | collected | health |")
    L.append("|---|--:|---|")
    for r in unmeasured_by_collected[:20]:
        L.append(f"| {r['site_id']} | {r['collected']:,} | {r['health']} |")
    L.append("")

    OUT_MD.write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()

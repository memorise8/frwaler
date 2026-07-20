"""
Phase 0 — Input Normalization
Loads scroll_index.xlsx (대상종합 sheet) and matches entries against
crawler/sites/configs/*.json, then writes data/audit/scroll_index.json.
"""

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import openpyxl

# ── paths ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
XLSX_PATH = BASE_DIR / "scroll_index.xlsx"
SHEET_NAME = "대상종합"
CONFIG_DIR = BASE_DIR / "crawler" / "sites" / "configs"
OUTPUT_DIR = BASE_DIR / "data" / "audit"
OUTPUT_PATH = OUTPUT_DIR / "scroll_index.json"

# zero-width / invisible Unicode characters to strip from URLs
_ZERO_WIDTH = "​‌‍﻿⁠"


# ── helpers ────────────────────────────────────────────────────────────────

def clean_url(raw) -> str | None:
    """Strip whitespace and zero-width chars; return None if empty."""
    if raw is None:
        return None
    s = str(raw).strip().strip(_ZERO_WIDTH).strip()
    return s if s else None


def normalize_parsed(url: str):
    """Return (host, path, query) tuple with lowercased host, trailing-/ stripped path."""
    p = urlparse(url)
    host = p.netloc.lower()
    path = p.path.rstrip("/") or "/"
    query = p.query
    return host, path, query


def host_slug(host: str) -> str:
    """www.dhs.gov → dhs-gov"""
    h = host.lower()
    if h.startswith("www."):
        h = h[4:]
    return h.replace(".", "-")


def entry_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]


# ── load configs ───────────────────────────────────────────────────────────

def load_configs(config_dir: Path) -> dict:
    """
    Returns index: host → list of dicts
      { config_file, matched_url, host, path, query }
    Configs without list_page.url are counted but not indexed.
    """
    index: dict[str, list[dict]] = {}
    scanned = 0
    no_url_files = []

    for fpath in sorted(config_dir.glob("*.json")):
        scanned += 1
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
        except Exception:
            continue

        list_page = data.get("list_page") or {}
        lp_url = list_page.get("url")
        if not lp_url:
            no_url_files.append(fpath.name)
            continue

        h, path, query = normalize_parsed(lp_url)
        if not h:
            no_url_files.append(fpath.name)
            continue

        entry = {
            "config_file": fpath.name,
            "matched_url": lp_url,
            "host": h,
            "path": path,
            "query": query,
        }
        index.setdefault(h, []).append(entry)

    return index, scanned, no_url_files


# ── match logic ────────────────────────────────────────────────────────────

LEVEL_ORDER = {"strict": 0, "loose": 1, "host-only": 2, "none": 3}


def match_level(e_host, e_path, e_query, cfg: dict) -> str:
    if e_host != cfg["host"]:
        return "none"
    if e_path == cfg["path"] and e_query == cfg["query"]:
        return "strict"
    # loose: one path is prefix of the other
    cp = cfg["path"]
    if e_path.startswith(cp) or cp.startswith(e_path):
        return "loose"
    return "host-only"


def best_match(e_host: str, e_path: str, e_query: str, index: dict) -> dict:
    candidates = index.get(e_host, [])
    if not candidates:
        return {"level": "none", "config_file": None, "matched_url": None}

    best_lvl = "none"
    best_cfg = None
    for cfg in candidates:
        lvl = match_level(e_host, e_path, e_query, cfg)
        if LEVEL_ORDER[lvl] < LEVEL_ORDER[best_lvl]:
            best_lvl = lvl
            best_cfg = cfg
        if best_lvl == "strict":
            break

    if best_cfg is None:
        return {"level": "none", "config_file": None, "matched_url": None}
    return {
        "level": best_lvl,
        "config_file": best_cfg["config_file"],
        "matched_url": best_cfg["matched_url"],
    }


def _parse_serial(val):
    """Return int if val is numeric, str if text, None if absent."""
    if val is None:
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val) if val == int(val) else val
    s = str(val).strip()
    try:
        return int(s)
    except ValueError:
        return s if s else None


# ── xlsx parse ─────────────────────────────────────────────────────────────

def parse_xlsx(xlsx_path: Path, sheet_name: str):
    wb = openpyxl.load_workbook(str(xlsx_path), read_only=True, data_only=True)
    ws = wb[sheet_name]

    rows_iter = ws.iter_rows(values_only=True)
    _header = next(rows_iter)  # skip header

    entries = []
    skipped = 0
    total_rows = 0

    for raw_row in rows_iter:
        total_rows += 1
        # columns: 시트명, 일련번호, 기관명, URL주소, 구분, 세부구분
        sheet_val = raw_row[0]
        serial = raw_row[1]
        name_val = raw_row[2]
        url_raw = raw_row[3]
        gubun_val = raw_row[4]
        detail_val = raw_row[5]

        url = clean_url(url_raw)
        if not url or not url.lower().startswith("http"):
            skipped += 1
            continue

        entries.append({
            "_row_idx": total_rows,  # 1-based, header excluded
            "sheet": str(sheet_val) if sheet_val is not None else None,
            "일련번호": _parse_serial(serial),
            "name": str(name_val).strip() if name_val is not None else None,
            "url": url,
            "gubun": str(gubun_val).strip() if gubun_val is not None else None,
            "detail": str(detail_val).strip() if detail_val is not None else None,
        })

    wb.close()
    return entries, total_rows + 1, skipped  # total_rows+1 includes header row counted


# ── main ───────────────────────────────────────────────────────────────────

def main():
    # 1. Load configs
    config_index, config_scanned, no_url_configs = load_configs(CONFIG_DIR)

    # 2. Parse xlsx
    raw_entries, total_rows_in_sheet, skipped = parse_xlsx(XLSX_PATH, SHEET_NAME)

    # 3. Build entries with dedup + host normalization + matching
    seen_urls: dict[str, bool] = {}
    entries = []

    for raw in raw_entries:
        url = raw["url"]
        eid = entry_id(url)

        is_dup = url in seen_urls
        if not is_dup:
            seen_urls[url] = True

        parsed = urlparse(url)
        h = parsed.netloc.lower()
        hs = host_slug(h)
        e_path = parsed.path.rstrip("/") or "/"
        e_query = parsed.query

        cm = best_match(h, e_path, e_query, config_index)

        entries.append({
            "entry_id": eid,
            "sheet": raw["sheet"],
            "row_idx": raw["_row_idx"],
            "일련번호": raw["일련번호"],
            "name": raw["name"],
            "url": url,
            "host": h,
            "host_slug": hs,
            "gubun": raw["gubun"],
            "detail": raw["detail"],
            "is_duplicate_url": is_dup,
            "config_match": cm,
        })

    # 4. Stats
    unique_hosts = len({e["host"] for e in entries})
    duplicate_count = sum(1 for e in entries if e["is_duplicate_url"])

    # summary
    per_sheet: dict[str, int] = {}
    per_gubun: dict[str, int] = {}
    per_level = {"strict": 0, "loose": 0, "host-only": 0, "none": 0}
    matched_configs: set[str] = set()

    for e in entries:
        sh = e["sheet"] or "(none)"
        per_sheet[sh] = per_sheet.get(sh, 0) + 1

        g = e["gubun"] or "(none)"
        per_gubun[g] = per_gubun.get(g, 0) + 1

        lvl = e["config_match"]["level"]
        per_level[lvl] += 1
        if lvl in ("strict", "loose") and e["config_match"]["config_file"]:
            matched_configs.add(e["config_match"]["config_file"])

    matched_configs_sorted = sorted(matched_configs)

    # 5. Build output document
    doc = {
        "metadata": {
            "source": "scroll_index.xlsx",
            "sheet": SHEET_NAME,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_rows_in_sheet": total_rows_in_sheet,
            "valid_url_rows": len(entries),
            "skipped_rows": skipped,
            "unique_hosts": unique_hosts,
            "duplicate_urls": duplicate_count,
            "config_files_scanned": config_scanned,
            "config_files_path": "crawler/sites/configs",
        },
        "entries": entries,
        "summary": {
            "per_sheet": per_sheet,
            "per_gubun": per_gubun,
            "per_match_level": per_level,
            "matched_configs": matched_configs_sorted,
            "_note": (
                "humoruniv.json and inven-diablo2.json are test fixtures "
                "unrelated to scroll_index.xlsx; they are included in the "
                "config scan but produce no matches (host never overlaps)."
            ),
        },
    }

    # 6. Write output
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_text = json.dumps(doc, ensure_ascii=False, indent=2)
    OUTPUT_PATH.write_text(output_text, encoding="utf-8")
    size_kb = OUTPUT_PATH.stat().st_size / 1024

    # 7. Console summary
    # Per-sheet strict/loose breakdown for sheets with ≥1 strict match
    sheet_strict: dict[str, int] = {}
    sheet_loose: dict[str, int] = {}
    for e in entries:
        sh = e["sheet"] or "(none)"
        lvl = e["config_match"]["level"]
        if lvl == "strict":
            sheet_strict[sh] = sheet_strict.get(sh, 0) + 1
        elif lvl == "loose":
            sheet_loose[sh] = sheet_loose.get(sh, 0) + 1

    # per-config entry counts (strict or loose)
    config_entry_counts: dict[str, int] = {}
    for e in entries:
        cf = e["config_match"]["config_file"]
        if cf and e["config_match"]["level"] in ("strict", "loose"):
            config_entry_counts[cf] = config_entry_counts.get(cf, 0) + 1

    lines = [
        f"[Phase 0] scroll_index.xlsx → data/audit/scroll_index.json",
        f"  total rows in {SHEET_NAME} sheet: {total_rows_in_sheet}",
        f"  valid URL rows:               {len(entries)}",
        f"  skipped (no URL / non-http):  {skipped}",
        f"  unique hosts:                 {unique_hosts}",
        f"  duplicate URLs:               {duplicate_count}",
        "",
        f"config files scanned: {config_scanned} (in crawler/sites/configs/)",
        f"  configs without list_page.url: {', '.join(no_url_configs) if no_url_configs else 'none'}",
        "",
        "config match distribution:",
        f"  strict     : {per_level['strict']}  (URL exact match — skip in audit)",
        f"  loose      : {per_level['loose']}  (host + path prefix match — flag for paste-eligibility)",
        f"  host-only  : {per_level['host-only']}  (same host, different path — informational only)",
        f"  none       : {per_level['none']}  (new — full audit needed)",
        "",
        "matched configs (first 10):",
    ]

    top_configs = sorted(config_entry_counts.items(), key=lambda x: -x[1])[:10]
    for cf, cnt in top_configs:
        lines.append(f"  {cf:<45} ← entries [{cnt}]")

    lines += [
        "",
        "per-sheet matches (sheets with ≥1 strict match):",
    ]
    sheets_with_strict = sorted(
        [(sh, cnt) for sh, cnt in sheet_strict.items() if cnt > 0],
        key=lambda x: -x[1],
    )
    for sh, sc in sheets_with_strict:
        lc = sheet_loose.get(sh, 0)
        total_sh = per_sheet.get(sh, 0)
        lines.append(f"  {sh}: {total_sh} entries, {sc} strict, {lc} loose")

    lines += [
        "",
        f"  NOTE: humoruniv.json and inven-diablo2.json are test fixtures; "
        f"no overlap with xlsx hosts.",
        "",
        f"✅ wrote data/audit/scroll_index.json ({size_kb:.1f} KB)",
    ]

    print("\n".join(lines))


if __name__ == "__main__":
    main()

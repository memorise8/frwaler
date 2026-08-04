# -*- coding: utf-8 -*-
"""Coverage audit (read-only) — how much of each source's data did we collect?

For every site in the DB, estimate `source_total` (how many items the source
actually has) via a chain of cheap platform probes, then compare against our
`collected` count to quantify shortfall.

Probe chain (first hit wins):
  1. CKAN            {base}/api/3/action/package_search?rows=0            -> result.count
  2. DSpace REST      {base}/server/api/discover/search/objects?size=1     -> page.totalElements
                       (+ legacy {base}/rest/items header fallback)
  3. OAI-PMH          {base}{/server/oai/request,/oai/request,/oai,...}    -> resumptionToken@completeListSize
  4. WordPress REST    {base}/wp-json/wp/v2/posts?per_page=1               -> X-WP-Total header
  5. HAL / Solr        api.archives-ouvertes.fr/search/{portal}/ or {base}/select? -> response.numFound
  6. Generic HTML      list page fetched via StealthSession, regex "N results" style patterns
  7. unknown           source_total=None

Usage:
    source .venv/bin/activate
    export PYTHONPATH=/data_raid/ruci_workspace/frwaler_job
    python3 scripts/audit/coverage_audit.py [--workers 12] [--timeout 28] [--limit N] [--summary-only]

Outputs (incremental, flushed per site so partial progress survives):
    scripts/audit/coverage_report.csv
    scripts/audit/coverage_summary.md   (written after the run, or via --summary-only)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, parse_qs

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REPO = "/data_raid/ruci_workspace/frwaler_job"
DB = f"{REPO}/libertree-app/data/libertree.db"
CONFIGS_DIR = f"{REPO}/crawler/sites/configs"
OUT_CSV = f"{REPO}/scripts/audit/coverage_report.csv"
OUT_MD = f"{REPO}/scripts/audit/coverage_summary.md"

CSV_FIELDS = ["site_id", "sheet", "site_url", "collected", "source_total",
              "coverage_pct", "method", "status", "note"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

REQ_TIMEOUT = 10          # per-HTTP-request timeout (secs)
DEFAULT_SITE_TIMEOUT = 28  # soft overall probe budget per site (secs)

_csv_lock = threading.Lock()


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    s.verify = False
    return s


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def candidate_bases(site_url: str) -> list[str]:
    """origin, plus origin+first-path-segment (CKAN often mounted under /data etc.)."""
    try:
        p = urlparse(site_url)
    except Exception:
        return []
    if not p.scheme or not p.netloc:
        return []
    origin = f"{p.scheme}://{p.netloc}"
    bases = [origin]
    parts = [x for x in p.path.split("/") if x]
    if parts:
        sub = f"{origin}/{parts[0]}"
        if sub not in bases:
            bases.append(sub)
    return bases


def load_config(site_id: str) -> dict | None:
    fp = f"{CONFIGS_DIR}/{site_id}.json"
    if not os.path.exists(fp):
        return None
    try:
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def deadline_ok(deadline: float) -> bool:
    return time.time() < deadline


# ---------------------------------------------------------------------------
# Platform adapters — each returns (total:int|None, method:str|None, note:str|None)
# ---------------------------------------------------------------------------

def probe_ckan(sess, bases, site_url, deadline):
    p = urlparse(site_url)
    qs = parse_qs(p.query)
    fq = []
    if "groups" in qs:
        fq.append(f"groups:{qs['groups'][0]}")
    if "organization" in qs:
        fq.append(f"organization:{qs['organization'][0]}")
    for base in bases:
        if not deadline_ok(deadline):
            return None, "ckan", "deadline"
        url = f"{base}/api/3/action/package_search"
        params = {"rows": 0}
        if fq:
            params["fq"] = " AND ".join(fq)
        try:
            r = sess.get(url, params=params, timeout=REQ_TIMEOUT, allow_redirects=True)
            if r.status_code != 200:
                continue
            data = r.json()
            count = data.get("result", {}).get("count")
            if isinstance(count, int):
                return count, "ckan", url
        except Exception:
            continue
    return None, None, None


def probe_dspace(sess, bases, deadline):
    for base in bases:
        if not deadline_ok(deadline):
            return None, "dspace", "deadline"
        try:
            r = sess.get(f"{base}/server/api/discover/search/objects",
                         params={"size": 1}, timeout=REQ_TIMEOUT,
                         headers={"Accept": "application/json"})
            if r.status_code == 200:
                data = r.json()
                total = (data.get("_embedded", {}).get("searchResult", {})
                         .get("page", {}).get("totalElements"))
                if isinstance(total, int):
                    return total, "dspace_rest7", f"{base}/server/api/discover/search/objects"
        except Exception:
            pass
        try:
            r = sess.get(f"{base}/rest/items", params={"limit": 1, "offset": 0},
                         timeout=REQ_TIMEOUT, headers={"Accept": "application/json"})
            if r.status_code == 200:
                for h in ("X-Total-Count", "totalCount", "Total-Count"):
                    if h in r.headers:
                        try:
                            return int(r.headers[h]), "dspace_legacy_header", base
                        except ValueError:
                            pass
        except Exception:
            pass
    return None, None, None


OAI_PATHS = ["/server/oai/request", "/oai/request", "/oai", "/cgi/oai2", "/dspace-oai/request"]
_OAI_NS = {"o": "http://www.openarchives.org/OAI/2.0/"}


def probe_oai(sess, bases, deadline):
    for base in bases:
        for path in OAI_PATHS:
            if not deadline_ok(deadline):
                return None, "oai", "deadline"
            url = f"{base}{path}"
            try:
                r = sess.get(url, params={"verb": "ListIdentifiers", "metadataPrefix": "oai_dc"},
                             timeout=REQ_TIMEOUT)
                if r.status_code != 200 or "<OAI-PMH" not in r.text:
                    continue
                root = ET.fromstring(r.text)
                err = root.find(".//o:error", _OAI_NS)
                if err is not None:
                    if err.get("code") == "noRecordsFound":
                        return 0, "oai_empty", url
                    continue
                rt = root.find(".//o:resumptionToken", _OAI_NS)
                if rt is not None:
                    if rt.get("completeListSize"):
                        return int(rt.get("completeListSize")), "oai_resumption", url
                    # resumptionToken present but no completeListSize attr means
                    # there IS more data beyond this page — the page count is a
                    # severe undercount, not a total, so this OAI attempt is a bust.
                    continue
                # no resumptionToken at all -> the whole set fit on one page
                ids = root.findall(".//o:header", _OAI_NS)
                if ids:
                    return len(ids), "oai_page", url
            except Exception:
                continue
    return None, None, None


_WP_CORE_TYPES = {
    "posts", "pages", "media", "menu-items", "blocks", "templates",
    "template-parts", "global-styles", "navigation", "font-families",
    "font-faces", "comments", "taxonomies", "types", "statuses", "settings",
    "themes", "plugins", "widgets", "widget-types", "block-types", "users",
    "search", "block-renderer", "sidebars", "status", "oembed",
}


def _wp_total(sess, base, route, deadline):
    if not deadline_ok(deadline):
        return None
    try:
        r = sess.get(f"{base}/wp-json/wp/v2/{route}", params={"per_page": 1}, timeout=REQ_TIMEOUT)
        total = r.headers.get("X-WP-Total")
        if total is not None:
            return int(total)
    except Exception:
        pass
    return None


def probe_wordpress(sess, bases, site_url, deadline):
    p = urlparse(site_url)
    path_words = {w for w in re.split(r"[/\-_.]+", p.path.lower()) if w}
    for base in bases:
        if not deadline_ok(deadline):
            return None, "wordpress", "deadline"
        # discover a custom post type whose slug matches a word in the site's URL path
        matched_route = None
        try:
            r = sess.get(f"{base}/wp-json/wp/v2/types", timeout=REQ_TIMEOUT)
            if r.status_code == 200:
                types = r.json()
                for t in types.values():
                    if not isinstance(t, dict):
                        continue
                    rb = t.get("rest_base")
                    if not rb or rb in _WP_CORE_TYPES:
                        continue
                    rb_words = {w for w in re.split(r"[/\-_]+", rb.lower()) if w}
                    if rb_words & path_words:
                        matched_route = rb
                        break
        except Exception:
            pass

        if matched_route:
            total = _wp_total(sess, base, matched_route, deadline)
            if total is not None:
                return total, f"wordpress_{matched_route}", f"{base}/wp-json/wp/v2/{matched_route}"

        # generic posts fallback — only trust it if non-zero (0 is usually the
        # wrong content type for a site we're actively crawling, not a real total)
        total = _wp_total(sess, base, "posts", deadline)
        if total:
            return total, "wordpress_posts", f"{base}/wp-json/wp/v2/posts"
    return None, None, None


def _hal_portal(netloc: str) -> str | None:
    parts = netloc.split(".")
    for marker in ("hal", "archives-ouvertes"):
        if marker in parts:
            idx = parts.index(marker)
            if idx > 0:
                return parts[0]
    return None


def probe_hal_solr(sess, site_url, deadline):
    p = urlparse(site_url)
    host = p.netloc.lower()
    full = site_url.lower()
    if "hal." in host or "archives-ouvertes" in host:
        portal = _hal_portal(host)
        if portal:
            url = "https://api.archives-ouvertes.fr/search/%s/" % portal
        elif host in ("hal.science", "archives-ouvertes.fr"):
            url = "https://api.archives-ouvertes.fr/search/"
        else:
            return None, None, None
        if not deadline_ok(deadline):
            return None, "hal_solr", "deadline"
        try:
            r = sess.get(url, params={"q": "*:*", "wt": "json", "rows": 0}, timeout=REQ_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                nf = data.get("response", {}).get("numFound")
                if isinstance(nf, int):
                    return nf, "hal_solr", url
        except Exception:
            pass
        return None, None, None
    if "solr" in host or "select?" in full:
        base = f"{p.scheme}://{p.netloc}{p.path}"
        if not deadline_ok(deadline):
            return None, "solr_generic", "deadline"
        try:
            r = sess.get(base, params={"q": "*:*", "rows": 0, "wt": "json"}, timeout=REQ_TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                nf = data.get("response", {}).get("numFound")
                if isinstance(nf, int):
                    return nf, "solr_generic", base
        except Exception:
            pass
    return None, None, None


_COUNT_WORD = (r"results?|résultats?|resultados?|documentos?|documents?|items?|"
               r"records?|Ergebnisse|risultati|resultaten|entries")
COUNT_RES = [
    # "1,234 results" — keyword mandatory, directly after the number
    re.compile(r"([\d][\d.,]{0,12})\s*(?:%s)\b" % _COUNT_WORD, re.I),
    # "Showing 1-20 of 1,234 results" — keyword mandatory on BOTH sides so a
    # bare date/slash (e.g. "Analyse / 8.6.2026") can never match
    re.compile(r"(?:of|sur|von|de)\s+([\d][\d.,]{0,12})\s+(?:%s)\b" % _COUNT_WORD, re.I),
    re.compile(r"([\d][\d.,]{0,12})\s*件"),
    re.compile(r"([\d][\d.,]{0,12})\s*αποτελέσματα", re.I),
]

_DATE_LIKE = re.compile(r"^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$")


def _to_int(numstr: str) -> int | None:
    """Parse a matched number, honoring thousands separators — and rejecting
    anything date-shaped (DD.MM.YYYY etc. gets misread as a huge number if we
    just strip separators blindly)."""
    s = numstr.strip()
    if _DATE_LIKE.match(s):
        return None
    if "," in s and "." in s:
        thousands, decimal = (".", ",") if s.rfind(",") > s.rfind(".") else (",", ".")
        groups = s.split(decimal)[0].split(thousands)
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        groups = s.split(sep)
        if len(groups) >= 2 and len(groups[-1]) in (1, 2):
            groups = groups[:-1]  # trailing decimal fraction, drop it
    else:
        groups = [s]
    if not groups or not groups[0].isdigit() or len(groups[0]) > 3:
        return None
    if any(not (g.isdigit() and len(g) == 3) for g in groups[1:]):
        return None
    digits = "".join(groups)
    if not digits.isdigit():
        return None
    return int(digits)


def probe_html_count(sess, list_url, deadline):
    if not list_url or not deadline_ok(deadline):
        return None, None, "deadline"
    try:
        from crawler.stealth_fetcher import StealthSession
        ss = StealthSession(timeout=REQ_TIMEOUT, playwright_timeout=15)
        html, info = ss.fetch_html(list_url)
    except Exception as e:
        return None, None, f"stealth_error:{type(e).__name__}"
    if not html:
        return None, None, "no_html"
    best = None
    for rx in COUNT_RES:
        for m in rx.finditer(html):
            v = _to_int(m.group(1))
            if v is None or v <= 0 or v > 2_000_000:
                continue
            if best is None or v > best:
                best = v
    if best is not None:
        return best, "html_count_regex", list_url
    return None, None, "no_match"


# ---------------------------------------------------------------------------
# Per-site orchestration
# ---------------------------------------------------------------------------

def probe_site(site_id: str, site_url: str, timeout_secs: float):
    deadline = time.time() + timeout_secs
    sess = _session()
    bases = candidate_bases(site_url)
    cfg = load_config(site_id)
    list_url = None
    if cfg:
        list_url = (cfg.get("list_page") or {}).get("url")
    if list_url:
        try:
            lp = urlparse(list_url)
            if lp.scheme and lp.netloc:
                lp_origin = f"{lp.scheme}://{lp.netloc}"
                if lp_origin not in bases:
                    bases.append(lp_origin)
        except Exception:
            pass

    if not bases:
        return None, "unknown", "bad_site_url"

    chain = [
        lambda: probe_ckan(sess, bases, site_url, deadline),
        lambda: probe_dspace(sess, bases, deadline),
        lambda: probe_oai(sess, bases, deadline),
        lambda: probe_wordpress(sess, bases, site_url, deadline),
        lambda: probe_hal_solr(sess, site_url, deadline),
    ]
    for fn in chain:
        if not deadline_ok(deadline):
            break
        try:
            total, method, note = fn()
        except Exception as e:
            total, method, note = None, None, f"probe_error:{type(e).__name__}"
        if total is not None:
            return total, method, note or ""

    if deadline_ok(deadline):
        target = list_url or site_url
        total, method, note = probe_html_count(sess, target, deadline)
        if total is not None:
            return total, method, note or ""

    return None, "unknown", ""


def compute_status(collected: int, source_total: int | None) -> tuple[float | None, str]:
    if source_total is None:
        return None, "unknown"
    if source_total <= 0:
        return (100.0 if collected == 0 else 0.0), ("complete" if collected == 0 else "over")
    pct = round(100.0 * collected / source_total, 1)
    if collected > source_total:
        return pct, "over"
    if pct >= 95.0:
        return pct, "complete"
    return pct, "partial"


# ---------------------------------------------------------------------------
# Main audit run
# ---------------------------------------------------------------------------

def load_sites(limit: int | None):
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = conn.cursor()
    sites = cur.execute("SELECT site_id, site_name, site_url, sheet FROM sites"
                         " ORDER BY site_id").fetchall()
    collected = dict(cur.execute("SELECT site_id, COUNT(*) FROM documents GROUP BY site_id").fetchall())
    conn.close()
    if limit:
        sites = sites[:limit]
    return sites, collected


def run_audit(workers: int, timeout_secs: float, limit: int | None):
    sites, collected_map = load_sites(limit)
    total_n = len(sites)
    print(f"[coverage_audit] {total_n} sites to probe, {workers} workers, "
          f"{timeout_secs}s/site budget")

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        f.flush()

        def worker(site_id, site_name, site_url, sheet):
            collected = collected_map.get(site_id, 0)
            try:
                source_total, method, note = probe_site(site_id, site_url or "", timeout_secs)
            except Exception as e:
                source_total, method, note = None, "unknown", f"fatal:{type(e).__name__}"
            pct, status = compute_status(collected, source_total)
            return {
                "site_id": site_id, "sheet": sheet or "", "site_url": site_url or "",
                "collected": collected, "source_total": (source_total if source_total is not None else ""),
                "coverage_pct": (pct if pct is not None else ""),
                "method": method or "unknown", "status": status,
                "note": (note or "")[:200],
            }

        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(worker, sid, sname, surl, sheet): sid
                    for sid, sname, surl, sheet in sites}
            for fut in as_completed(futs):
                row = fut.result()
                with _csv_lock:
                    writer.writerow(row)
                    f.flush()
                done += 1
                if done % 25 == 0 or done == total_n:
                    print(f"[coverage_audit] {done}/{total_n} done  (last: {row['site_id']} "
                          f"-> {row['status']}/{row['method']})")

    print(f"[coverage_audit] wrote {OUT_CSV}")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def write_summary():
    if not os.path.exists(OUT_CSV):
        print(f"[coverage_audit] no {OUT_CSV} found; run the audit first")
        return

    rows = list(csv.DictReader(open(OUT_CSV, encoding="utf-8")))

    def as_int(v):
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    def as_float(v):
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    for r in rows:
        r["_collected"] = as_int(r["collected"]) or 0
        r["_source_total"] = as_int(r["source_total"])
        r["_coverage_pct"] = as_float(r["coverage_pct"])

    total_sites = len(rows)
    status_counts = Counter(r["status"] for r in rows)
    measurable = [r for r in rows if r["_source_total"] is not None]

    total_collected_all = sum(r["_collected"] for r in rows)
    total_collected_measurable = sum(r["_collected"] for r in measurable)
    total_source_measurable = sum(r["_source_total"] for r in measurable)
    overall_pct = (round(100.0 * total_collected_measurable / total_source_measurable, 1)
                   if total_source_measurable else None)
    shortfall = sum(r["_source_total"] - r["_collected"] for r in measurable)

    top30 = sorted(measurable, key=lambda r: -(r["_source_total"] - r["_collected"]))[:30]

    sheet_agg = defaultdict(lambda: {"collected": 0, "source_total": 0, "measurable": 0, "total": 0})
    for r in rows:
        s = sheet_agg[r["sheet"] or "(none)"]
        s["total"] += 1
        s["collected"] += r["_collected"]
        if r["_source_total"] is not None:
            s["source_total"] += r["_source_total"]
            s["measurable"] += 1

    lines = []
    lines.append("# Coverage Audit Summary")
    lines.append("")
    lines.append(f"Generated from `{os.path.basename(OUT_CSV)}` — {total_sites} sites total.")
    lines.append("")
    lines.append("## Overall")
    lines.append("")
    lines.append(f"- Total collected (all sites): **{total_collected_all:,}**")
    lines.append(f"- Measurable sites (source_total known): **{len(measurable)}/{total_sites}** "
                 f"({total_sites - len(measurable)} unknown/unmeasurable)")
    lines.append(f"- Total collected (measurable sites only): **{total_collected_measurable:,}**")
    lines.append(f"- Total source_total (measurable sites): **{total_source_measurable:,}**")
    lines.append(f"- Overall coverage (measurable sites): **{overall_pct}%**" if overall_pct is not None
                 else "- Overall coverage (measurable sites): n/a")
    lines.append(f"- Estimated absolute shortfall (source_total − collected, measurable sites): "
                 f"**{shortfall:,}**")
    lines.append("")
    lines.append("## Sites by status")
    lines.append("")
    lines.append("| status | sites |")
    lines.append("|---|---|")
    for st in ("complete", "partial", "over", "unknown"):
        lines.append(f"| {st} | {status_counts.get(st, 0)} |")
    lines.append("")
    lines.append("## Top 30 sites by absolute shortfall (re-crawl priorities)")
    lines.append("")
    lines.append("| site_id | sheet | collected | source_total | coverage% | method | shortfall |")
    lines.append("|---|---|---:|---:|---:|---|---:|")
    for r in top30:
        shortfall_r = r["_source_total"] - r["_collected"]
        lines.append(f"| {r['site_id']} | {r['sheet']} | {r['_collected']:,} | "
                     f"{r['_source_total']:,} | {r['coverage_pct']} | {r['method']} | {shortfall_r:,} |")
    lines.append("")
    lines.append("## Per-sheet rollup")
    lines.append("")
    lines.append("| sheet | sites | measurable | collected | source_total | coverage% |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for sheet, agg in sorted(sheet_agg.items(), key=lambda kv: -kv[1]["collected"]):
        pct = round(100.0 * agg["collected"] / agg["source_total"], 1) if agg["source_total"] else None
        lines.append(f"| {sheet} | {agg['total']} | {agg['measurable']} | {agg['collected']:,} | "
                     f"{agg['source_total']:,} | {pct if pct is not None else 'n/a'} |")
    lines.append("")

    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"[coverage_audit] wrote {OUT_MD}")
    print("\n".join(lines[:40]))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--timeout", type=float, default=DEFAULT_SITE_TIMEOUT,
                     help="soft per-site probe budget in seconds")
    ap.add_argument("--limit", type=int, default=None, help="limit number of sites (testing)")
    ap.add_argument("--summary-only", action="store_true",
                     help="skip probing, regenerate summary from existing CSV")
    args = ap.parse_args()

    if not args.summary_only:
        run_audit(args.workers, args.timeout, args.limit)
    write_summary()


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""Remeasure TRUE collectable totals for BIG / repository-platform sites (read-only).

The previous audit (coverage_audit / full_survey) left many big sites as
lower-bounds, `unknown`, or as suspect `html_count_regex` "exact" numbers.
This standalone script — modelled on `remeasure_hal.py` — replays each
candidate crawler's own filters (where visible in the site_url) against the
platform's total-count API and records the true count.

Supported platforms (detected from site_url + light live probing):
  * CKAN            /api/3/action/package_search?rows=0   -> result.count
  * DSpace v7 REST  /server/api/discover/search/objects?size=1 -> page.totalElements
  * DSpace v6 / XMLUI / JSPUI  -> OAI-PMH completeListSize
  * EPrints         /cgi/oai2 (OAI)  -> completeListSize
  * OAI-PMH generic -> ListRecords resumptionToken@completeListSize
  * Invenio / SONAR /api/records?size=1 -> hits.total
  * WordPress       /wp-json/wp/v2/posts?per_page=1 -> X-WP-Total header
  * gov.uk finder   <finder>.json?<filters> -> total   (exact API total)
  * gov.scot / e-stat -> on-page count (keyword-adjacent regex) re-verification
  * ntrs.nasa.gov   POST /api/citations/search -> stats.total
  * Generic fallback: keyword-adjacent count regex on the listing HTML.

Candidate selection (see build_candidates):
  A site is a candidate iff it is NOT already measured with an exact API total
  (hal_totals.csv, or coverage_report.csv with a real API method
  ckan/dspace_rest7/oai_resumption/wordpress_*) AND
  (collected >= 100 OR its site_url matches a repository/data-portal platform).
  PLUS the known-suspect html_count_regex "exact" sites are always re-verified:
  e-stat-go-jp-stat-search, gov-scot-publications, gov-uk-search, gov-scot*,
  and any html_count_regex row with source_total > 20000.

Usage:
    source .venv/bin/activate
    export PYTHONPATH=/data_raid/ruci_workspace/frwaler_job
    python3 scripts/audit/remeasure_big.py

Output:
    scripts/audit/big_totals.csv
"""
from __future__ import annotations

import csv
import random
import re
import sqlite3
import time
from urllib.parse import urlparse, parse_qs, urlencode

import urllib3
from curl_cffi import requests as crequests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REPO = "/data_raid/ruci_workspace/frwaler_job"
DB = f"{REPO}/libertree-app/data/libertree.db"
AUDIT = f"{REPO}/scripts/audit"
HAL_CSV = f"{AUDIT}/hal_totals.csv"
COVERAGE_CSV = f"{AUDIT}/coverage_report.csv"
OUT_CSV = f"{AUDIT}/big_totals.csv"

REQ_TIMEOUT = 10
IMPERSONATE = "chrome"

# coverage methods that already give a real, exact API total -> treat as done
REAL_API_METHODS = {"ckan", "dspace_rest7", "oai_resumption"}
# html_count_regex rows above this source_total are re-verified here
HCR_REVERIFY_THRESHOLD = 20000
# always re-verify these specific suspect html_count_regex "exact" sites
FORCE_REVERIFY = {
    "e-stat-go-jp-stat-search",
    "gov-scot-publications",
    "gov-scot",
    "gov-scot-statistics-and-resea",
    "gov-uk-search",
}

# keyword tokens that must sit adjacent to a number for the generic fallback
COUNT_KEYWORDS = [
    "results found", "results", "résultats", "resultaten", "resultados",
    "records found", "records", "documents", "publications", "件", "matches",
    "entries", "items found", "datasets", "treffer", "risultati",
]


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
def http_get(sess, url, headers=None, retry=1):
    """GET with 1 retry. Returns (response_or_None, error_str_or_None)."""
    last = "unknown"
    for attempt in range(retry + 1):
        try:
            r = sess.get(url, headers=headers, timeout=REQ_TIMEOUT,
                         verify=False, impersonate=IMPERSONATE)
            return r, None
        except Exception as exc:  # noqa: BLE001
            last = type(exc).__name__
        if attempt < retry:
            time.sleep(0.5)
    return None, last


def http_post(sess, url, json_body, headers=None, retry=1):
    last = "unknown"
    for attempt in range(retry + 1):
        try:
            r = sess.post(url, json=json_body, headers=headers,
                          timeout=REQ_TIMEOUT, verify=False,
                          impersonate=IMPERSONATE)
            return r, None
        except Exception as exc:  # noqa: BLE001
            last = type(exc).__name__
        if attempt < retry:
            time.sleep(0.5)
    return None, last


def _is_json(r):
    return "json" in (r.headers.get("content-type", "") or "").lower()


# ---------------------------------------------------------------------------
# candidate selection
# ---------------------------------------------------------------------------
PLATFORM_URL_RE = re.compile(
    r"/dataset|/api/3/action|opendata|datos\.gob|data\.gov|/catalog|avoindata|"
    r"daten|dspace|/handle/|/xmlui|/jspui|/server/api|eprints|/cgi/|/oai|"
    r"invenio|sonar|zenodo|/api/records|/wp-json|repository|ntrs\.nasa|par\.nsf",
    re.I,
)


def load_done_and_reverify():
    done = set()
    for r in csv.DictReader(open(HAL_CSV, encoding="utf-8")):
        done.add(r["site_id"])
    reverify = set(FORCE_REVERIFY)
    for r in csv.DictReader(open(COVERAGE_CSV, encoding="utf-8")):
        m = (r.get("method") or "").strip()
        sid = r["site_id"]
        if m in REAL_API_METHODS or m.startswith("wordpress_"):
            done.add(sid)
        if m == "html_count_regex":
            try:
                st = int(float(r.get("source_total") or 0))
            except ValueError:
                st = 0
            if st > HCR_REVERIFY_THRESHOLD:
                reverify.add(sid)
    return done, reverify


def build_candidates():
    """Return list of dicts: {site_id, sheet, site_url, collected, reverify}."""
    done, reverify = load_done_and_reverify()
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    sites = con.execute(
        "SELECT site_id, site_url, sheet FROM sites ORDER BY site_id"
    ).fetchall()
    collected = {sid: c for sid, c in con.execute(
        "SELECT site_id, COUNT(*) FROM documents GROUP BY site_id"
    )}
    con.close()

    out = []
    for s in sites:
        sid = s["site_id"]
        url = s["site_url"] or ""
        c = collected.get(sid, 0)
        is_reverify = sid in reverify
        if sid in done and not is_reverify:
            continue
        platform_hint = bool(PLATFORM_URL_RE.search(url))
        if not (is_reverify or c >= 100 or platform_hint):
            continue
        out.append({
            "site_id": sid, "sheet": s["sheet"] or "", "site_url": url,
            "collected": c, "reverify": is_reverify,
        })
    # measure the biggest first
    out.sort(key=lambda r: r["collected"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# platform detection
# ---------------------------------------------------------------------------
def detect_platform(url):
    """Return a platform tag string based on the site_url (cheap, no network)."""
    p = urlparse(url)
    host = (p.netloc or "").lower()
    path = (p.path or "").lower()
    full = url.lower()

    # --- special hosts ---
    if "gov.uk" in host and "/search/" in path:
        return "gov_uk_finder"
    if "gov.scot" in host:
        return "onpage_count"
    if "e-stat.go.jp" in host:
        return "onpage_count"
    if "ntrs.nasa.gov" in host:
        return "ntrs"
    if "par.nsf.gov" in host:
        return "generic"  # no clean API; keyword fallback
    if "sonar.ch" in host or "invenio" in full or "zenodo" in host:
        return "invenio"
    if "open.canada.ca" in host:
        return "ckan"

    # --- generic platform families ---
    if "eprints" in host or "/cgi/" in path:
        return "eprints"
    if ("/api/3/action" in full or "/dataset" in path or "opendata" in host
            or "datos.gob" in host or "data.gov" in host or "avoindata" in host
            or host.startswith("data.") or host.startswith("daten.")
            or "/catalog" in path):
        return "ckan"
    if ("dspace" in host or "/handle/" in path or "/xmlui" in path
            or "/jspui" in path or "/server/api" in full):
        return "dspace"
    if "/api/records" in full:
        return "invenio"
    if "/wp-json" in full:
        return "wordpress"
    if "/oai" in path:
        return "oai"
    if host.startswith("repository") or host.startswith("repositorio"):
        return "dspace"
    return "generic"


# ---------------------------------------------------------------------------
# per-platform measurers  -> return (total|None, method, api_url, filters, err)
# ---------------------------------------------------------------------------
def _base(url):
    p = urlparse(url)
    return f"{p.scheme or 'https'}://{p.netloc}"


def measure_ckan(sess, url):
    p = urlparse(url)
    host = p.netloc.lower()
    qs = parse_qs(p.query, keep_blank_values=False)

    # build fq from the crawler's own filters
    fq = []
    fmt = qs.get("resource_format") or qs.get("res_format")
    if fmt:
        fq.append(f"res_format:{fmt[0].upper()}")
    for key in ("groups", "organization"):
        if key in qs and qs[key][0]:
            fq.append(f"{key}:{qs[key][0]}")
    rt = qs.get("resource_type")
    if rt and rt[0]:
        fq.append(f"dataset_type:{rt[0]}")

    # candidate API bases (special-case open.canada.ca -> /data)
    if "open.canada.ca" in host:
        bases = [f"{p.scheme}://open.canada.ca/data"]
    else:
        bases = [f"{p.scheme}://{host}", f"{p.scheme}://{host}/data"]

    filters = ",".join(fq) if fq else ""
    last_err = "no_ckan_endpoint"
    for base in bases:
        params = {"rows": "0"}
        if fq:
            params["fq"] = " ".join(fq)
        api = f"{base}/api/3/action/package_search?{urlencode(params)}"
        r, err = http_get(sess, api)
        if r is None:
            last_err = err
            continue
        if r.status_code != 200 or not _is_json(r):
            last_err = f"http_{r.status_code}"
            continue
        try:
            j = r.json()
            cnt = j.get("result", {}).get("count")
            if isinstance(cnt, int):
                return cnt, "ckan", api, filters, None
            last_err = "no_count"
        except Exception as exc:  # noqa: BLE001
            last_err = f"json_{type(exc).__name__}"
    return None, "ckan", f"{bases[0]}/api/3/action/package_search", filters, last_err


def measure_dspace7(sess, url):
    base = _base(url)
    for api_base in (f"{base}/server/api", f"{base}/api"):
        api = f"{api_base}/discover/search/objects?size=1"
        r, err = http_get(sess, api)
        if r is None:
            continue
        if r.status_code == 200 and _is_json(r):
            try:
                j = r.json()
                te = (j.get("_embedded", {}).get("searchResult", {})
                      .get("page", {}).get("totalElements"))
                if isinstance(te, int):
                    return te, "dspace_rest7", api, "", None
            except Exception:  # noqa: BLE001
                pass
    return None, "dspace_rest7", f"{base}/server/api/discover/search/objects", "", "no_dspace7"


OAI_PATHS = ["/oai", "/oai/request", "/oai/oai", "/cgi/oai2",
             "/server/oai/request", "/do/oai/"]


def measure_oai(sess, url, extra_paths=None):
    base = _base(url)
    paths = (extra_paths or []) + OAI_PATHS
    seen = set()
    last_err = "no_oai_endpoint"
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        ident = f"{base}{path}?verb=Identify"
        r, err = http_get(sess, ident, retry=0)
        if r is None or r.status_code != 200:
            last_err = err or (f"http_{r.status_code}" if r else "no_resp")
            continue
        if "OAI-PMH" not in (r.text or "") and "<Identify>" not in (r.text or ""):
            last_err = "not_oai"
            continue
        # got an OAI endpoint -> ask for ListRecords completeListSize
        lr = f"{base}{path}?verb=ListRecords&metadataPrefix=oai_dc"
        r2, err2 = http_get(sess, lr, retry=0)
        if r2 is not None and r2.status_code == 200:
            m = re.search(r'completeListSize="(\d+)"', r2.text or "")
            if m:
                return int(m.group(1)), "oai_resumption", lr, "", None
            last_err = "no_completeListSize"
        else:
            last_err = err2 or "listrecords_fail"
    return None, "oai_resumption", f"{base}{paths[0]}", "", last_err


def measure_invenio(sess, url):
    base = _base(url)
    p = urlparse(url)
    qs = parse_qs(p.query, keep_blank_values=False)
    filt = ""
    dt = qs.get("document_type")
    extra = ""
    if dt and dt[0]:
        extra = f"&document_type={dt[0]}"
        filt = f"document_type={dt[0]}"
    last_err = "no_invenio_endpoint"
    for api in (f"{base}/api/documents?q=&size=1{extra}",
                f"{base}/api/records?size=1{extra}"):
        r, err = http_get(sess, api, headers={"Accept": "application/json"})
        if r is None:
            last_err = err
            continue
        if r.status_code == 200 and _is_json(r):
            try:
                total = r.json().get("hits", {}).get("total")
                if isinstance(total, dict):
                    total = total.get("value")
                if isinstance(total, int):
                    return total, "invenio", api, filt, None
            except Exception:  # noqa: BLE001
                pass
            last_err = "no_hits_total"
        else:
            last_err = f"http_{r.status_code}"
    return None, "invenio", f"{base}/api/documents", filt, last_err


def measure_wordpress(sess, url):
    base = _base(url)
    api = f"{base}/wp-json/wp/v2/posts?per_page=1"
    r, err = http_get(sess, api)
    if r is None:
        return None, "wordpress_posts", api, "", err
    if r.status_code == 200:
        tot = r.headers.get("X-WP-Total") or r.headers.get("x-wp-total")
        if tot and tot.isdigit():
            return int(tot), "wordpress_posts", api, "", None
    return None, "wordpress_posts", api, "", f"http_{r.status_code}"


def measure_gov_uk(sess, url):
    p = urlparse(url)
    finder = f"{p.scheme}://{p.netloc}{p.path.rstrip('/')}.json"
    api = f"{finder}?{p.query}" if p.query else finder
    filt = p.query
    r, err = http_get(sess, api)
    if r is None:
        return None, "gov_uk_finder", api, filt, err
    if r.status_code == 200 and _is_json(r):
        try:
            total = r.json().get("total")
            if isinstance(total, int):
                return total, "gov_uk_finder", api, filt, None
        except Exception:  # noqa: BLE001
            pass
    return None, "gov_uk_finder", api, filt, f"http_{r.status_code}"


def measure_ntrs(sess, url):
    base = _base(url)
    api = f"{base}/api/citations/search"
    r, err = http_post(sess, api, {"page": {"size": 1, "from": 0}})
    if r is None:
        return None, "ntrs_api", api, "no_filters_applied", err
    if r.status_code == 200 and _is_json(r):
        try:
            total = r.json().get("stats", {}).get("total")
            if isinstance(total, int) and total > 0:
                return total, "ntrs_api", api, "no_filters_applied", None
        except Exception:  # noqa: BLE001
            pass
    return None, "ntrs_api", api, "no_filters_applied", f"http_{r.status_code}"


def measure_generic(sess, url):
    """Fetch the listing HTML and regex a keyword-adjacent count. Conservative."""
    r, err = http_get(sess, url)
    if r is None:
        return None, "html_count_regex", url, "", err
    if r.status_code != 200:
        return None, "html_count_regex", url, "", f"http_{r.status_code}"
    text = r.text or ""
    best = None
    for kw in COUNT_KEYWORDS:
        kw_esc = re.escape(kw)
        # number immediately before keyword  ("50,542 results")
        for m in re.finditer(rf"([\d][\d,\.\s]{{0,12}}?\d|\d)\s*{kw_esc}", text, re.I):
            n = _parse_num(m.group(1))
            if _looks_like_year(n):
                continue  # "2021 records" is almost always a year facet, not a total
            if n is not None and (best is None or n > best):
                best = n
        # number immediately after keyword  ("results: 50,542")
        for m in re.finditer(rf"{kw_esc}[^\d]{{0,8}}([\d][\d,\.\s]{{0,12}}?\d|\d)", text, re.I):
            n = _parse_num(m.group(1))
            if _looks_like_year(n):
                continue
            if n is not None and (best is None or n > best):
                best = n
    if best is not None:
        return best, "html_count_regex", url, "", None
    return None, "html_count_regex", url, "", "no_count_keyword"


def _parse_num(s):
    s = re.sub(r"[\s,\.]", "", s or "")
    if not s.isdigit():
        return None
    v = int(s)
    return v if v > 0 else None


def _looks_like_year(n):
    """A bare 4-digit value in [1900, 2099] scraped next to a keyword is far more
    likely a year facet ("2021 records") than a genuine total, so drop it."""
    return n is not None and 1900 <= n <= 2099


# methods/platforms whose totals are authoritative (exact API or on-page total).
# Generic HTML-scraped counts are recorded but flagged non-authoritative so the
# consolidation step does not override real data with a possibly-garbage number.
AUTHORITATIVE_METHODS = {
    "ckan", "dspace_rest7", "oai_resumption", "invenio", "wordpress_posts",
    "gov_uk_finder", "ntrs_api",
}


def classify_note(platform, method, total):
    if total is None:
        return None
    if total == 0:
        return "error:zero"
    if method in AUTHORITATIVE_METHODS or platform == "onpage_count":
        return "ok"
    return "ok_html_unverified"


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------
def measure_site(sess, url):
    plat = detect_platform(url)
    handlers = {
        "ckan": measure_ckan,
        "dspace": measure_dspace7,        # dspace7 first, OAI fallback below
        "eprints": lambda s, u: measure_oai(s, u, extra_paths=["/cgi/oai2"]),
        "oai": measure_oai,
        "invenio": measure_invenio,
        "wordpress": measure_wordpress,
        "gov_uk_finder": measure_gov_uk,
        "ntrs": measure_ntrs,
        "onpage_count": measure_generic,
        "generic": measure_generic,
    }
    fn = handlers.get(plat, measure_generic)
    total, method, api_url, filters, err = fn(sess, url)

    # dspace7 miss -> try OAI-PMH (v6 / XMLUI / JSPUI)
    if total is None and plat == "dspace":
        t2, m2, a2, f2, e2 = measure_oai(sess, url)
        if t2 is not None:
            return plat, t2, m2, a2, f2, None
        err = f"{err}|{e2}"

    # any platform miss -> generic HTML fallback (single request)
    if total is None and plat not in ("generic", "onpage_count"):
        t3, m3, a3, f3, e3 = measure_generic(sess, url)
        if t3 is not None:
            return plat, t3, m3, a3, f3, None
        err = f"{err}|generic:{e3}"

    return plat, total, method, api_url, filters, err


def main():
    candidates = build_candidates()
    print(f"Candidates: {len(candidates)}")

    sess = crequests.Session()
    results = []
    for i, c in enumerate(candidates):
        sid, url = c["site_id"], c["site_url"]
        try:
            plat, total, method, api_url, filters, err = measure_site(sess, url)
        except Exception as exc:  # noqa: BLE001 - never let one site abort the run
            plat, total, method, api_url, filters = "?", None, "", url, ""
            err = f"exc:{type(exc).__name__}"

        note = classify_note(plat, method, total)
        if note is None:
            note = f"error:{err}" if err else "could_not_measure"

        results.append({
            "site_id": sid,
            "sheet": c["sheet"],
            "platform_detected": plat,
            "total": total if total is not None else "",
            "method": method,
            "api_url": api_url,
            "filters_applied": filters,
            "reverify": "yes" if c["reverify"] else "",
            "collected": c["collected"],
            "note": note,
        })
        flag = total if total is not None else note
        print(f"[{i + 1}/{len(candidates)}] {sid} [{plat}] -> {flag}")
        time.sleep(random.uniform(0.5, 1.0))

    fields = ["site_id", "sheet", "platform_detected", "total", "method",
              "api_url", "filters_applied", "reverify", "collected", "note"]
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(results)

    print_summary(results)


def print_summary(results):
    ok = [r for r in results if r["note"] == "ok"]
    unverified = [r for r in results if r["note"] == "ok_html_unverified"]
    failed = [r for r in results if r["note"] not in ("ok", "ok_html_unverified")]
    total_sum = sum(int(r["total"]) for r in ok)

    print("\n" + "=" * 66)
    print(f"big remeasure complete: {len(results)} candidates "
          f"({len(ok)} ok-authoritative, {len(unverified)} ok_html_unverified, "
          f"{len(failed)} failed)")
    print(f"Sum of totals (ok-authoritative sites): {total_sum:,}")
    print(f"Output: {OUT_CSV}")

    print("\nTop 20 by total:")
    for r in sorted(ok, key=lambda x: int(x["total"]), reverse=True)[:20]:
        print(f"  {int(r['total']):>12,}  {r['site_id']:45s} "
              f"[{r['platform_detected']}/{r['method']}]")

    # re-verification verdict for the suspect html_count_regex sites
    print("\n" + "-" * 66)
    print("RE-VERIFICATION (html_count_regex suspects): API/JSON vs old html count")
    cov = {}
    for row in csv.DictReader(open(COVERAGE_CSV, encoding="utf-8")):
        cov[row["site_id"]] = row
    for r in results:
        if r["reverify"] != "yes":
            continue
        old = cov.get(r["site_id"], {}).get("source_total", "?")
        new = r["total"] if r["total"] != "" else r["note"]
        try:
            verdict = "AGREE" if int(float(old)) == int(r["total"]) else "DIFFER"
        except (ValueError, TypeError):
            verdict = "n/a"
        print(f"  {r['site_id']:35s} old_html={old:>10}  "
              f"new={str(new):>12}  [{r['method']}]  {verdict}")


if __name__ == "__main__":
    main()

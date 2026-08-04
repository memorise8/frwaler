# -*- coding: utf-8 -*-
"""Remeasure HAL crawler capacity WITH the crawler's own query filters (read-only).

The old coverage_audit `probe_hal_solr` queried the HAL Solr API with a bare
`q=*:*` and no filter queries, so every crawler reported its whole portal
corpus (or the 4.6M global corpus for bare hal.science). That is wrong: a HAL
crawler only collects the documents that match the filters baked into its
site_url (submitType_s=file, docType_s=THESE OR COMM ..., collCode_s:..., etc).

This standalone script replays each HAL crawler's site_url filters against the
Solr API and records the true `numFound`.

Rules:
  * portal = subdomain before `.hal.science` / `.archives-ouvertes.fr`
    (e.g. ens-lyon, inria). Bare hal.science/archives-ouvertes.fr -> global
    endpoint https://api.archives-ouvertes.fr/search/ with the filters still
    applied.
  * q param: bare star (`*`) -> `*:*`; a field query (contains `:`) -> verbatim.
  * every other query param except UI-only ones (rows/sort/page/start/fl/wt/
    index) -> an fq filter query. Multi-value OR lists become `field:(A OR B)`.
  * always wt=json, rows=0.

Usage:
    source .venv/bin/activate
    export PYTHONPATH=/data_raid/ruci_workspace/frwaler_job
    python3 scripts/audit/remeasure_hal.py

Output:
    scripts/audit/hal_totals.csv
"""
from __future__ import annotations

import csv
import sqlite3
import time
from urllib.parse import urlparse, parse_qs

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REPO = "/data_raid/ruci_workspace/frwaler_job"
DB = f"{REPO}/libertree-app/data/libertree.db"
OUT_CSV = f"{REPO}/scripts/audit/hal_totals.csv"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

REQ_TIMEOUT = 10
SLEEP = 0.5

# query params that control the UI/pagination, not the result set
UI_PARAMS = {"rows", "sort", "page", "start", "fl", "wt", "index", "submitType"}


def hal_portal(netloc: str) -> str | None:
    """Subdomain before hal.science / archives-ouvertes.fr, or None if bare."""
    parts = netloc.split(".")
    for marker in ("hal", "archives-ouvertes"):
        if marker in parts:
            idx = parts.index(marker)
            if idx > 0:
                return parts[0]
    return None


def build_query(site_url: str):
    """Return (endpoint_url, params_list, portal_label) for a HAL site_url.

    params_list is a list of (key, value) tuples so `fq` can repeat.
    """
    p = urlparse(site_url)
    host = p.netloc.lower()
    portal = hal_portal(host)
    if portal:
        endpoint = f"https://api.archives-ouvertes.fr/search/{portal}/"
        portal_label = portal
    else:
        # bare hal.science / archives-ouvertes.fr -> global endpoint
        endpoint = "https://api.archives-ouvertes.fr/search/"
        portal_label = "(global)"

    qs = parse_qs(p.query, keep_blank_values=False)

    # Collection codes live in the URL path, not the query string:
    #   https://cea.hal.science/CNRGH/search/...   -> collCode_s:CNRGH
    #   https://hal.science/IGN-ENSG/search/...     -> collCode_s:IGN-ENSG
    # (segments before the "search" segment). Without this filter a bare
    # hal.science collection reports the whole global corpus.
    coll_codes = []
    for seg in [s for s in p.path.split("/") if s]:
        if seg.lower() in ("search", "index"):
            break
        coll_codes.append(seg)

    params: list[tuple[str, str]] = []

    # --- q ---
    q_vals = qs.get("q", [])
    q_raw = q_vals[0].strip() if q_vals else ""
    if not q_raw or q_raw == "*":
        q = "*:*"
    elif ":" in q_raw:
        q = q_raw  # already a field query, pass verbatim
    elif q_raw == "*:*":
        q = "*:*"
    else:
        q = q_raw
    params.append(("q", q))

    # --- collection codes from the path ---
    for code in coll_codes:
        params.append(("fq", f"collCode_s:{code}"))

    # --- fq filters from every non-UI param ---
    for key, values in qs.items():
        if key == "q" or key in UI_PARAMS:
            continue
        for val in values:
            val = val.strip()
            if not val:
                continue
            # parse_qs already turned '+' into ' ', so "THESE OR COMM ..."
            if " OR " in val or " AND " in val:
                fq = f"{key}:({val})"
            else:
                fq = f"{key}:{val}"
            params.append(("fq", fq))

    params.append(("wt", "json"))
    params.append(("rows", "0"))
    return endpoint, params, portal_label


def fetch_numfound(sess, endpoint, params):
    last_err = "unknown"
    for attempt in range(2):  # 1 try + 1 retry
        try:
            r = sess.get(endpoint, params=params, timeout=REQ_TIMEOUT, verify=False)
            if r.status_code != 200:
                last_err = f"http_{r.status_code}"
            elif "json" not in r.headers.get("content-type", "").lower():
                # some invalid portal aliases return the API's HTML doc page
                last_err = "non_json"
            else:
                data = r.json()
                nf = data.get("response", {}).get("numFound")
                if isinstance(nf, int):
                    return nf, r.url, None
                last_err = "no_numFound"
        except Exception as exc:  # noqa: BLE001
            last_err = type(exc).__name__
        if attempt == 0:
            time.sleep(SLEEP)
    return None, None, last_err


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT site_id, site_name, site_url, sheet FROM sites "
        "WHERE site_url LIKE '%hal.%' OR site_url LIKE '%archives-ouvertes%' "
        "ORDER BY site_id"
    ).fetchall()
    con.close()

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})

    results = []
    for i, row in enumerate(rows):
        site_id = row["site_id"]
        site_url = row["site_url"] or ""
        sheet = row["sheet"] or ""
        endpoint, params, portal_label = build_query(site_url)

        nf, final_url, err = fetch_numfound(sess, endpoint, params)
        if nf is not None:
            note = "ok"
            filtered_total = nf
            api_url = final_url
        else:
            note = f"error:{err}"
            filtered_total = ""
            # reconstruct a readable api_url even on failure
            api_url = endpoint + "?" + "&".join(f"{k}={v}" for k, v in params)

        results.append({
            "site_id": site_id,
            "sheet": sheet,
            "portal": portal_label,
            "filtered_total": filtered_total,
            "api_url": api_url,
            "note": note,
        })
        print(f"[{i + 1}/{len(rows)}] {site_id}: "
              f"{filtered_total if filtered_total != '' else note} "
              f"(portal={portal_label})")
        if i < len(rows) - 1:
            time.sleep(SLEEP)

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f, fieldnames=["site_id", "sheet", "portal", "filtered_total", "api_url", "note"]
        )
        w.writeheader()
        w.writerows(results)

    # --- summary ---
    ok = [r for r in results if r["note"] == "ok"]
    total = sum(int(r["filtered_total"]) for r in ok)
    print("\n" + "=" * 60)
    print(f"HAL remeasure complete: {len(results)} sites "
          f"({len(ok)} ok, {len(results) - len(ok)} errors)")
    print(f"Sum of filtered_total (ok sites): {total:,}")
    print(f"Output: {OUT_CSV}")
    print("\nTop 10 by filtered_total:")
    top = sorted(ok, key=lambda r: int(r["filtered_total"]), reverse=True)[:10]
    for r in top:
        print(f"  {int(r['filtered_total']):>10,}  {r['site_id']}  (portal={r['portal']})")


if __name__ == "__main__":
    main()

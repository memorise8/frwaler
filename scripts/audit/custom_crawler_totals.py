# -*- coding: utf-8 -*-
"""Source-total probe for the CUSTOM-python-crawler sites left `unknown` by
scripts/audit/coverage_audit.py.

coverage_audit.py only ever probed each site's `base_url` / `site_url`. Many
of our ~605 bespoke crawlers hit a *different* endpoint for their actual
listing (a WordPress custom-post-type route, a CKAN package_search call, an
OAI-PMH endpoint, a JSON search API, ...) that is only visible as a
class-level or module-level constant inside the crawler's own source file
(e.g. `_API_URL`, `LIST_API_URL`, `_SEARCH_URL`, `GROUP_FILTER`).

This script uses the `crawler.sites.CRAWLERS` registry to pull each crawler
class's already-*evaluated* string constants (via the class's own method
`__globals__`, so f-strings like `f"{_BASE}/search"` come back fully
resolved) plus its class-level attributes, builds a short list of candidate
endpoints, and runs the same total-count detector chain as coverage_audit.py
against those endpoints instead of the base_url.

Detector chain per candidate (first confident hit wins):
    wp_total        wp-json/wp/v2/<route>?per_page=1  -> X-WP-Total header
    ckan            .../api/3/action/package_search?rows=0 -> result.count
    oai             ...oai...?verb=ListIdentifiers      -> resumptionToken@completeListSize
    dspace          /server/api/discover/search/objects  -> page.totalElements
    hal_solr/solr   HAL or generic Solr `select?`        -> response.numFound
    json_total      generic JSON GET, deep-search for a total-like key
    html_count_regex  fetch site_url HTML, regex "N results" style (StealthSession)
    html_paginate     bounded binary-search over a discovered page param (last resort, low confidence)

Usage:
    source .venv/bin/activate
    export PYTHONPATH=/data_raid/ruci_workspace/frwaler_job
    python3 scripts/audit/custom_crawler_totals.py [--workers 12] [--timeout 30] [--limit N] [--summary-only]

Outputs (incremental, flushed per site):
    scripts/audit/custom_crawler_totals.csv
    scripts/audit/custom_crawler_summary.md
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sqlite3
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, unquote_plus

from scripts.audit.coverage_audit import (
    _session, _to_int, COUNT_RES, probe_dspace, probe_hal_solr,
    probe_html_count, _wp_total, deadline_ok,
)

REPO = "/data_raid/ruci_workspace/frwaler_job"
DB = f"{REPO}/libertree-app/data/libertree.db"
CUSTOM_DIR = f"{REPO}/crawler/sites/custom"
COVERAGE_CSV = f"{REPO}/scripts/audit/coverage_report.csv"
OUT_CSV = f"{REPO}/scripts/audit/custom_crawler_totals.csv"
OUT_MD = f"{REPO}/scripts/audit/custom_crawler_summary.md"

CSV_FIELDS = ["site_id", "sheet", "collected", "source_total", "method",
              "confidence", "endpoint_used", "note"]

# 7 sites already known to be generic-JSON-config driven (not custom python) —
# handled by a separate workstream, excluded here per task scope.
EXCLUDE_SITE_IDS = {
    "eprr-lanl-gov", "ir-arcnl-nl", "ir-cwi-nl", "krivet-re-kr", "nypi-re-kr",
    "gob-mx-bienestar", "gob-mx-agricultura",
}

REQ_TIMEOUT = 10
DEFAULT_SITE_TIMEOUT = 42

_csv_lock = threading.Lock()
_OAI_NS = {"o": "http://www.openarchives.org/OAI/2.0/"}

NONDATA_HOST_BLOCKLIST = {
    "fonts.googleapis.com", "fonts.gstatic.com", "www.w3.org", "creativecommons.org",
    "schema.org", "googletagmanager.com", "www.googletagmanager.com",
    "google-analytics.com", "www.google-analytics.com", "sentry.io",
    "cdn.jsdelivr.net", "unpkg.com", "recaptcha.net", "www.gstatic.com",
    "youtube.com", "www.youtube.com", "facebook.com", "www.facebook.com",
    "twitter.com", "x.com", "linkedin.com", "www.linkedin.com", "instagram.com",
    "ajax.googleapis.com", "code.jquery.com", "cdnjs.cloudflare.com",
    "use.fontawesome.com", "polyfill.io", "gravatar.com", "secure.gravatar.com",
    "maps.googleapis.com", "www.google.com", "google.com",
}

ASSET_EXT_BLOCKLIST = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".css", ".js", ".ico",
    ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".zip", ".doc", ".docx", ".xml.gz",
)

BASE_ATTR_NAMES = (
    "base_url", "_BASE", "BASE_URL", "_BASE_URL", "BASE", "_ROOT_URL",
    "ROOT_URL", "_SITE_BASE", "SITE_BASE", "_DOMAIN", "DOMAIN", "_HOST", "HOST",
)

GROUP_FILTER_ATTR_NAMES = ("GROUP_FILTER", "_GROUP_FILTER", "CKAN_FQ", "_CKAN_FQ")

# Query-string keys extracted via a plain regex scan of the crawler's raw
# source text — this is how we recover scoping params (e.g. OAI `set=`, CKAN
# `groups=`/`organization=`) that only ever exist as *locals* inside a method
# body (built at call time, e.g. `f"{_BASE}?verb=...&set=publication:x"`) and
# therefore never show up in the module's evaluated __globals__. Losing a
# scoping param like this is dangerous: an unscoped OAI/CKAN endpoint reports
# the WHOLE repository, not just the subset this crawler actually collects
# (seen in practice: 20,695 vs a true per-collection count in the hundreds).
_QPARAM_RE = re.compile(r'[?&]([A-Za-z_][\w\[\]]{0,40})=([^&\'"{}\s#]{1,80})')
# Same as _QPARAM_RE but for the very common `f"...?key={VARNAME}"` shape,
# where the value is an f-string placeholder rather than a literal — e.g.
# `f"{_SEARCH_API}?stiTypeDetails={_STI_TYPE}"`. _STI_TYPE is itself a
# resolvable module-level constant (caught by gather_source_strings), so we
# just need to notice the placeholder and look it up.
_QPARAM_VAR_RE = re.compile(r'[?&]([A-Za-z_][\w\[\]]{0,40})=\{(\w+)\}')
_PAGINATION_KEYS = {
    "page", "p", "pg", "offset", "start", "limit", "rows", "size", "per_page",
    "perpage", "pagesize", "page_size", "resumptiontoken", "cursor", "from",
    "count", "top", "num", "skip", "verb", "metadataprefix",
}


def read_source_text(site_id: str) -> str:
    try:
        with open(f"{CUSTOM_DIR}/{site_id}.py", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def extract_static_query_params(source_text: str, strings: dict) -> dict:
    """First-seen literal `key=value` query fragments found anywhere in the
    raw source (module consts AND locals inside methods), excluding purely
    pagination/paging keys we control ourselves during probing. Also resolves
    the common `key={VARNAME}` f-string-placeholder shape against `strings`
    (module globals / class attrs) when VARNAME is itself a plain string
    constant."""
    # Values are unquoted here (not left as whatever the source literally
    # wrote) because some crawlers pre-URL-encode their constants for manual
    # f-string concatenation (e.g. `_STI_TYPE = "Contractor%20Report%20(CR)"`)
    # while others store the raw literal. Every consumer below re-encodes via
    # urlencode()/requests `params=`, so a pre-encoded value left as-is would
    # get double-encoded into garbage that matches nothing (confirmed in
    # practice: NTRS's stiTypeDetails filter silently became a no-match).
    # unquote_plus is a no-op on values that weren't encoded to begin with.
    out = {}
    for k, v in _QPARAM_RE.findall(source_text):
        lk = k.lower()
        if lk in _PAGINATION_KEYS or lk in out:
            continue
        out[k] = unquote_plus(v)
    for k, varname in _QPARAM_VAR_RE.findall(source_text):
        lk = k.lower()
        if lk in _PAGINATION_KEYS or lk in out:
            continue
        val = strings.get(varname)
        if isinstance(val, str) and val and "{" not in val:
            out[k] = unquote_plus(val)
    return out


def extract_oai_scope(source_text: str):
    set_m = re.search(r'[?&]set=([\w:\-.+%]+)', source_text)
    prefix_m = re.search(r'[?&]metadataPrefix=([\w\-]+)', source_text)
    return (set_m.group(1) if set_m else None,
            prefix_m.group(1) if prefix_m else None)


# ---------------------------------------------------------------------------
# Candidate endpoint discovery — introspect the crawler class/module
# ---------------------------------------------------------------------------

def gather_source_strings(cls) -> dict:
    """Class-level string attrs (own class + all non-object bases) plus the
    fully-evaluated globals of the module the class was defined in (so
    f-strings like `_SEARCH_URL = f"{_BASE}/search"` come back resolved)."""
    out: dict = {}
    if cls is None:
        return out
    for klass in getattr(cls, "__mro__", [cls]):
        if klass.__name__ == "object":
            continue
        for k, v in vars(klass).items():
            if isinstance(v, str) and not k.startswith("__"):
                out.setdefault(k, v)
    mod_globals = None
    for _, val in vars(cls).items():
        if callable(val) and hasattr(val, "__globals__"):
            mod_globals = val.__globals__
            break
    if mod_globals:
        for k, v in mod_globals.items():
            if isinstance(v, str) and not k.startswith("__"):
                out.setdefault(k, v)
    return out


def _priority_for(url: str, attr_name: str) -> int:
    low = url.lower()
    if "wp-json/wp/v2/" in low:
        return 0
    if "/api/3/action" in low:
        return 1
    if "/oai" in low:
        return 2
    if "/server/api/discover" in low or "/rest/items" in low:
        return 3
    if "archives-ouvertes" in low or "solr" in low or "select?" in low:
        return 4
    if any(t in low for t in ("/api/", "wp-json", "json", "search")) or "api" in attr_name.lower():
        return 5
    return 20


def build_candidates(cls, site_url: str, base_url_csv: str):
    strings = gather_source_strings(cls)

    base_vals = set()
    for name in BASE_ATTR_NAMES:
        v = strings.get(name)
        if isinstance(v, str) and v.startswith(("http://", "https://")):
            base_vals.add(v.rstrip("/"))
    cls_base = getattr(cls, "base_url", None)
    if isinstance(cls_base, str) and cls_base.startswith(("http://", "https://")):
        base_vals.add(cls_base.rstrip("/"))
    if base_url_csv:
        base_vals.add(base_url_csv.rstrip("/"))

    group_filter = None
    for name in GROUP_FILTER_ATTR_NAMES:
        if name in strings:
            group_filter = strings[name]
            break
    if group_filter is None:
        # Broader net: any class/module string attr whose name contains "FQ"
        # as a distinct token (e.g. `LIST_FQ`, `SEARCH_FQ`) is, in this
        # codebase, consistently a CKAN `fq` scoping filter — confirmed by
        # hand for search-open-canada-ca-opendata's `LIST_FQ`, which our
        # fixed GROUP_FILTER_ATTR_NAMES list didn't cover and would
        # otherwise have silently returned an unscoped (too-large) CKAN
        # count.
        for k, v in strings.items():
            if isinstance(v, str) and v and "FQ" in re.split(r"[^A-Za-z]+", k.upper()):
                group_filter = v
                break

    scored = []

    def add(url: str, priority: int):
        try:
            netloc = urlparse(url).netloc.lower()
        except Exception:
            return
        if not netloc or netloc in NONDATA_HOST_BLOCKLIST:
            return
        path_only = url.lower().split("?")[0]
        if any(path_only.endswith(ext) for ext in ASSET_EXT_BLOCKLIST):
            return
        scored.append((priority, url))

    for k, v in strings.items():
        if not isinstance(v, str):
            continue
        if v.startswith("/") and len(v) > 1 and base_vals:
            for b in base_vals:
                cand = b + v
                add(cand, _priority_for(cand, k))
        elif v.startswith("http://") or v.startswith("https://"):
            add(v, _priority_for(v, k))

    for b in base_vals:
        add(b, 90)
    if site_url:
        add(site_url, 95)

    scored.sort(key=lambda t: t[0])
    seen = set()
    ordered = []
    for _, u in scored:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    return ordered[:10], group_filter, strings


# ---------------------------------------------------------------------------
# Per-candidate detectors — each takes an explicit endpoint URL and returns
# (total:int|None, method:str|None, note:str|None)
# ---------------------------------------------------------------------------

def probe_wp_explicit(sess, url, deadline):
    if not deadline_ok(deadline):
        return None, None, "deadline"
    try:
        base, rest = url.split("wp-json/wp/v2/", 1)
        base = base.rstrip("/")
        route = rest.split("?")[0].strip("/").split("/")[0]
        if not route:
            return None, None, None
        total = _wp_total(sess, base, route, deadline)
        if total is not None:
            return total, "wp_total", f"route={route}"
    except Exception as e:
        return None, None, f"error:{type(e).__name__}"
    return None, None, None


def probe_ckan_explicit(sess, url, group_filter, static_params, deadline):
    if not deadline_ok(deadline):
        return None, None, "deadline"
    try:
        base = url.split("/api/3/action")[0].rstrip("/")
        target = f"{base}/api/3/action/package_search"
        params = {"rows": 0}
        fq = []
        qs = parse_qs(urlparse(url).query)
        if "groups" in qs:
            fq.append(f"groups:{qs['groups'][0]}")
        if "organization" in qs:
            fq.append(f"organization:{qs['organization'][0]}")
        for k in ("groups", "organization", "tags"):
            if k in static_params and f"{k}:" not in " ".join(fq):
                fq.append(f"{k}:{static_params[k]}")
        if "fq" in static_params:
            fq.append(static_params["fq"])
        if group_filter:
            fq.append(group_filter)
        if fq:
            params["fq"] = " AND ".join(dict.fromkeys(fq))  # dedupe, keep order
        r = sess.get(target, params=params, timeout=REQ_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            count = data.get("result", {}).get("count")
            if isinstance(count, int):
                return count, "ckan", target
    except Exception as e:
        return None, None, f"error:{type(e).__name__}"
    return None, None, None


def probe_oai_explicit(sess, url, oai_set, oai_prefix, deadline):
    if not deadline_ok(deadline):
        return None, None, "deadline"
    try:
        p = urlparse(url)
        endpoint = f"{p.scheme}://{p.netloc}{p.path}"
        qs = parse_qs(p.query)
        params = {"verb": "ListIdentifiers", "metadataPrefix": oai_prefix or "oai_dc"}
        if "set" in qs:
            params["set"] = qs["set"][0]
        elif oai_set:
            params["set"] = oai_set
        r = sess.get(endpoint, params=params, timeout=REQ_TIMEOUT)
        note_url = r.url if hasattr(r, "url") else endpoint
        if r.status_code != 200 or "<OAI-PMH" not in r.text:
            return None, None, None
        root = ET.fromstring(r.text)
        err = root.find(".//o:error", _OAI_NS)
        if err is not None:
            if err.get("code") == "noRecordsFound":
                return 0, "oai_empty", note_url
            return None, None, None
        rt = root.find(".//o:resumptionToken", _OAI_NS)
        if rt is not None:
            if rt.get("completeListSize"):
                return int(rt.get("completeListSize")), "oai_resumption", note_url
            return None, None, None
        ids = root.findall(".//o:header", _OAI_NS)
        if ids:
            return len(ids), "oai_page", note_url
    except Exception as e:
        return None, None, f"error:{type(e).__name__}"
    return None, None, None


_TOTAL_KEYS = {
    "numFound", "total", "totalResults", "totalElements", "totalCount",
    "count", "recordsTotal", "totalHits", "nbHits",
}


def _deep_find_total(obj, depth=0, path=(), found=None):
    if found is None:
        found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "value" and path[-1:] == ("total",) and isinstance(v, int):
                found.append((depth, path + (k,), v))
            elif k in _TOTAL_KEYS and isinstance(v, int):
                found.append((depth, path + (k,), v))
            if isinstance(v, (dict, list)):
                _deep_find_total(v, depth + 1, path + (k,), found)
    elif isinstance(obj, list):
        for v in obj[:5]:
            if isinstance(v, (dict, list)):
                _deep_find_total(v, depth + 1, path, found)
    return found


def _pick_total(found):
    if not found:
        return None, None
    best_depth = min(f[0] for f in found)
    shallow = [f for f in found if f[0] == best_depth]
    shallow.sort(key=lambda t: -t[2])
    d, p, v = shallow[0]
    return v, ".".join(p)


_PAGE_SIZE_PARAMS = {"rows", "size", "per_page", "perpage", "limit", "pagesize",
                      "page_size", "count", "n", "top", "num", "pagesize[]"}


def probe_json_total(sess, url, static_params, deadline):
    if not deadline_ok(deadline):
        return None, None, "deadline"
    try:
        p = urlparse(url)
        qs = parse_qs(p.query)
        for k in list(qs.keys()):
            if k.lower() in _PAGE_SIZE_PARAMS:
                qs[k] = ["1"]
        for k, v in list(static_params.items())[:5]:
            if k not in qs:
                qs[k] = [v]
        new_q = urlencode(qs, doseq=True)
        probe_url = urlunparse(p._replace(query=new_q)) if new_q else url
        r = sess.get(probe_url, timeout=REQ_TIMEOUT, headers={"Accept": "application/json"})
        if r.status_code != 200:
            return None, None, f"http_{r.status_code}"
        text = r.text.strip()
        if not text:
            return None, None, "empty"
        try:
            data = r.json()
        except Exception:
            return None, None, "not_json"
        if not isinstance(data, (dict, list)):
            return None, None, "not_json_container"
        found = _deep_find_total(data)
        val, path = _pick_total(found)
        if val is not None and 0 <= val <= 50_000_000:
            return val, "json_total", f"key={path}"
        return None, None, "no_total_field"
    except Exception as e:
        return None, None, f"error:{type(e).__name__}"


# ---------------------------------------------------------------------------
# HTML pagination fallback (last resort, low confidence)
# ---------------------------------------------------------------------------

_ITEM_CLASS_RE = re.compile(r'class="([^"]{2,60})"')
_PAGE_PARAM_CANDIDATES = ("page", "p", "pg", "pageNum", "pageNo", "offset")


def _fetch_plain(sess, url, deadline):
    if not deadline_ok(deadline):
        return None
    try:
        r = sess.get(url, timeout=REQ_TIMEOUT, allow_redirects=True)
        if r.status_code == 200 and r.text:
            return r.text
    except Exception:
        pass
    return None


def _estimate_item_signature(html):
    classes = _ITEM_CLASS_RE.findall(html)
    if not classes:
        return None, 0
    c = Counter(classes)
    candidates = [(cls_, n) for cls_, n in c.items() if 2 <= n <= 100]
    if not candidates:
        return None, 0
    candidates.sort(key=lambda t: -t[1])
    return candidates[0]


def probe_html_paginate(sess, list_url, deadline):
    if not list_url or not deadline_ok(deadline):
        return None, None, "deadline"
    html1 = _fetch_plain(sess, list_url, deadline)
    if not html1:
        return None, None, "no_html"
    cls_name, per_page = _estimate_item_signature(html1)
    if not cls_name or per_page < 2:
        return None, None, "no_item_signature"

    sep = "&" if "?" in list_url else "?"
    working_param = None
    for param in _PAGE_PARAM_CANDIDATES:
        if not deadline_ok(deadline):
            return None, None, "deadline"
        test_url = f"{list_url}{sep}{param}=2"
        html2 = _fetch_plain(sess, test_url, deadline)
        if not html2 or html2 == html1:
            continue
        if html2.count(f'class="{cls_name}"') > 0:
            working_param = param
            break
    if not working_param:
        return None, None, "no_page_param"

    last_good, hi = 1, None
    for pg in (2, 4, 8, 16, 32, 64, 128):
        if not deadline_ok(deadline):
            break
        test_url = f"{list_url}{sep}{working_param}={pg}"
        html_n = _fetch_plain(sess, test_url, deadline)
        cnt = html_n.count(f'class="{cls_name}"') if html_n else 0
        if cnt > 0:
            last_good = pg
        else:
            hi = pg
            break
    if hi is None:
        return None, None, "no_upper_bound_found"

    while hi - last_good > 1 and deadline_ok(deadline):
        mid = (last_good + hi) // 2
        test_url = f"{list_url}{sep}{working_param}={mid}"
        html_n = _fetch_plain(sess, test_url, deadline)
        cnt = html_n.count(f'class="{cls_name}"') if html_n else 0
        if cnt > 0:
            last_good = mid
        else:
            hi = mid

    final_url = f"{list_url}{sep}{working_param}={last_good}"
    html_last = _fetch_plain(sess, final_url, deadline)
    cnt_last = html_last.count(f'class="{cls_name}"') if html_last else per_page
    total = (last_good - 1) * per_page + cnt_last
    if total <= 0:
        return None, None, "zero_estimate"
    return total, "html_paginate", final_url


# ---------------------------------------------------------------------------
# Per-site orchestration
# ---------------------------------------------------------------------------

def confidence_for(method: str) -> str:
    if not method:
        return ""
    if method in ("ckan", "dspace_rest7", "dspace_legacy_header", "oai_resumption",
                  "oai_page", "oai_empty", "hal_solr", "solr_generic") or method.startswith("wp_total"):
        return "high"
    if method in ("json_total", "html_count_regex"):
        return "medium"
    if method == "html_paginate":
        return "low"
    return "medium"


def dispatch(sess, cand, group_filter, static_params, oai_set, oai_prefix, deadline):
    low = cand.lower()
    if "wp-json/wp/v2/" in low:
        return probe_wp_explicit(sess, cand, deadline)
    if "/api/3/action" in low:
        return probe_ckan_explicit(sess, cand, group_filter, static_params, deadline)
    if "/oai" in low:
        return probe_oai_explicit(sess, cand, oai_set, oai_prefix, deadline)
    if "/server/api/discover" in low or "/rest/items" in low:
        p = urlparse(cand)
        origin = f"{p.scheme}://{p.netloc}"
        return probe_dspace(sess, [origin], deadline)
    if "archives-ouvertes" in low or "solr" in low or "select?" in low:
        return probe_hal_solr(sess, cand, deadline)
    return probe_json_total(sess, cand, static_params, deadline)


# Heuristic methods guess a total out of an arbitrary shape (deep JSON key
# search, "N results" text regex, or a page-count estimate) and have a real
# false-positive rate — verified in practice by two false hits during manual
# spot-checks (a "mini search" widget count, and an unrelated "3" pulled off
# a press-release page). A source_total smaller than what we've *already*
# collected is a strong tell that the wrong field/number was grabbed, so for
# these methods we reject and keep searching rather than trust it. Structured
# API methods (ckan/dspace/oai/wp_total/hal_solr/solr_generic) read a
# well-defined schema field, but a total<collected mismatch can *still*
# happen for a structured method via a unit mismatch rather than a wrong
# field — confirmed by hand: a CKAN `package_search` count (1,052 matching
# *datasets*) came in far under our collected count (5,287 *PDF resources*,
# several per dataset). Either way — wrong field or wrong unit — the number
# isn't a trustworthy "source total", so total<collected now rejects
# uniformly across every method rather than only the heuristic ones.
HEURISTIC_METHODS = {"json_total", "html_count_regex", "html_paginate"}
ALL_METHODS_REJECT_BELOW_COLLECTED = True

# Methods whose scoping we've explicitly verified/repaired (OAI set= is
# threaded through end-to-end) or that read the crawler's own rendered page
# directly (html_paginate) — these are exempt from the "surprisingly large
# ratio" review flag below. Everything else (json_total, dspace, ckan,
# wp_total, html_count_regex) hits an endpoint whose scoping we can only
# partially reconstruct via static source scanning, and a *huge* ratio vs.
# collected is exactly the signature a missed scope filter leaves behind
# (confirmed in practice: an unscoped DSpace search returning the whole
# instance at 900x collected, and an unscoped NASA NTRS search returning
# every document type at 100x collected, both caught by hand-verification).
RATIO_SAFE_METHODS = {"oai_resumption", "oai_page", "oai_empty", "html_paginate"}
RATIO_REVIEW_THRESHOLD = 30


def _ratio_review_note(total, collected, method):
    # No `collected < N` exemption: a small sample doesn't make a huge ratio
    # less suspicious — confirmed by hand on wodc-nl-documenten
    # (collected=2, total=3527, ratio 1763x), where a `query=Jaarverslag`
    # search-term filter lives in a local var our static scan can't trace,
    # so the probe silently hit the *whole* unscoped repository. A high
    # ratio is exactly this failure signature regardless of sample size.
    if method in RATIO_SAFE_METHODS:
        return None
    ratio = total / max(collected, 1)
    if ratio >= RATIO_REVIEW_THRESHOLD:
        return f"ratio={ratio:.0f}x:review_possible_unscoped_endpoint"
    return None


def _accept_or_reject(total, method, collected, cand_label, note, notes):
    """Returns (accepted:bool, confidence_override:str|None, note_with_flag)."""
    if total < collected:
        notes.append(f"{cand_label}:rejected_total<collected({total}<{collected},method={method})")
        return False, None, note
    ratio_note = _ratio_review_note(total, collected, method)
    conf_override = None
    flag = ""
    if ratio_note:
        flag = f" [{ratio_note}]"
        conf_override = "low"
    return True, conf_override, ((note or "") + flag)[:150]


def probe_custom_site(site_id, cls, site_url, base_url_csv, collected, timeout_secs):
    deadline = time.time() + timeout_secs
    sess = _session()
    candidates, group_filter, strings = build_candidates(cls, site_url, base_url_csv)
    if not candidates and site_url:
        candidates = [site_url]

    source_text = read_source_text(site_id)
    static_params = extract_static_query_params(source_text, strings)
    oai_set, oai_prefix = extract_oai_scope(source_text)

    notes = []
    for cand in candidates:
        if not deadline_ok(deadline):
            break
        try:
            total, method, note = dispatch(sess, cand, group_filter, static_params,
                                            oai_set, oai_prefix, deadline)
        except Exception as e:
            total, method, note = None, None, f"probe_error:{type(e).__name__}"
        if total is not None:
            accepted, conf_override, note_out = _accept_or_reject(
                total, method, collected, cand.split('//')[-1][:40], note, notes)
            if accepted:
                return total, method, conf_override or confidence_for(method), cand, note_out
            continue
        if note:
            notes.append(f"{cand.split('//')[-1][:40]}:{note}")

    if deadline_ok(deadline) and site_url:
        try:
            total, method, note = probe_html_count(sess, site_url, deadline)
        except Exception as e:
            total, method, note = None, None, f"probe_error:{type(e).__name__}"
        if total is not None:
            accepted, conf_override, note_out = _accept_or_reject(
                total, method, collected, "html_count_regex", note, notes)
            if accepted:
                return total, method, conf_override or confidence_for(method), site_url, note_out

    if deadline_ok(deadline) and site_url:
        try:
            total, method, note = probe_html_paginate(sess, site_url, deadline)
        except Exception as e:
            total, method, note = None, None, f"probe_error:{type(e).__name__}"
        if total is not None:
            accepted, conf_override, note_out = _accept_or_reject(
                total, method, collected, "html_paginate", note, notes)
            if accepted:
                return total, method, conf_override or confidence_for(method), note or site_url, note_out

    return None, "unknown", "", "", "; ".join(notes[:4])[:200]


# ---------------------------------------------------------------------------
# Target selection + main run
# ---------------------------------------------------------------------------

def load_targets():
    rows = list(csv.DictReader(open(COVERAGE_CSV, encoding="utf-8")))
    targets = []
    for r in rows:
        if r["method"] != "unknown":
            continue
        sid = r["site_id"]
        if sid in EXCLUDE_SITE_IDS:
            continue
        if not os.path.exists(f"{CUSTOM_DIR}/{sid}.py"):
            continue
        targets.append(r)
    return targets


def load_collected():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = conn.cursor()
    collected = dict(cur.execute("SELECT site_id, COUNT(*) FROM documents GROUP BY site_id").fetchall())
    site_urls = dict(cur.execute("SELECT site_id, site_url FROM sites").fetchall())
    conn.close()
    return collected, site_urls


def run(workers: int, timeout_secs: float, limit: int | None):
    from crawler.sites import CRAWLERS  # import once, shared read-only across threads

    targets = load_targets()
    if limit:
        targets = targets[:limit]
    collected_map, site_url_map = load_collected()

    total_n = len(targets)
    print(f"[custom_crawler_totals] {total_n} target sites, {workers} workers, "
          f"{timeout_secs}s/site budget")

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        f.flush()

        def worker(row):
            sid = row["site_id"]
            sheet = row.get("sheet", "")
            site_url = site_url_map.get(sid) or row.get("site_url") or ""
            collected = collected_map.get(sid, 0)
            cls = CRAWLERS.get(sid)
            try:
                total, method, confidence, endpoint, note = probe_custom_site(
                    sid, cls, site_url, getattr(cls, "base_url", "") if cls else "",
                    collected, timeout_secs)
            except Exception as e:
                total, method, confidence, endpoint, note = None, "unknown", "", "", f"fatal:{type(e).__name__}"
            return {
                "site_id": sid, "sheet": sheet, "collected": collected,
                "source_total": (total if total is not None else ""),
                "method": method or "unknown", "confidence": confidence,
                "endpoint_used": endpoint, "note": note,
            }

        done = 0
        newly_measured = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(worker, row): row["site_id"] for row in targets}
            for fut in as_completed(futs):
                out_row = fut.result()
                with _csv_lock:
                    writer.writerow(out_row)
                    f.flush()
                done += 1
                if out_row["source_total"] != "":
                    newly_measured += 1
                if done % 25 == 0 or done == total_n:
                    print(f"[custom_crawler_totals] {done}/{total_n} done, "
                          f"{newly_measured} measured so far "
                          f"(last: {out_row['site_id']} -> {out_row['method']})")

    print(f"[custom_crawler_totals] wrote {OUT_CSV}")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def write_summary():
    if not os.path.exists(OUT_CSV):
        print(f"[custom_crawler_totals] no {OUT_CSV} found; run first")
        return
    rows = list(csv.DictReader(open(OUT_CSV, encoding="utf-8")))

    def as_int(v):
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    for r in rows:
        r["_collected"] = as_int(r["collected"]) or 0
        r["_source_total"] = as_int(r["source_total"])

    total_targets = len(rows)
    measured = [r for r in rows if r["_source_total"] is not None]
    still_unknown = [r for r in rows if r["_source_total"] is None]
    method_counts = Counter(r["method"] for r in measured)

    total_collected = sum(r["_collected"] for r in measured)
    total_source = sum(r["_source_total"] for r in measured)
    overall_pct = round(100.0 * total_collected / total_source, 1) if total_source else None
    shortfall = sum(r["_source_total"] - r["_collected"] for r in measured)

    top30 = sorted(measured, key=lambda r: -(r["_source_total"] - r["_collected"]))[:30]

    lines = []
    lines.append("# Custom Crawler Source-Total Summary")
    lines.append("")
    lines.append(f"Generated from `{os.path.basename(OUT_CSV)}` — {total_targets} custom-crawler "
                 f"sites that coverage_audit.py left `unknown`.")
    lines.append("")
    lines.append("## Results")
    lines.append("")
    lines.append(f"- Targets probed: **{total_targets}**")
    lines.append(f"- Newly measured (source_total found): **{len(measured)}**")
    lines.append(f"- Still unknown: **{len(still_unknown)}**")
    lines.append("")
    lines.append("### By method")
    lines.append("")
    lines.append("| method | sites |")
    lines.append("|---|---:|")
    for m, n in method_counts.most_common():
        lines.append(f"| {m} | {n} |")
    lines.append("")
    lines.append("## Coverage (newly-measured sites)")
    lines.append("")
    lines.append(f"- Collected (sum): **{total_collected:,}**")
    lines.append(f"- Source total (sum): **{total_source:,}**")
    lines.append(f"- Coverage%: **{overall_pct}%**" if overall_pct is not None else "- Coverage%: n/a")
    lines.append(f"- Added shortfall (source_total − collected): **{shortfall:,}**")
    lines.append("")
    lines.append("## Top 30 newly-measured by absolute shortfall")
    lines.append("")
    lines.append("| site_id | collected | source_total | method | confidence | shortfall |")
    lines.append("|---|---:|---:|---|---|---:|")
    for r in top30:
        sf = r["_source_total"] - r["_collected"]
        lines.append(f"| {r['site_id']} | {r['_collected']:,} | {r['_source_total']:,} | "
                     f"{r['method']} | {r['confidence']} | {sf:,} |")
    lines.append("")

    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[custom_crawler_totals] wrote {OUT_MD}")
    print("\n".join(lines[:40]))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--timeout", type=float, default=DEFAULT_SITE_TIMEOUT)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--summary-only", action="store_true")
    args = ap.parse_args()

    if not args.summary_only:
        run(args.workers, args.timeout, args.limit)
    write_summary()


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""Storage-safe count-only capacity harness.

Measures how many documents each UNMEASURED source actually has by running
each site crawler's own list-pagination logic, but with every write path
(DB insert, PDF download, blob save) monkeypatched to a counting no-op.
Zero disk growth, zero DB writes — this only sizes total capacity.

Targets = all site_ids in `sites` MINUS site_ids that already have a numeric
`source_total` in scripts/audit/coverage_report.csv or
scripts/audit/custom_crawler_totals.SNAPSHOT.csv (snapshot preferred; falls
back to custom_crawler_totals.csv when the snapshot doesn't cover a site),
MINUS the HAL-global-scope sites (method == hal_solr with source_total >
1.5M — these are already known/measured as effectively unbounded and are
skipped rather than re-counted).

Safety design
-------------
Every site crawl runs in its own forked subprocess with a HARD external
wall-clock timeout (default 360s). Inside the child, before the crawler
class is even instantiated:

  1. crawler.base_crawler.BaseCrawler._save_paper / _save_paper_v2 /
     _save_document / _save_paper_legacy -> replaced with a counting
     no-op that increments a counter and returns a fake id. No DB write.
  2. crawler.db_libertree.insert_document and crawler.db.upsert_document /
     upsert_paper -> replaced with no-ops (safety net; _save_* above
     already short-circuits before these would ever be reached).
  3. crawler.pdf_downloader.download_pdf_for, crawler.blob_storage.save_pdf
     / save_text, crawler.storage.cleanup_doc_files -> replaced with
     no-ops (safety net; crawl() normally never calls these).
  4. Two extra generic network-layer guards (this codebase has ~17 custom
     crawlers that shell out to `curl`, or use `requests` directly, to pull
     PDF bytes inline for text extraction — outside the pdf_downloader.py /
     blob_storage.py path):
       - subprocess.run() is wrapped: a `curl`/`wget` invocation whose
         target argument matches `*.pdf` is short-circuited to a fake empty
         CompletedProcess instead of actually running.
       - requests.Session.request() is wrapped: a GET to a URL path ending
         in `.pdf` returns a fake empty 200 response instead of hitting
         the network.
     Both guards only touch calls targeting a `.pdf`-looking URL, so normal
     HTML/JSON listing-page fetches (the whole point of this harness) are
     unaffected.
  5. The DB connection handed to the crawler is opened `mode=ro` (read-only)
     against the real libertree.db, so even a crawler that bypasses every
     patch above and issues a raw `conn.execute("INSERT ...")` gets a
     sqlite3 OperationalError instead of writing — defense in depth.
  6. Known page-cap / wall-clock-budget class or module constants (várious
     spellings: MAX_PAGES, _SAFETY_CAP, _WALL_BUDGET_S, ...) are bumped to a
     very large value where they're discoverable as a class/module
     attribute, so a fast site isn't truncated at the crawler's own
     ~200-page safety cap before our external timeout would ever bind.
     Caps implemented as a *local variable* inside crawl() can't be reached
     this way — best effort only, per site.

Each child writes its running count to a small progress file every ~2s, so
if the external per-site timeout fires and the process is killed, the
parent can still recover the last-seen count as a LOWER BOUND.

Usage
-----
    source .venv/bin/activate
    export PYTHONPATH=/data_raid/ruci_workspace/frwaler_job

    # 3-site smoke test (see run() docstring for picking small targets)
    python3 scripts/audit/count_only_harness.py --only site-a site-b site-c \
        --out-csv scripts/audit/out/smoke_totals.csv

    # full run
    python3 scripts/audit/count_only_harness.py --workers 8 --site-timeout 360

Outputs (incremental, flushed per site):
    scripts/audit/count_only_totals.csv
    scripts/audit/count_only_summary.md
"""
from __future__ import annotations

import argparse
import csv
import json
import multiprocessing as mp
import os
import re
import sqlite3
import sys
import time
import traceback
from collections import Counter

REPO = "/data_raid/ruci_workspace/frwaler_job"
DB = f"{REPO}/libertree-app/data/libertree.db"
COVERAGE_CSV = f"{REPO}/scripts/audit/coverage_report.csv"
SNAPSHOT_CSV = f"{REPO}/scripts/audit/custom_crawler_totals.SNAPSHOT.csv"
FALLBACK_CSV = f"{REPO}/scripts/audit/custom_crawler_totals.csv"
OUT_CSV = f"{REPO}/scripts/audit/count_only_totals.csv"
OUT_MD = f"{REPO}/scripts/audit/count_only_summary.md"
OUT_DIR = f"{REPO}/scripts/audit/out"
PROGRESS_DIR = f"{OUT_DIR}/count_only_progress"
LOG_DIR = f"{OUT_DIR}/count_only_logs"

CSV_FIELDS = ["site_id", "sheet", "collected", "counted", "server_total", "completed",
              "elapsed_s", "method", "note", "health"]

# Server-reported total-count keys seen in list-API JSON responses. When a
# crawler paginates a JSON API, its response often carries the FULL result-set
# size (e.g. data.busan -> result.total_count=12421). We capture it for free by
# scanning every json.loads() result, so a locally-capped crawler still yields
# the true total. Strong-signal names only (avoid per-page "count").
_TOTAL_KEY_RE = re.compile(
    r"^(total_?count|totalcount|total_?records?|total_?results?|total_?hits|"
    r"total_?elements|num_?found|records_?total|recordstotal|nb_?hits|"
    r"tot_?cnt|totcnt|total)$", re.I)


def _scan_server_total(obj, depth=0):
    """Recursively find the largest plausible total-count int in a parsed
    JSON object. Returns 0 if none."""
    best = 0
    if depth > 8:
        return best
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and _TOTAL_KEY_RE.match(str(k)):
                iv = int(v)
                if 0 <= iv < 1_000_000_000:
                    best = max(best, iv)
            elif isinstance(v, (dict, list)):
                best = max(best, _scan_server_total(v, depth + 1))
    elif isinstance(obj, list):
        for it in obj[:50]:
            if isinstance(it, (dict, list)):
                best = max(best, _scan_server_total(it, depth + 1))
    return best

HAL_GLOBAL_METHOD = "hal_solr"
HAL_GLOBAL_THRESHOLD = 1_500_000

DEFAULT_SITE_TIMEOUT_S = 360.0   # external hard per-site cap (6 min)
DEFAULT_CRAWL_DELAY = 0.5
DEFAULT_WORKERS = 8
DEFAULT_PROBE_LIMIT = 3
GRACE_KILL_S = 5.0
POLL_INTERVAL_S = 1.0
PROGRESS_FLUSH_S = 2.0

_PAGE_CAP_NAMES = (
    "MAX_PAGES", "_MAX_PAGES", "PAGE_CAP", "_PAGE_CAP",
    "PAGE_SAFETY_CAP", "_PAGE_SAFETY_CAP", "SAFETY_CAP", "_SAFETY_CAP",
    "SAFETY_PAGE_CAP", "_SAFETY_PAGE_CAP",
)
_WALL_BUDGET_NAMES = (
    "MAX_SECONDS", "_MAX_SECONDS", "MAX_WALL", "_MAX_WALL",
    "WALL_BUDGET", "_WALL_BUDGET", "WALL_BUDGET_S", "_WALL_BUDGET_S",
    "TIME_BUDGET", "_TIME_BUDGET", "TIME_BUDGET_S", "_TIME_BUDGET_S",
)
_BIG_PAGE_CAP = 10_000_000
_BIG_WALL_BUDGET_S = 10_000_000


# ---------------------------------------------------------------------------
# Target selection
# ---------------------------------------------------------------------------

def _merge_measured(path, measured):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sid = row.get("site_id")
            st = (row.get("source_total") or "").strip()
            if not sid or st == "":
                continue
            try:
                v = float(st)
            except ValueError:
                continue
            measured[sid] = (v, row.get("method", ""))


def load_targets():
    """Returns (targets: list[dict], measured: dict[site_id -> (total, method)])."""
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = conn.cursor()
    all_sites = cur.execute("SELECT site_id, sheet FROM sites").fetchall()
    collected_map = dict(
        cur.execute("SELECT site_id, COUNT(*) FROM documents GROUP BY site_id").fetchall()
    )
    conn.close()

    # coverage_report.csv first, then SNAPSHOT overwrites (snapshot preferred),
    # then fall back to the live custom_crawler_totals.csv for anything the
    # snapshot doesn't cover.
    measured = {}
    _merge_measured(COVERAGE_CSV, measured)
    _merge_measured(FALLBACK_CSV, measured)
    _merge_measured(SNAPSHOT_CSV, measured)

    already = set(measured.keys())
    hal_global = {sid for sid, (v, m) in measured.items()
                  if m == HAL_GLOBAL_METHOD and v > HAL_GLOBAL_THRESHOLD}
    already |= hal_global

    targets = []
    for site_id, sheet in all_sites:
        if site_id in already:
            continue
        targets.append({
            "site_id": site_id,
            "sheet": sheet or "",
            "collected": collected_map.get(site_id, 0),
        })
    targets.sort(key=lambda r: r["site_id"])
    return targets, measured


# ---------------------------------------------------------------------------
# Child-process write guards (applied inside the fork, before crawl())
# ---------------------------------------------------------------------------

def _write_progress(path, n, server_total=0):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"count": n, "server_total": server_total, "ts": time.time()}, f)
    os.replace(tmp, path)


def apply_write_guards(progress_path):
    """Monkeypatch every known write/download entry point. Returns the
    live counter dict; counter["saves"] is incremented on every counted
    save call."""
    from crawler import base_crawler as _base
    from crawler import db_libertree as _ldb
    from crawler import db as _db
    from crawler import pdf_downloader as _pdfdl
    from crawler import blob_storage as _blobs
    from crawler import storage as _storage

    counter = {"saves": 0, "server_total": 0}
    state = {"last_flush": 0.0}

    # Capture server-reported totals from any JSON the crawler parses.
    import json as _json
    _orig_loads = _json.loads

    def _loads_capture(*a, **k):
        obj = _orig_loads(*a, **k)
        try:
            t = _scan_server_total(obj)
            if t > counter["server_total"]:
                counter["server_total"] = t
        except Exception:
            pass
        return obj
    _json.loads = _loads_capture

    def _tick():
        counter["saves"] += 1
        now = time.time()
        if now - state["last_flush"] > PROGRESS_FLUSH_S:
            state["last_flush"] = now
            try:
                _write_progress(progress_path, counter["saves"], counter.get("server_total", 0))
            except OSError:
                pass
        return counter["saves"]

    def _count_and_fake_id(*_a, **_k):
        return _tick()

    _base.BaseCrawler._save_paper = _count_and_fake_id
    _base.BaseCrawler._save_paper_v2 = _count_and_fake_id
    _base.BaseCrawler._save_document = _count_and_fake_id
    _base.BaseCrawler._save_paper_legacy = _count_and_fake_id

    _ldb.insert_document = lambda conn, doc: _count_and_fake_id()
    _db.upsert_document = lambda *a, **k: _count_and_fake_id()
    _db.upsert_paper = lambda *a, **k: _count_and_fake_id()

    def _noop_download(*_a, **_k):
        return {"success": False, "size_bytes": 0, "sha256": None,
                "path": None, "error": "count_only_harness: downloads disabled"}
    _pdfdl.download_pdf_for = _noop_download

    def _noop_save_pdf(*_a, **_k):
        raise RuntimeError("count_only_harness: blob writes disabled")
    _blobs.save_pdf = _noop_save_pdf
    _blobs.save_text = lambda *a, **k: None
    _storage.cleanup_doc_files = lambda *a, **k: 0

    _block_pdf_network_calls()

    return counter


def _block_pdf_network_calls():
    """Extra safety net: some hand-written custom crawlers pull PDF bytes
    inline (via `curl` subprocess or `requests`) for text extraction,
    outside the pdf_downloader.py/blob_storage.py path. Short-circuit any
    such call that targets a `.pdf`-looking URL; leave every other request
    (the HTML/JSON listing pages we actually need to walk) untouched."""
    import subprocess as _sp
    import requests as _rq

    _orig_run = _sp.run

    def _guarded_run(cmd, *args, **kwargs):
        try:
            if isinstance(cmd, (list, tuple)) and cmd:
                argv0 = str(cmd[0]).lower()
                if "curl" in argv0 or "wget" in argv0:
                    for c in cmd[1:]:
                        if isinstance(c, str) and c.lower().startswith(("http://", "https://")) \
                                and ".pdf" in c.lower().split("?")[0]:
                            class _FakeCompleted:
                                returncode = 0
                                stdout = b""
                                stderr = b""
                            return _FakeCompleted()
        except Exception:
            pass
        return _orig_run(cmd, *args, **kwargs)

    _sp.run = _guarded_run

    _orig_request = _rq.Session.request

    def _guarded_request(self, method, url, *args, **kwargs):
        try:
            if isinstance(method, str) and method.upper() == "GET" and isinstance(url, str):
                path = url.split("?")[0].split("#")[0]
                if path.lower().endswith(".pdf"):
                    resp = _rq.models.Response()
                    resp.status_code = 200
                    resp._content = b""
                    resp.url = url
                    resp.reason = "OK"
                    resp.headers["Content-Length"] = "0"
                    return resp
        except Exception:
            pass
        return _orig_request(self, method, url, *args, **kwargs)

    _rq.Session.request = _guarded_request


# Regex patterns (name-based) — catch varied cap constant names across the
# many hand-written crawlers, e.g. _MAX_PAGES / PAGE_SAFETY_CAP and
# _MAX_WALL_SECONDS / _WALL_CLOCK_BUDGET_S / _WALL_SECONDS / TIME_BUDGET_S.
_PAGE_CAP_RE = re.compile(r"(MAX_?PAGES?|PAGE_?CAP|SAFETY_?CAP|PAGE_?LIMIT|MAX_?ITEMS?|MAX_?RECORDS?|MAX_?RESULTS?)", re.I)
_WALL_CAP_RE = re.compile(r"(MAX_?WALL|WALL_?BUDGET|WALL_?CLOCK|WALL_?SEC|TIME_?BUDGET|MAX_?SEC|BUDGET_?S\b|DEADLINE|MAX_?MIN)", re.I)
# Never touch these — per-page counts, delays, retries, connect timeouts, UA, etc.
_CAP_EXCLUDE_RE = re.compile(r"(SIZE|PER_?PAGE|DELAY|RETRY|SLEEP|BACKOFF|CONNECT|USER_?AGENT|\bUA\b|VERSION|ENCODING)", re.I)


def neutralize_caps(cls):
    """Bump ANY class/module-level page-cap or wall-clock-budget constant
    (matched by NAME regex, not a fixed list) to a huge value, so a crawler
    isn't truncated by its own safety cap / 25-min budget before our external
    per-site timeout binds. Caps that are LOCAL variables inside crawl()
    remain unreachable (documented limitation). Returns "name=old->big" notes."""
    touched = []
    # Object targets: the class itself + its module (if importable via sys.modules).
    obj_targets = [cls]
    mod = sys.modules.get(getattr(cls, "__module__", None))
    if mod is not None:
        obj_targets.append(mod)
    # Dict target: the module namespace reached via a bound method's __globals__.
    # Custom crawlers loaded via importlib often aren't in sys.modules, so this
    # is the reliable way to reach MODULE-level cap constants (e.g. app-mps).
    dict_targets = []
    for meth in ("crawl", "_fetch_list", "_list_url"):
        fn = getattr(cls, meth, None)
        g = getattr(fn, "__globals__", None)
        if isinstance(g, dict) and g not in dict_targets:
            dict_targets.append(g)

    def _consider(name, old, setter):
        if _CAP_EXCLUDE_RE.search(name):
            return
        if isinstance(old, bool) or not isinstance(old, (int, float)):
            return
        if _PAGE_CAP_RE.search(name) and 10 <= old < _BIG_PAGE_CAP:
            try:
                setter(_BIG_PAGE_CAP); touched.append(f"{name}={old}->big")
            except Exception:
                pass
        elif _WALL_CAP_RE.search(name) and 10 <= old < _BIG_WALL_BUDGET_S:
            try:
                setter(_BIG_WALL_BUDGET_S); touched.append(f"{name}={old}->big")
            except Exception:
                pass

    for tgt in obj_targets:
        try:
            names = list(vars(tgt).keys())
        except TypeError:
            names = [n for n in dir(tgt) if not n.startswith("__")]
        for name in names:
            try:
                old = getattr(tgt, name)
            except Exception:
                continue
            _consider(name, old, lambda v, _t=tgt, _n=name: setattr(_t, _n, v))
    for g in dict_targets:
        for name in list(g.keys()):
            _consider(name, g.get(name), lambda v, _g=g, _n=name: _g.__setitem__(_n, v))
    return touched


# ---------------------------------------------------------------------------
# Health classification (used by both the child, on normal/error completion,
# and the parent, for the external-timeout-kill path in _collect_result)
# ---------------------------------------------------------------------------

def _classify_health(method, completed, counted):
    """Classify a single result row into a coarse pass/fail bucket for
    --probe health-check runs (also computed, harmlessly, for normal
    count-only runs)."""
    if isinstance(method, str) and method.startswith("error:"):
        return "broken_error"
    if method == "no_crawler":
        return "no_crawler"
    if not completed:
        return "timeout"
    if (counted or 0) >= 1:
        return "ok"
    return "empty_zero_parse"


# ---------------------------------------------------------------------------
# Child process entry point
# ---------------------------------------------------------------------------

def _child_main(site_id, delay, progress_path, log_path, result_path, probe=None):
    start = time.time()
    result = {"counted": 0, "server_total": 0, "completed": False, "method": "count_crawl", "note": ""}
    counter = {"saves": 0, "server_total": 0}
    log_f = None
    old_out, old_err = sys.stdout, sys.stderr
    try:
        log_f = open(log_path, "w", encoding="utf-8")
        sys.stdout = log_f
        sys.stderr = log_f
        try:
            counter = apply_write_guards(progress_path)
            from crawler.sites import CRAWLERS
            cls = CRAWLERS.get(site_id)
            if cls is None:
                result.update(counted=0, completed=True, method="no_crawler", note="")
            else:
                conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
                inst = cls(db_conn=conn, delay=delay)
                if probe is not None:
                    # Health-probe mode: test the crawler as-is (caps left
                    # intact) with a small item limit — we want to know
                    # whether it still fetches+parses successfully right
                    # now, not size its full capacity.
                    inst.crawl(limit=probe)
                    result.update(
                        counted=counter["saves"], server_total=counter.get("server_total", 0),
                        completed=True, method="count_crawl",
                        note=f"probe_limit:{probe}",
                    )
                else:
                    cap_notes = neutralize_caps(cls)
                    inst.crawl(limit=None)
                    result.update(
                        counted=counter["saves"], server_total=counter.get("server_total", 0),
                        completed=True, method="count_crawl",
                        note=("caps_neutralized:" + ";".join(cap_notes))[:200] if cap_notes else "",
                    )
        finally:
            sys.stdout, sys.stderr = old_out, old_err
    except Exception as e:
        sys.stdout, sys.stderr = old_out, old_err
        result.update(
            counted=counter.get("saves", 0), server_total=counter.get("server_total", 0),
            completed=False, method=f"error:{type(e).__name__}", note=str(e)[:200],
        )
        try:
            if log_f:
                traceback.print_exc(file=log_f)
        except Exception:
            pass
    finally:
        result["elapsed_s"] = round(time.time() - start, 1)
        result["health"] = _classify_health(result.get("method"), result.get("completed"), result.get("counted"))
        try:
            _write_progress(progress_path, result["counted"], result.get("server_total", 0))
        except Exception:
            pass
        try:
            with open(result_path, "w", encoding="utf-8") as f:
                json.dump(result, f)
        except Exception:
            pass
        if log_f:
            try:
                log_f.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Parent orchestration
# ---------------------------------------------------------------------------

def _collect_result(sid, row, info):
    result_path = info["result_path"]
    progress_path = info["progress_path"]
    elapsed = round(time.time() - info["start"], 1)
    if os.path.exists(result_path):
        try:
            with open(result_path, encoding="utf-8") as f:
                r = json.load(f)
            method = r.get("method", "count_crawl")
            completed = r.get("completed", False)
            counted = r.get("counted", 0)
            return {
                "site_id": sid, "sheet": row.get("sheet", ""), "collected": row.get("collected", 0),
                "counted": counted, "server_total": r.get("server_total", 0),
                "completed": completed,
                "elapsed_s": r.get("elapsed_s", elapsed), "method": method,
                "note": r.get("note", ""),
                "health": r.get("health") or _classify_health(method, completed, counted),
            }
        except Exception:
            pass
    counted = 0
    if os.path.exists(progress_path):
        try:
            with open(progress_path, encoding="utf-8") as f:
                counted = json.load(f).get("count", 0)
        except Exception:
            pass
    server_total = 0
    if os.path.exists(progress_path):
        try:
            with open(progress_path, encoding="utf-8") as f:
                server_total = json.load(f).get("server_total", 0)
        except Exception:
            pass
    return {
        "site_id": sid, "sheet": row.get("sheet", ""), "collected": row.get("collected", 0),
        "counted": counted, "server_total": server_total, "completed": False,
        "elapsed_s": elapsed, "method": "count_crawl",
        "note": "external_timeout_kill:lower_bound",
        "health": _classify_health("count_crawl", False, counted),
    }


def run_all(targets, workers, site_timeout, delay, out_csv, probe=None):
    os.makedirs(PROGRESS_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    ctx = mp.get_context("fork")

    pending = list(targets)
    running = {}
    total = len(targets)
    done = 0

    with open(out_csv, "a", newline="", encoding="utf-8") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=CSV_FIELDS)

        while pending or running:
            while pending and len(running) < workers:
                row = pending.pop(0)
                sid = row["site_id"]
                progress_path = f"{PROGRESS_DIR}/{sid}.json"
                log_path = f"{LOG_DIR}/{sid}.log"
                result_path = f"{PROGRESS_DIR}/{sid}.result.json"
                for p in (progress_path, result_path):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                proc = ctx.Process(target=_child_main, args=(sid, delay, progress_path, log_path, result_path, probe))
                proc.start()
                running[sid] = {
                    "proc": proc, "start": time.time(), "progress_path": progress_path,
                    "result_path": result_path, "log_path": log_path, "row": row,
                }

            time.sleep(POLL_INTERVAL_S)

            finished_ids = []
            for sid, info in list(running.items()):
                proc = info["proc"]
                elapsed = time.time() - info["start"]
                if not proc.is_alive():
                    proc.join(timeout=5)
                    finished_ids.append(sid)
                elif elapsed > site_timeout:
                    print(f"[count_only] {sid} exceeded {site_timeout}s, terminating", flush=True)
                    proc.terminate()
                    proc.join(timeout=GRACE_KILL_S)
                    if proc.is_alive():
                        proc.kill()
                        proc.join(timeout=10)
                    finished_ids.append(sid)

            for sid in finished_ids:
                info = running.pop(sid)
                row = info["row"]
                out_row = _collect_result(sid, row, info)
                writer.writerow(out_row)
                out_f.flush()
                done += 1
                if done % 10 == 0 or done == total:
                    print(f"[count_only] {done}/{total} done "
                          f"(last={sid} -> counted={out_row['counted']} "
                          f"completed={out_row['completed']} method={out_row['method']})", flush=True)

    print(f"[count_only] wrote {out_csv}")


def write_no_crawler_rows(rows, out_csv, mode="w"):
    write_header = mode == "w" or not os.path.exists(out_csv)
    with open(out_csv, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if write_header:
            writer.writeheader()
        for row in rows:
            writer.writerow({
                "site_id": row["site_id"], "sheet": row["sheet"], "collected": row["collected"],
                "counted": 0, "completed": True, "elapsed_s": 0, "method": "no_crawler", "note": "",
                "health": _classify_health("no_crawler", True, 0),
            })
        f.flush()


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def write_summary(out_csv=OUT_CSV, out_md=OUT_MD):
    if not os.path.exists(out_csv):
        print(f"[count_only] no {out_csv} found; run first")
        return
    rows = list(csv.DictReader(open(out_csv, encoding="utf-8")))

    def as_num(v):
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return 0

    for r in rows:
        r["_counted"] = as_num(r.get("counted"))
        r["_collected"] = as_num(r.get("collected"))
        r["_completed"] = r.get("completed") in ("True", "true", "1", True)

    total_targets = len(rows)
    count_crawl_rows = [r for r in rows if r["method"] == "count_crawl"]
    completed_rows = [r for r in count_crawl_rows if r["_completed"]]
    capped_rows = [r for r in count_crawl_rows if not r["_completed"]]
    error_rows = [r for r in rows if r["method"].startswith("error:")]
    no_crawler_rows = [r for r in rows if r["method"] == "no_crawler"]

    total_counted = sum(r["_counted"] for r in rows)

    _, measured = load_targets()
    baseline_n = len(measured)
    baseline_total = sum(v for v, _m in measured.values())

    combined_estimate = baseline_total + total_counted

    method_counts = Counter(r["method"] for r in rows)

    top30 = sorted(rows, key=lambda r: -r["_counted"])[:30]

    lines = []
    lines.append("# Count-Only Capacity Summary")
    lines.append("")
    lines.append(f"Generated from `{os.path.basename(out_csv)}` — {total_targets} previously-unmeasured "
                 f"sites (all `sites` rows minus those with a numeric source_total already in "
                 f"coverage_report.csv/custom_crawler_totals(.SNAPSHOT).csv, minus HAL-global-scope "
                 f"hal_solr sites >1.5M).")
    lines.append("")
    lines.append("## Results")
    lines.append("")
    lines.append(f"- Targets: **{total_targets}**")
    lines.append(f"- Completed (exact count, crawl finished before the external cap): **{len(completed_rows)}**")
    lines.append(f"- Capped (external timeout hit — count is a LOWER BOUND): **{len(capped_rows)}**")
    lines.append(f"- Errors: **{len(error_rows)}**")
    lines.append(f"- No crawler registered: **{len(no_crawler_rows)}**")
    lines.append("")
    lines.append("### By method")
    lines.append("")
    lines.append("| method | sites |")
    lines.append("|---|---:|")
    for m, n in method_counts.most_common():
        lines.append(f"| {m} | {n} |")
    lines.append("")
    lines.append("## Capacity")
    lines.append("")
    lines.append(f"- This pass's counted total (sum of `counted`, exact + lower-bound rows): **{total_counted:,}**")
    lines.append(f"  - NOTE: rows with `completed=False` (capped, {len(capped_rows)} sites) are LOWER BOUNDS, "
                 f"so this sum understates true capacity for those sites.")
    lines.append(f"- Already-measured baseline (union of coverage_report.csv + custom_crawler_totals(.SNAPSHOT).csv, "
                 f"{baseline_n} sites): **{baseline_total:,.0f}**")
    lines.append(f"- **Combined capacity estimate: {combined_estimate:,.0f}**")
    lines.append("")
    lines.append("## Top 30 newly-counted sites")
    lines.append("")
    lines.append("| site_id | collected | counted | completed | method |")
    lines.append("|---|---:|---:|---|---|")
    for r in top30:
        lines.append(f"| {r['site_id']} | {r['_collected']:,} | {r['_counted']:,} | "
                     f"{r['_completed']} | {r['method']} |")
    lines.append("")

    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[count_only] wrote {out_md}")
    print("\n".join(lines[:40]))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--site-timeout", type=float, default=DEFAULT_SITE_TIMEOUT_S)
    ap.add_argument("--delay", type=float, default=DEFAULT_CRAWL_DELAY)
    ap.add_argument("--limit", type=int, default=None, help="cap number of runnable targets (after --only filter)")
    ap.add_argument("--only", nargs="*", default=None, help="restrict to these site_ids (smoke test)")
    ap.add_argument("--probe", type=int, default=None,
                     help="health-probe mode: run each crawler with crawl(limit=N) and DO NOT neutralize "
                          "caps; classifies whether the crawler still works")
    ap.add_argument("--out-csv", default=OUT_CSV)
    ap.add_argument("--out-md", default=OUT_MD)
    ap.add_argument("--summary-only", action="store_true")
    args = ap.parse_args()

    if args.summary_only:
        write_summary(args.out_csv, args.out_md)
        return

    targets, _measured = load_targets()
    print(f"[count_only] {len(targets)} target sites (unmeasured)")

    from crawler.sites import CRAWLERS  # warm import once; forked children inherit it
    print(f"[count_only] crawler registry has {len(CRAWLERS)} entries")

    runnable = [r for r in targets if r["site_id"] in CRAWLERS]
    no_crawler_rows = [r for r in targets if r["site_id"] not in CRAWLERS]

    if args.only:
        # --only 는 measured(이미 값 있는) 사이트도 포함해 강제 스캔할 수 있어야 한다
        # (예: DOAJ가 OAI로 35k 잘못측정 → server_total로 진짜 13.3M 확인).
        # 그래서 미측정 targets 필터를 우회하고 --only id 로 직접 구성한다.
        wanted = list(dict.fromkeys(args.only))
        _conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        _sheets = dict(_conn.execute("SELECT site_id, sheet FROM sites").fetchall())
        _coll = dict(_conn.execute("SELECT site_id, COUNT(*) FROM documents GROUP BY site_id").fetchall())
        _conn.close()
        runnable = [{"site_id": sid, "sheet": _sheets.get(sid, ""), "collected": _coll.get(sid, 0)}
                    for sid in wanted if sid in CRAWLERS]
        no_crawler_rows = [{"site_id": sid, "sheet": _sheets.get(sid, ""), "collected": _coll.get(sid, 0),
                            "method": "no_crawler"}
                           for sid in wanted if sid not in CRAWLERS]

    if args.limit:
        runnable = runnable[:args.limit]

    print(f"[count_only] runnable={len(runnable)} no_crawler={len(no_crawler_rows)}")

    write_no_crawler_rows(no_crawler_rows, args.out_csv, mode="w")
    run_all(runnable, args.workers, args.site_timeout, args.delay, args.out_csv, probe=args.probe)
    write_summary(args.out_csv, args.out_md)


if __name__ == "__main__":
    main()

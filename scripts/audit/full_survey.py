# -*- coding: utf-8 -*-
"""Reliable ~1-day FULL capacity survey — ALL sites, stealth, resume, observable.

Builds directly on `scripts/audit/count_only_harness.py` (same directory,
imported as a module below): reuses its zero-disk-growth write guards
(`apply_write_guards`, `neutralize_caps`, `_write_progress`) UNCHANGED — every
DB insert / PDF download / blob save path is still a counting no-op, and the
DB connection handed to each crawler is still opened `mode=ro`.

What this script adds on top:

  1. Target = ALL site_ids in `sites` (789), not just previously-unmeasured
     ones — a complete capacity picture, not a delta.
  2. `--resume`: the output CSV is append-only (flushed per site). On
     `--resume`, site_ids already present as a row are skipped. Rows whose
     `outcome` is retryable (`fetch_fail`, `site_timeout`, `error`) are
     re-run instead of skipped when `--retry-failed` is also passed. This
     lets a killed run (or a watchdog-restarted one) continue where it left
     off without re-fetching completed sites.
  3. Stealth fetch injection: before each site crawler runs, the child
     process patches (in addition to count_only_harness's write guards):
       - `crawler.base_crawler.BaseCrawler._request` — GET calls try
         `crawler.stealth_fetcher.StealthSession.fetch_html()` first (its own
         fast curl_cffi path, browser fallback only on block), then fall back
         to the crawler's original request path unchanged.
       - `requests.Session.request` — same GET-first-try-stealth treatment
         for any custom crawler that calls `requests` directly instead of
         through `BaseCrawler._request`.
       - `subprocess.run` — best-effort: a `curl ... <http(s) url>` GET
         invocation (no `-d`/`--data`/`-X POST`-style flags) is routed
         through StealthSession too, covering the ~30 custom crawlers with
         module-level `_curl_get`/`_curl_json` helpers that shell out to
         curl directly. Non-GET curl calls, and any call that Stealth can't
         clear, fall through to the crawler's own curl invocation unchanged
         (those already carry a realistic UA + retry/backoff per file).
     Every fallback path is best-effort and always safe: on stealth failure
     the crawler's original, working fetch logic still runs exactly as
     before this script existed.
  4. Slow & polite defaults: `--workers 3`, `--delay 2.0`, `--site-timeout
     2400` (40 min) so large sites have room to finish; sites still hitting
     the cap record a lower-bound `completed=False` row.
  5. Heavy observability:
       - Heartbeat log `out/full_survey_heartbeat.log`: one line every ~30s
         AND on every site completion — done/total, per-outcome counts,
         in-flight site_ids with elapsed seconds, throughput, ETA.
       - Status JSON `out/full_survey_status.json`: atomically rewritten on
         the same cadence, meant for an external watchdog to poll.
       - Per-site logs kept under `out/full_survey_logs/<site_id>.log`
         (stdout+stderr captured, inspected afterwards for outcome
         classification).
       - SIGTERM/SIGINT: stop accepting new sites, terminate in-flight
         children, flush CSV/status/heartbeat, exit cleanly. In-flight sites
         with no row written are naturally picked up by the next
         `--resume` run.
  6. Per-row outcome classification (site fully blocked/errored vs. ran fine
     but genuinely parsed 0 vs. hit the external timeout): `ok`,
     `zero_parsed`, `fetch_fail`, `site_timeout`, `no_crawler`, `error`.

Usage
-----
    source .venv/bin/activate
    export PYTHONPATH=/data_raid/ruci_workspace/frwaler_job

    # smoke test (5 sites, dedicated output files)
    python3 scripts/audit/full_survey.py --only site-a site-b \
        --out-csv scripts/audit/out/smoke_full_survey.csv \
        --heartbeat-log scripts/audit/out/smoke_full_survey_heartbeat.log \
        --status-json scripts/audit/out/smoke_full_survey_status.json

    # full resumable run (background; survives crashes/restarts)
    nohup python3 scripts/audit/full_survey.py --resume \
        > scripts/audit/out/full_survey_run.log 2>&1 &

Outputs (default paths, all incremental / flushed per site):
    scripts/audit/full_survey_totals.csv
    scripts/audit/full_survey_summary.md
    scripts/audit/out/full_survey_heartbeat.log
    scripts/audit/out/full_survey_status.json
    scripts/audit/out/full_survey_logs/<site_id>.log
"""
from __future__ import annotations

import argparse
import csv
import json
import multiprocessing as mp
import os
import signal
import sqlite3
import sys
import time
import traceback
from collections import Counter
from urllib.parse import urlencode

REPO = "/data_raid/ruci_workspace/frwaler_job"
DB = f"{REPO}/libertree-app/data/libertree.db"
ENV_PATH = f"{REPO}/crawler/.env"

# Reuse count_only_harness's write guards / cap-neutralization unchanged.
sys.path.insert(0, os.path.dirname(__file__))
import count_only_harness as coh  # noqa: E402

OUT_CSV = f"{REPO}/scripts/audit/full_survey_totals.csv"
OUT_MD = f"{REPO}/scripts/audit/full_survey_summary.md"
OUT_DIR = f"{REPO}/scripts/audit/out"
PROGRESS_DIR = f"{OUT_DIR}/full_survey_progress"
LOG_DIR = f"{OUT_DIR}/full_survey_logs"
HEARTBEAT_LOG = f"{OUT_DIR}/full_survey_heartbeat.log"
STATUS_JSON = f"{OUT_DIR}/full_survey_status.json"

CSV_FIELDS = ["site_id", "sheet", "collected", "counted", "completed",
              "elapsed_s", "method", "outcome", "note"]

DEFAULT_SITE_TIMEOUT_S = 2400.0  # 40 min — generous, big sites need room
DEFAULT_CRAWL_DELAY = 2.0        # polite per-request delay inside crawlers
DEFAULT_WORKERS = 3              # slow & polite, avoid re-triggering anti-bot
GRACE_KILL_S = 5.0
POLL_INTERVAL_S = 1.0
HEARTBEAT_INTERVAL_S = 30.0

RETRYABLE_OUTCOMES = {"fetch_fail", "site_timeout", "error"}

_FETCH_FAIL_KEYWORDS = (
    "403", "forbidden", "cloudflare", "cloudflare_challenge", "challenge",
    "checking your browser", "just a moment", "rate limit", "429",
    "timed out", "timeout", "connection reset", "connection refused",
    "ssl", "certificate verify", "curl: (", "expecting value",
    "could not extract", "network is unreachable", "dns", "name resolution",
    "getaddrinfo", "name or service not known", "econnreset", "http error",
)

_stop_requested = False


def _handle_stop_signal(signum, _frame):
    global _stop_requested
    _stop_requested = True


# ---------------------------------------------------------------------------
# Target selection — ALL sites (not just unmeasured)
# ---------------------------------------------------------------------------

def load_all_targets():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = conn.cursor()
    all_sites = cur.execute("SELECT site_id, sheet FROM sites").fetchall()
    collected_map = dict(
        cur.execute("SELECT site_id, COUNT(*) FROM documents GROUP BY site_id").fetchall()
    )
    conn.close()
    targets = [
        {"site_id": sid, "sheet": sheet or "", "collected": collected_map.get(sid, 0)}
        for sid, sheet in all_sites
    ]
    targets.sort(key=lambda r: r["site_id"])
    return targets


def load_done_site_ids(out_csv, retry_failed):
    """Returns set of site_ids to SKIP, honoring --retry-failed semantics.

    Append-only CSV may contain more than one row per site_id across resumed
    runs; the LAST row for a site_id determines whether it's still pending.
    """
    last_outcome = {}
    if not os.path.exists(out_csv):
        return set()
    with open(out_csv, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sid = row.get("site_id")
            if not sid:
                continue
            last_outcome[sid] = row.get("outcome", "")
    if retry_failed:
        return {sid for sid, oc in last_outcome.items() if oc not in RETRYABLE_OUTCOMES}
    return set(last_outcome.keys())


# ---------------------------------------------------------------------------
# Stealth fetch injection (child process only, applied AFTER
# count_only_harness.apply_write_guards so the .pdf network guards there
# still take precedence for any .pdf-looking target)
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Minimal requests.Response look-alike backing a StealthSession result."""

    def __init__(self, text, status_code=200, url=""):
        self.status_code = status_code
        self.text = text or ""
        self.content = self.text.encode("utf-8", errors="replace")
        self.url = url
        self.headers = {}
        self.reason = "OK"
        self.ok = 200 <= status_code < 400
        self.encoding = "utf-8"

    def raise_for_status(self):
        if not self.ok:
            import requests as _rq
            raise _rq.HTTPError(f"{self.status_code} Error for url: {self.url}")

    def json(self, **_kwargs):
        return json.loads(self.text)


_NON_GET_CURL_FLAGS = ("-d", "--data", "--data-raw", "--data-binary",
                        "--data-urlencode", "-F", "--form",
                        "-T", "--upload-file")


def apply_stealth_patches(delay):
    """Patch BaseCrawler._request, requests.Session.request, and curl-based
    subprocess.run GET calls to try crawler.stealth_fetcher.StealthSession
    first. Every path falls back to the pre-existing (already-guarded)
    fetch logic on stealth failure. Returns a live stats dict."""
    from crawler.stealth_fetcher import StealthSession
    import crawler.base_crawler as _base
    import requests as _rq
    import subprocess as _sp

    session = StealthSession(timeout=25, playwright_timeout=45)
    stats = {"stealth_ok": 0, "stealth_fail": 0, "fallback": 0}

    def _try_stealth(url):
        try:
            html, info = session.fetch_html(url)
            if info.get("final_reason") == "ok" and html:
                stats["stealth_ok"] += 1
                return _FakeResponse(html, 200, url)
            stats["stealth_fail"] += 1
            print(f"[stealth] fail url={url} reason={info.get('final_reason')} "
                  f"attempts={info.get('attempts')}")
        except Exception as exc:
            stats["stealth_fail"] += 1
            print(f"[stealth] error url={url} err={type(exc).__name__}:{exc}")
        return None

    def _with_params(url, params):
        if not params:
            return url
        try:
            sep = "&" if "?" in url else "?"
            return f"{url}{sep}{urlencode(params)}"
        except Exception:
            return url

    # --- 1. BaseCrawler._request -------------------------------------------
    _orig_base_request = _base.BaseCrawler._request

    def _stealth_base_request(self, url, params=None, method="GET", retries=3, **kwargs):
        if isinstance(method, str) and method.upper() == "GET":
            full_url = _with_params(url, params)
            time.sleep(delay)
            resp = _try_stealth(full_url)
            if resp is not None:
                return resp
        stats["fallback"] += 1
        return _orig_base_request(self, url, params=params, method=method, retries=retries, **kwargs)

    _base.BaseCrawler._request = _stealth_base_request

    # --- 2. requests.Session.request (already .pdf-guarded by count_only_harness) --
    _orig_session_request = _rq.Session.request

    def _stealth_session_request(self, method, url, *args, **kwargs):
        if isinstance(method, str) and method.upper() == "GET" and isinstance(url, str):
            path = url.split("?")[0].split("#")[0]
            if not path.lower().endswith(".pdf"):
                full_url = _with_params(url, kwargs.get("params"))
                resp = _try_stealth(full_url)
                if resp is not None:
                    return resp
                stats["fallback"] += 1
        return _orig_session_request(self, method, url, *args, **kwargs)

    _rq.Session.request = _stealth_session_request

    # --- 3. subprocess.run curl GET calls (already .pdf-guarded) -----------
    _orig_sp_run = _sp.run

    def _stealth_sp_run(cmd, *args, **kwargs):
        try:
            if isinstance(cmd, (list, tuple)) and cmd:
                argv0 = str(cmd[0]).lower()
                if "curl" in argv0:
                    cmd_l = [str(c) for c in cmd]
                    is_post = False
                    for i, c in enumerate(cmd_l):
                        if c in _NON_GET_CURL_FLAGS:
                            is_post = True
                            break
                        if c in ("-X", "--request") and i + 1 < len(cmd_l) \
                                and cmd_l[i + 1].upper() != "GET":
                            is_post = True
                            break
                    url = next((c for c in cmd_l
                                if c.lower().startswith(("http://", "https://"))), None)
                    if url and not is_post and not url.split("?")[0].lower().endswith(".pdf"):
                        resp = _try_stealth(url)
                        if resp is not None:
                            class _FakeCompleted:
                                returncode = 0
                                stdout = resp.content
                                stderr = b""
                            return _FakeCompleted()
                        stats["fallback"] += 1
        except Exception:
            pass
        return _orig_sp_run(cmd, *args, **kwargs)

    _sp.run = _stealth_sp_run

    return stats


# ---------------------------------------------------------------------------
# Outcome classification
# ---------------------------------------------------------------------------

def _log_has_fetch_signal(log_path, max_bytes=300_000):
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            text = f.read(max_bytes).lower()
        return any(k in text for k in _FETCH_FAIL_KEYWORDS)
    except OSError:
        return False


def classify_outcome(result, log_path):
    method = result.get("method", "")
    completed = result.get("completed", False)
    counted = result.get("counted", 0)

    if method == "no_crawler":
        return "no_crawler"
    if method.startswith("error:"):
        return "fetch_fail" if _log_has_fetch_signal(log_path) else "error"
    if not completed:
        # external site-timeout kill (a lower bound on `counted`)
        return "fetch_fail" if (counted == 0 and _log_has_fetch_signal(log_path)) else "site_timeout"
    if counted == 0:
        return "fetch_fail" if _log_has_fetch_signal(log_path) else "zero_parsed"
    return "ok"


# ---------------------------------------------------------------------------
# Child process entry point
# ---------------------------------------------------------------------------

def _child_main(site_id, delay, progress_path, log_path, result_path):
    start = time.time()
    result = {"counted": 0, "completed": False, "method": "count_crawl", "note": ""}
    counter = {"saves": 0}
    stealth_stats = {"stealth_ok": 0, "stealth_fail": 0, "fallback": 0}
    log_f = None
    old_out, old_err = sys.stdout, sys.stderr
    try:
        log_f = open(log_path, "w", encoding="utf-8")
        sys.stdout = log_f
        sys.stderr = log_f
        try:
            counter = coh.apply_write_guards(progress_path)
            stealth_stats = apply_stealth_patches(delay)
            from crawler.sites import CRAWLERS
            cls = CRAWLERS.get(site_id)
            if cls is None:
                result.update(counted=0, completed=True, method="no_crawler", note="")
            else:
                cap_notes = coh.neutralize_caps(cls)
                conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
                inst = cls(db_conn=conn, delay=delay)
                inst.crawl(limit=None)
                note = ("caps_neutralized:" + ";".join(cap_notes))[:150] if cap_notes else ""
                result.update(counted=counter["saves"], completed=True, method="count_crawl", note=note)
        finally:
            sys.stdout, sys.stderr = old_out, old_err
    except Exception as e:
        sys.stdout, sys.stderr = old_out, old_err
        result.update(
            counted=counter.get("saves", 0), completed=False,
            method=f"error:{type(e).__name__}", note=str(e)[:200],
        )
        try:
            if log_f:
                traceback.print_exc(file=log_f)
        except Exception:
            pass
    finally:
        result["elapsed_s"] = round(time.time() - start, 1)
        stealth_note = (f"stealth_ok={stealth_stats.get('stealth_ok', 0)} "
                         f"stealth_fail={stealth_stats.get('stealth_fail', 0)} "
                         f"fallback={stealth_stats.get('fallback', 0)}")
        result["stealth_note"] = stealth_note
        try:
            coh._write_progress(progress_path, result["counted"])
        except Exception:
            pass
        try:
            with open(result_path, "w", encoding="utf-8") as f:
                json.dump(result, f)
        except Exception:
            pass
        if log_f:
            try:
                log_f.write(f"\n[full_survey] {stealth_note}\n")
                log_f.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Observability: heartbeat log + status JSON
# ---------------------------------------------------------------------------

def _atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)
    os.replace(tmp, path)


def _fmt_eta(seconds):
    if seconds is None or seconds < 0:
        return "unknown"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _throughput_and_eta(done, total, start_ts):
    elapsed = time.time() - start_ts
    if done <= 0 or elapsed <= 0:
        return 0.0, None
    rate_per_min = done / (elapsed / 60.0)
    remaining = max(total - done, 0)
    eta_s = (remaining / rate_per_min) * 60.0 if rate_per_min > 0 else None
    return rate_per_min, eta_s


def write_heartbeat(path, done, total, outcome_counts, running, start_ts, note=""):
    rate, eta_s = _throughput_and_eta(done, total, start_ts)
    in_flight = ", ".join(
        f"{sid}:{int(time.time() - info['start'])}s" for sid, info in running.items()
    ) or "-"
    line = (
        f"{time.strftime('%Y-%m-%dT%H:%M:%S')} done={done}/{total} "
        f"ok={outcome_counts.get('ok', 0)} zero={outcome_counts.get('zero_parsed', 0)} "
        f"fetch_fail={outcome_counts.get('fetch_fail', 0)} "
        f"site_timeout={outcome_counts.get('site_timeout', 0)} "
        f"error={outcome_counts.get('error', 0)} "
        f"no_crawler={outcome_counts.get('no_crawler', 0)} "
        f"in_flight=[{in_flight}] rate={rate:.2f}/min eta={_fmt_eta(eta_s)}"
        + (f" note={note}" if note else "")
    )
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def write_status(path, started_at, total, done, outcome_counts, running, last_site, note=""):
    obj = {
        "started_at": started_at,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total": total,
        "done": done,
        "pct": round(100.0 * done / total, 2) if total else 0.0,
        "ok": outcome_counts.get("ok", 0),
        "zero": outcome_counts.get("zero_parsed", 0),
        "fetch_fail": outcome_counts.get("fetch_fail", 0),
        "timeout": outcome_counts.get("site_timeout", 0),
        "error": outcome_counts.get("error", 0),
        "no_crawler": outcome_counts.get("no_crawler", 0),
        "in_flight": [
            {"site_id": sid, "elapsed_s": round(time.time() - info["start"], 1)}
            for sid, info in running.items()
        ],
        "last_site": last_site,
        "note": note,
    }
    _atomic_write_json(path, obj)


# ---------------------------------------------------------------------------
# Parent orchestration
# ---------------------------------------------------------------------------

def _collect_result(sid, row, info):
    result_path = info["result_path"]
    progress_path = info["progress_path"]
    log_path = info["log_path"]
    elapsed = round(time.time() - info["start"], 1)
    if os.path.exists(result_path):
        try:
            with open(result_path, encoding="utf-8") as f:
                r = json.load(f)
            out_row = {
                "site_id": sid, "sheet": row.get("sheet", ""), "collected": row.get("collected", 0),
                "counted": r.get("counted", 0), "completed": r.get("completed", False),
                "elapsed_s": r.get("elapsed_s", elapsed), "method": r.get("method", "count_crawl"),
                "note": (r.get("note", "") + " | " + r.get("stealth_note", "")).strip(" |"),
            }
            out_row["outcome"] = classify_outcome(r, log_path)
            return out_row
        except Exception:
            pass
    counted = 0
    if os.path.exists(progress_path):
        try:
            with open(progress_path, encoding="utf-8") as f:
                counted = json.load(f).get("count", 0)
        except Exception:
            pass
    fake_result = {"counted": counted, "completed": False, "method": "count_crawl"}
    return {
        "site_id": sid, "sheet": row.get("sheet", ""), "collected": row.get("collected", 0),
        "counted": counted, "completed": False,
        "elapsed_s": elapsed, "method": "count_crawl",
        "outcome": classify_outcome(fake_result, log_path),
        "note": "external_timeout_kill:lower_bound",
    }


def run_all(targets, workers, site_timeout, delay, out_csv, heartbeat_log, status_json,
            heartbeat_interval, csv_mode, total_all=None, done_so_far=0,
            outcome_counts=None, start_ts=None, started_at=None):
    os.makedirs(PROGRESS_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(heartbeat_log), exist_ok=True)
    ctx = mp.get_context("fork")

    prev_handlers = {
        signal.SIGTERM: signal.signal(signal.SIGTERM, _handle_stop_signal),
        signal.SIGINT: signal.signal(signal.SIGINT, _handle_stop_signal),
    }

    pending = list(targets)
    running = {}
    total_all = len(targets) if total_all is None else total_all
    done = done_so_far
    started_at = started_at or time.strftime("%Y-%m-%dT%H:%M:%S")
    start_ts = start_ts if start_ts is not None else time.time()
    last_heartbeat = 0.0
    outcome_counts = outcome_counts if outcome_counts is not None else Counter()
    last_site = None
    stop_note = ""

    write_header = csv_mode == "w" or not os.path.exists(out_csv)
    out_f = open(out_csv, csv_mode, newline="", encoding="utf-8")
    writer = csv.DictWriter(out_f, fieldnames=CSV_FIELDS)
    if write_header:
        writer.writeheader()
        out_f.flush()

    try:
        while pending or running:
            if _stop_requested:
                stop_note = "stop_requested:draining"
                break

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
                proc = ctx.Process(target=_child_main, args=(sid, delay, progress_path, log_path, result_path))
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
                    print(f"[full_survey] {sid} exceeded {site_timeout}s, terminating", flush=True)
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
                os.fsync(out_f.fileno())
                done += 1
                last_site = sid
                outcome_counts[out_row["outcome"]] += 1
                print(f"[full_survey] {done}/{total_all} done "
                      f"(last={sid} -> counted={out_row['counted']} "
                      f"outcome={out_row['outcome']} elapsed={out_row['elapsed_s']}s)", flush=True)
                write_heartbeat(heartbeat_log, done, total_all, outcome_counts, running, start_ts)
                write_status(status_json, started_at, total_all, done, outcome_counts, running, last_site)

            now = time.time()
            if now - last_heartbeat >= heartbeat_interval:
                write_heartbeat(heartbeat_log, done, total_all, outcome_counts, running, start_ts)
                write_status(status_json, started_at, total_all, done, outcome_counts, running, last_site)
                last_heartbeat = now

        if _stop_requested and running:
            print(f"[full_survey] signal received, terminating {len(running)} in-flight site(s)", flush=True)
            for sid, info in running.items():
                proc = info["proc"]
                try:
                    proc.terminate()
                    proc.join(timeout=GRACE_KILL_S)
                    if proc.is_alive():
                        proc.kill()
                        proc.join(timeout=10)
                except Exception:
                    pass
            # in-flight sites get NO row written (never completed) — the next
            # --resume run naturally re-fetches them.
    finally:
        out_f.flush()
        os.fsync(out_f.fileno())
        out_f.close()
        write_heartbeat(heartbeat_log, done, total_all, outcome_counts, {}, start_ts,
                         note=stop_note or "run_finished")
        write_status(status_json, started_at, total_all, done, outcome_counts, {}, last_site,
                     note=stop_note or "run_finished")
        for sig, handler in prev_handlers.items():
            try:
                signal.signal(sig, handler)
            except Exception:
                pass

    print(f"[full_survey] wrote {out_csv} ({done} rows this run)")
    return stop_note


def write_no_crawler_rows(rows, out_csv, csv_mode, heartbeat_log, status_json,
                           outcome_counts, start_ts, started_at, total_all, done_so_far):
    write_header = csv_mode == "w" or not os.path.exists(out_csv)
    with open(out_csv, csv_mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if write_header:
            writer.writeheader()
        done = done_so_far
        for row in rows:
            writer.writerow({
                "site_id": row["site_id"], "sheet": row["sheet"], "collected": row["collected"],
                "counted": 0, "completed": True, "elapsed_s": 0, "method": "no_crawler",
                "outcome": "no_crawler", "note": "",
            })
            done += 1
            outcome_counts["no_crawler"] += 1
        f.flush()
    if rows:
        write_heartbeat(heartbeat_log, done, total_all, outcome_counts, {}, start_ts,
                         note=f"{len(rows)} no_crawler rows written")
        write_status(status_json, started_at, total_all, done, outcome_counts, {}, None)
    return done


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def write_summary(out_csv=OUT_CSV, out_md=OUT_MD):
    if not os.path.exists(out_csv):
        print(f"[full_survey] no {out_csv} found; run first")
        return
    rows = list(csv.DictReader(open(out_csv, encoding="utf-8")))
    # append-only CSV may have >1 row per site_id across resumed runs — keep last
    latest = {}
    for r in rows:
        latest[r["site_id"]] = r
    rows = list(latest.values())

    def as_num(v):
        try:
            return int(float(v))
        except (ValueError, TypeError):
            return 0

    for r in rows:
        r["_counted"] = as_num(r.get("counted"))
        r["_collected"] = as_num(r.get("collected"))

    outcome_counts = Counter(r.get("outcome", "") for r in rows)
    total_counted = sum(r["_counted"] for r in rows)
    total_sites = len(rows)
    top30 = sorted(rows, key=lambda r: -r["_counted"])[:30]

    lines = []
    lines.append("# Full Survey Capacity Summary")
    lines.append("")
    lines.append(f"Generated from `{os.path.basename(out_csv)}` — {total_sites} sites "
                 f"(deduped to last row per site_id across resumed runs).")
    lines.append("")
    lines.append("## Outcomes")
    lines.append("")
    lines.append("| outcome | sites |")
    lines.append("|---|---:|")
    for m, n in outcome_counts.most_common():
        lines.append(f"| {m} | {n} |")
    lines.append("")
    lines.append(f"- Total counted (sum of `counted` across all rows): **{total_counted:,}**")
    lines.append(f"  - `site_timeout` / `fetch_fail` rows with counted>0 are LOWER BOUNDS.")
    lines.append("")
    lines.append("## Top 30 sites by counted")
    lines.append("")
    lines.append("| site_id | collected | counted | outcome | elapsed_s |")
    lines.append("|---|---:|---:|---|---:|")
    for r in top30:
        lines.append(f"| {r['site_id']} | {r['_collected']:,} | {r['_counted']:,} | "
                     f"{r.get('outcome','')} | {r.get('elapsed_s','')} |")
    lines.append("")

    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[full_survey] wrote {out_md}")
    print("\n".join(lines[:40]))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_env():
    try:
        from dotenv import load_dotenv
        if os.path.exists(ENV_PATH):
            load_dotenv(ENV_PATH)
            print(f"[full_survey] loaded env from {ENV_PATH}")
    except ImportError:
        print("[full_survey] python-dotenv not installed, skipping .env load", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--site-timeout", type=float, default=DEFAULT_SITE_TIMEOUT_S)
    ap.add_argument("--delay", type=float, default=DEFAULT_CRAWL_DELAY)
    ap.add_argument("--limit", type=int, default=None, help="cap number of runnable targets (after --only filter)")
    ap.add_argument("--only", nargs="*", default=None, help="restrict to these site_ids (smoke test)")
    ap.add_argument("--resume", action="store_true",
                     help="append to --out-csv, skipping site_ids already recorded there")
    ap.add_argument("--retry-failed", action="store_true",
                     help="with --resume, also re-run rows whose last outcome was "
                          "fetch_fail/site_timeout/error instead of skipping them")
    ap.add_argument("--out-csv", default=OUT_CSV)
    ap.add_argument("--out-md", default=OUT_MD)
    ap.add_argument("--heartbeat-log", default=HEARTBEAT_LOG)
    ap.add_argument("--status-json", default=STATUS_JSON)
    ap.add_argument("--heartbeat-interval", type=float, default=HEARTBEAT_INTERVAL_S)
    ap.add_argument("--summary-only", action="store_true")
    args = ap.parse_args()

    if args.summary_only:
        write_summary(args.out_csv, args.out_md)
        return

    _load_env()

    targets = load_all_targets()
    print(f"[full_survey] {len(targets)} total sites in DB")

    if args.only:
        wanted = set(args.only)
        targets = [r for r in targets if r["site_id"] in wanted]

    skip_ids = set()
    csv_mode = "w"
    if args.resume:
        skip_ids = load_done_site_ids(args.out_csv, args.retry_failed)
        csv_mode = "a"
        print(f"[full_survey] --resume: {len(skip_ids)} site_ids already recorded in "
              f"{args.out_csv}, skipping" + (" (retry-failed enabled)" if args.retry_failed else ""))
    elif os.path.exists(args.out_csv):
        print(f"[full_survey] {args.out_csv} exists and --resume not set — overwriting fresh", flush=True)

    targets = [r for r in targets if r["site_id"] not in skip_ids]

    from crawler.sites import CRAWLERS  # warm import once; forked children inherit it
    print(f"[full_survey] crawler registry has {len(CRAWLERS)} entries")

    runnable = [r for r in targets if r["site_id"] in CRAWLERS]
    no_crawler_rows = [r for r in targets if r["site_id"] not in CRAWLERS]

    if args.limit:
        runnable = runnable[:args.limit]

    print(f"[full_survey] this run: runnable={len(runnable)} no_crawler={len(no_crawler_rows)} "
          f"(skipped={len(skip_ids)})")

    total_all = len(runnable) + len(no_crawler_rows)
    outcome_counts = Counter()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    start_ts = time.time()

    os.makedirs(os.path.dirname(args.heartbeat_log), exist_ok=True)
    done_so_far = write_no_crawler_rows(
        no_crawler_rows, args.out_csv, csv_mode, args.heartbeat_log, args.status_json,
        outcome_counts, start_ts, started_at, total_all, 0,
    )
    # no_crawler rows are written up-front in one shot (cheap, no subprocess needed);
    # remaining rows written incrementally by run_all(), continuing the same
    # done/total/outcome_counts tally so heartbeat/status progress is monotonic.
    csv_mode_for_run = "a" if (csv_mode == "a" or no_crawler_rows) else "w"

    run_all(runnable, args.workers, args.site_timeout, args.delay, args.out_csv,
            args.heartbeat_log, args.status_json, args.heartbeat_interval, csv_mode_for_run,
            total_all=total_all, done_so_far=done_so_far, outcome_counts=outcome_counts,
            start_ts=start_ts, started_at=started_at)
    write_summary(args.out_csv, args.out_md)


if __name__ == "__main__":
    main()

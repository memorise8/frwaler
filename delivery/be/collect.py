"""/collect: operator-only front for the collect runner plus per-site collection judgments.

The runner (deploy/collect-runner/runner.py) owns run admission; this module only
proxies to it with a fixed set of paths on the one configured base URL, adds the
operator identity as ``requested_by`` and never follows redirects. Site judgments
come from dev-flow site-status CSV files (same "latest updated_at wins" rule and
class mapping as the delivery dashboard's uncollected.py).
"""
from __future__ import annotations

import csv
import datetime
import glob
import http.client
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse

from delivery.be.access import require_operator
from delivery.be.cache import TTLCache
from delivery.be.counts import counts_available

SITE_RE = re.compile(r"^[0-9A-Za-z._-]{1,120}$")
RUN_RE = re.compile(r"^r[0-9]{4,12}$")
MODES = ("trial", "resume")
RUNNER_TIMEOUT_SECONDS = 5
RUNNER_MAX_BODY = 8 * 1024 * 1024

# Same classification as the collect console (uncollected.py CLASS / CLASS_KO).
CLASS = {"ready": "run", "plan_written": "dev", "needs_dev": "dev", "moved": "dev", "no_crawler": "dev",
         "blocked_access": "nego", "skip": "skip"}
CLASS_KO = {"run": "실행 가능", "dev": "개발 필요", "nego": "협의 필요", "skip": "대상 아님", "": "판정 없음"}
CLASS_ORDER = {"nego": 0, "dev": 2, "": 3, "run": 4, "skip": 5}
NOTES_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "collect_notes.json")
# Sites that only exist in the DB (no judgment file, no runner record) and are collected another way.
DB_ONLY_LABELS = {"scienceon-api": "API 수집"}
# O1 collection status (status/O1-full.csv), synced every 5 minutes into <COLLECT_STATUS_DIR>/runs/.
# It lives in a sub-folder so load_status (top-level *.csv only) never mixes it into the judgments.
PROGRESS_FILE = os.path.join("runs", "O1-full.csv")
DIRECT_RUNNING = ("direct_running", "running")
_SAVED_RE = re.compile(r"문서 ([0-9][0-9,]*)")


class RunnerError(Exception):
    def __init__(self, reason: str, detail: str):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # a 3xx surfaces as HTTPError instead of being followed


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


class _UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP over a unix socket (COLLECT_RUNNER_SOCKET): no TCP path between container and host is needed."""

    def __init__(self, socket_path: str, timeout: float):
        super().__init__("runner", timeout=timeout)
        self._socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._socket_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def _runner_socket() -> str | None:
    path = (os.environ.get("COLLECT_RUNNER_SOCKET") or "").strip()
    if not path:
        return None
    if not os.path.isabs(path):
        raise RunnerError("runner_unconfigured", "COLLECT_RUNNER_SOCKET must be an absolute path")
    return path


def _unix_call(socket_path: str, method: str, path: str, data: bytes | None, headers: dict):
    conn = _UnixHTTPConnection(socket_path, RUNNER_TIMEOUT_SECONDS)
    try:
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        return response.status, response.read(RUNNER_MAX_BODY + 1)
    finally:
        conn.close()


def _runner_base() -> str:
    base = (os.environ.get("COLLECT_RUNNER_URL") or "http://runner:8793").rstrip("/")
    parts = urllib.parse.urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment \
            or parts.username or parts.password:
        raise RunnerError("runner_unconfigured", "COLLECT_RUNNER_URL is not a plain http(s) base URL")
    return base


def _runner_token() -> str:
    token = os.environ.get("COLLECT_RUNNER_TOKEN") or ""
    token_file = os.environ.get("COLLECT_RUNNER_TOKEN_FILE") or ""
    if not token and token_file:
        try:
            with open(token_file, encoding="utf-8") as handle:
                token = handle.read()
        except OSError:
            raise RunnerError("runner_unconfigured", "runner token file is not readable") from None
    token = token.strip()
    if not token:
        raise RunnerError("runner_unconfigured", "COLLECT_RUNNER_TOKEN or COLLECT_RUNNER_TOKEN_FILE is not set")
    return token


def runner_call(method: str, path: str, body: dict | None = None):
    """Return (status, parsed JSON) from the runner. Raises RunnerError when it cannot be used."""
    socket_path = _runner_socket()
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"X-Runner-Token": _runner_token(), "Accept": "application/json",
               **({"Content-Type": "application/json"} if data is not None else {})}
    if socket_path:
        try:
            status, raw = _unix_call(socket_path, method, path, data, headers)
        except (OSError, ValueError, http.client.HTTPException):
            raise RunnerError("runner_unavailable", "runner is not reachable") from None
        return _runner_payload(status, raw)
    url = _runner_base() + path
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _OPENER.open(request, timeout=RUNNER_TIMEOUT_SECONDS) as response:
            status, raw = response.status, response.read(RUNNER_MAX_BODY + 1)
    except urllib.error.HTTPError as exc:
        try:
            status, raw = exc.code, exc.read(RUNNER_MAX_BODY + 1)
        except (OSError, http.client.HTTPException):
            raise RunnerError("runner_unavailable", "runner connection failed") from None
        finally:
            exc.close()
    # http.client.HTTPException: malformed status line, truncated body, etc.
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        raise RunnerError("runner_unavailable", "runner is not reachable") from None
    return _runner_payload(status, raw)


def _runner_payload(status: int, raw: bytes):
    if status == 401:
        raise RunnerError("runner_auth_failed", "runner rejected the configured token")
    if 300 <= status < 400:
        raise RunnerError("runner_bad_response", "runner answered with a redirect")
    if len(raw) > RUNNER_MAX_BODY:
        raise RunnerError("runner_bad_response", "runner response too large")
    try:
        payload = json.loads(raw or b"null")
    except ValueError:
        raise RunnerError("runner_bad_response", "runner response is not JSON") from None
    return status, payload


def _error(status: int, reason: str, detail=None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"reason": reason, "detail": detail})


def _runner_error(exc: RunnerError) -> JSONResponse:
    return _error(503 if exc.reason == "runner_unconfigured" else 502, exc.reason, exc.detail)


def _passthrough(status: int, payload):
    """Map a runner answer onto our response. 2xx bodies are returned as-is."""
    if 200 <= status < 300:
        return JSONResponse(status_code=status, content=payload)
    if status in (400, 404, 409) and isinstance(payload, dict):
        return _error(status, str(payload.get("reason") or "runner_rejected"), payload.get("detail"))
    return _error(502, "runner_bad_response", "runner answered %d" % status)


def reason_codes(detail: str) -> list[str]:
    """Tags (ext:…, new-ext:…, api:…, [사용자 확인 필요]) found in a judgment text (uncollected.py rule)."""
    out = []
    for word in detail.replace("[", " ").replace("]", " ").replace(",", " ").split():
        word = word.rstrip(".:;)")
        if word.startswith(("ext:", "new-ext:", "api:")) and word not in out:
            out.append(word)
    if "사용자 확인 필요" in detail:
        out.append("user_decision")
    return out


def load_status(directory: str | None):
    """(source info, {site_id: latest row}). Later updated_at wins; on a tie the later file (by name) wins."""
    if not directory:
        return {"ok": False, "reason": "not_configured", "files": 0}, {}
    if not os.path.isdir(directory):
        return {"ok": False, "reason": "missing", "files": 0}, {}
    latest, files, unreadable = {}, sorted(glob.glob(os.path.join(directory, "*.csv"))), []
    for path in files:
        try:
            with open(path, encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    sid = (row.get("site_id") or "").strip()
                    if not sid or sid == "site_id":
                        continue
                    row["_file"] = os.path.basename(path)
                    if sid not in latest or (row.get("updated_at") or "") >= (latest[sid].get("updated_at") or ""):
                        latest[sid] = row
        except (OSError, UnicodeDecodeError, csv.Error):
            unreadable.append(os.path.basename(path))
    info = {"ok": True, "reason": None, "files": len(files)}
    if unreadable:
        info["unreadable_files"] = unreadable
    return info, latest


def load_progress(directory: str | None):
    """(source info, {site_id: row}) from the O1 status file; read-only, never fatal."""
    path = os.environ.get("COLLECT_PROGRESS_FILE") or (os.path.join(directory, PROGRESS_FILE) if directory else "")
    if not path:
        return {"ok": False, "reason": "not_configured", "updated_at": None}, {}
    try:
        updated = datetime.datetime.fromtimestamp(os.path.getmtime(path), datetime.timezone.utc)
        with open(path, encoding="utf-8-sig", newline="") as handle:
            rows = {}
            for row in csv.DictReader(handle):
                sid = (row.get("site_id") or "").strip()
                if sid and sid != "site_id":
                    rows[sid] = row
    except FileNotFoundError:
        return {"ok": False, "reason": "missing", "updated_at": None}, {}
    except (OSError, UnicodeDecodeError, csv.Error):
        return {"ok": False, "reason": "unreadable", "updated_at": None}, {}
    stamp = updated.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"ok": True, "reason": None, "updated_at": stamp}, {sid: _direct(row, stamp) for sid, row in rows.items()}


def _direct(row: dict, file_updated_at: str) -> dict:
    """One O1 row: status, documents saved so far (the "문서 N" in result) and when we last saw it."""
    result = " ".join((row.get("result") or "").split())
    found = _SAVED_RE.findall(result)
    status = (row.get("status") or "").strip()
    return {"status": status or None, "running": status in DIRECT_RUNNING,
            "saved": int(found[-1].replace(",", "")) if found else None,
            "run_id": (row.get("run_id") or "").strip() or None, "run_folder": (row.get("run_folder") or "").strip() or None,
            "started_at": (row.get("started") or "").strip() or None, "updated_at": file_updated_at,
            "detail": result[:400]}


def _notes():
    """(ok, {site_id: note}). ok is False when the notes file is missing or unreadable."""
    try:
        with open(NOTES_PATH, encoding="utf-8") as handle:
            return True, dict(json.load(handle).get("notes") or {})
    except (OSError, ValueError, AttributeError, TypeError):
        return False, {}


def build_router(conn_factory, cache) -> APIRouter:
    router = APIRouter(prefix="/collect", dependencies=[Depends(require_operator)])
    # One /collect/sites or /collect/uncollected refresh needs runs and plans; a
    # short cache keeps a page refresh (and polling) from hammering the runner.
    # Failures are not cached (TTLCache only stores successful results).
    runner_cache = TTLCache(float(os.environ.get("COLLECT_RUNNER_CACHE_SECONDS", "3")))

    def runner_snapshot():
        code, runs = runner_call("GET", "/runner/runs")
        code2, plan_rows = runner_call("GET", "/runner/plans")
        if code != 200 or code2 != 200 or not isinstance(runs, list) or not isinstance(plan_rows, list):
            raise RunnerError("runner_bad_response", "unexpected runner answer")
        return runs, plan_rows

    def document_counts():
        def collect():
            conn = conn_factory()
            try:
                # document_counts (delivery.be.counts) is O(sites); a live GROUP BY over millions of
                # rows is only the fallback for a DB whose counts were never refreshed.
                sql = ("SELECT site_id, sum(n)::bigint AS documents FROM document_counts GROUP BY site_id"
                       if counts_available(conn) else "SELECT site_id, count(*) AS documents FROM documents GROUP BY site_id")
                rows = conn.execute(sql).fetchall()
            finally:
                conn.close()
            return {row["site_id"]: int(row["documents"]) for row in rows}
        try:
            return cache.get_or_create("collect_document_counts", collect)
        except Exception:  # the table is still useful without counts
            return None

    def site_table():
        status_info, status = load_status(os.environ.get("COLLECT_STATUS_DIR"))
        progress_info, progress = load_progress(os.environ.get("COLLECT_STATUS_DIR"))
        counts = document_counts()
        runner = {"ok": True, "reason": None}
        latest_runs, plans = {}, {}
        try:
            runs, plan_rows = runner_cache.get_or_create(_runner_base(), runner_snapshot)
            for run in runs:  # newest first
                if isinstance(run, dict) and run.get("site_id") and run["site_id"] not in latest_runs:
                    latest_runs[run["site_id"]] = run
            plans = {p["site_id"]: p for p in plan_rows if isinstance(p, dict) and p.get("site_id")}
        except RunnerError as exc:
            runner = {"ok": False, "reason": exc.reason}
        sites = []
        in_db = {sid for sid, n in (counts or {}).items() if n > 0}
        for sid in sorted(set(status) | set(latest_runs) | set(plans) | in_db):
            row = status.get(sid) or {}
            state = (row.get("state") or "").strip()
            cls = CLASS.get(state, "")
            db_only = sid not in status and sid not in latest_runs and sid not in plans
            detail = " ".join((row.get("detail") or "").split())
            plan = plans.get(sid)
            sites.append({
                "site_id": sid, "state": state or None, "class": cls,
                "class_label": DB_ONLY_LABELS.get(sid, CLASS_KO[cls]) if db_only else CLASS_KO[cls],
                "source": "db_only" if db_only else "status" if sid in status else "runner",
                "detail": detail[:400], "reason_codes": reason_codes(detail),
                "updated_at": row.get("updated_at") or None, "status_file": row.get("_file"),
                "documents": None if counts is None else counts.get(sid, 0),
                "plan": None if plan is None else {"ready": bool(plan.get("ready")), "why": plan.get("why"),
                                                   "strategy": plan.get("strategy"), "folder": plan.get("folder"),
                                                   "expected_items": plan.get("expected_items"),
                                                   "approved_large": bool(plan.get("approved_large"))},
                "latest_run": latest_runs.get(sid),
                "direct": progress.get(sid),
            })
        return {"status_source": status_info, "progress_source": progress_info,
                "runner_ok": runner["ok"], "runner_reason": runner["reason"],
                "documents_ok": counts is not None, "sites": sites}

    @router.get("/runner/health")
    def get_runner_health():
        try:
            code, payload = runner_call("GET", "/runner/health")
        except RunnerError as exc:
            return {"runner_ok": False, "reason": exc.reason, "detail": exc.detail}
        if code != 200 or not isinstance(payload, dict):
            return {"runner_ok": False, "reason": "runner_bad_response", "detail": "runner answered %d" % code}
        return {"runner_ok": True, "reason": None, "detail": None,
                **{k: payload.get(k) for k in ("paused", "max_parallel", "running", "queued")}}

    @router.get("/plans")
    def get_plans():
        try:
            return _passthrough(*runner_call("GET", "/runner/plans"))
        except RunnerError as exc:
            return _runner_error(exc)

    @router.get("/runs")
    def get_runs(site_id: str | None = Query(default=None)):
        if site_id is not None and not SITE_RE.match(site_id):
            return _error(422, "bad_site_id", "site_id must match ^[0-9A-Za-z._-]{1,120}$")
        path = "/runner/runs" + ("?" + urllib.parse.urlencode({"site_id": site_id}) if site_id else "")
        try:
            return _passthrough(*runner_call("GET", path))
        except RunnerError as exc:
            return _runner_error(exc)

    @router.post("/runs")
    def post_run(body: dict = Body(...), operator: str = Depends(require_operator)):
        site_id, mode, limit = body.get("site_id"), body.get("mode"), body.get("limit")
        if not isinstance(site_id, str) or not SITE_RE.match(site_id):
            return _error(422, "bad_site_id", "site_id must match ^[0-9A-Za-z._-]{1,120}$")
        if mode not in MODES:
            return _error(422, "bad_mode", "mode must be trial or resume")
        if limit is not None and (type(limit) is not int or limit < 1):
            return _error(422, "bad_limit", "limit must be a positive integer or omitted")
        payload = {"site_id": site_id, "mode": mode, "requested_by": operator}
        if limit is not None:
            payload["limit"] = limit
        try:
            return _passthrough(*runner_call("POST", "/runner/runs", payload))
        except RunnerError as exc:
            return _runner_error(exc)

    @router.post("/runs/{run_id}/cancel")
    def post_cancel(run_id: str, operator: str = Depends(require_operator)):
        if not RUN_RE.match(run_id):
            return _error(422, "bad_run_id", "run_id must match ^r[0-9]{4,12}$")
        try:
            return _passthrough(*runner_call("POST", "/runner/runs/%s/cancel" % run_id, {"requested_by": operator}))
        except RunnerError as exc:
            return _runner_error(exc)

    @router.get("/sites")
    def get_sites():
        return site_table()

    @router.get("/uncollected")
    def get_uncollected():
        table, (notes_ok, notes) = site_table(), _notes()
        by_id = {site["site_id"]: site for site in table["sites"]}
        rows = []
        for sid in sorted(set(by_id) | set(notes)):
            site = by_id.get(sid) or {
                "site_id": sid, "state": None, "class": "", "class_label": CLASS_KO[""], "detail": "",
                "reason_codes": [], "updated_at": None, "status_file": None,
                "documents": None if not table["documents_ok"] else 0, "plan": None, "latest_run": None,
                "source": "notes", "direct": None,
            }
            # A DB-only site (no judgment, no runner record) has documents, so it is collected.
            not_done = site["source"] != "db_only" and (site["class"] != "run" or site["documents"] == 0)
            if not not_done and sid not in notes:
                continue
            reasons = []
            if site["class"] != "run":
                reasons.append("class_" + (site["class"] or "none"))
            if site["documents"] == 0:
                reasons.append("no_documents")
            rows.append({**site, "note": notes.get(sid), "uncollected_reasons": reasons})
        rows.sort(key=lambda r: (CLASS_ORDER.get(r["class"], 9), r["site_id"]))
        by_class = {}
        for row in rows:
            by_class[row["class"] or "none"] = by_class.get(row["class"] or "none", 0) + 1
        return {"status_source": table["status_source"], "progress_source": table["progress_source"],
                "runner_ok": table["runner_ok"],
                "runner_reason": table["runner_reason"], "documents_ok": table["documents_ok"],
                "notes_ok": notes_ok, "count": len(rows), "by_class": by_class, "sites": rows}

    return router

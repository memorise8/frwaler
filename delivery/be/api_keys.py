"""/settings/api-keys: provider API keys, tested with the provider before they are saved.

Write-only: no answer ever carries a key, part of one or its length; the status file and the audit
log hold only times and results. A key is saved only after one light request with it succeeds
(through the collectors' Fetcher: robots.txt, our User-Agent, the key header sent to the provider's
API host only); a failed test keeps the key that was there. The key file is the collector's env file
(``<API_KEYS_DIR>/<provider>.env``, ``NAME=value`` lines, mode 600, owned by the folder's owner so the
collect runner's env_file_refusal accepts it), replaced atomically.

The body is read by hand (bounded, never echoed): a pydantic body model would put the key into
FastAPI's 422 ``input`` field.
"""
from __future__ import annotations

import datetime
import fcntl
import json
import os
import re
import stat
import tempfile
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from delivery.be.access import require_operator

MAX_BODY = 2048
ATTEMPT_WINDOW_S = 600
MAX_ATTEMPTS = 5
DEFAULT_COOLDOWN_S = 60
MAX_COOLDOWN_S = 24 * 3600
TEST_RUNTIME_S = 30
STATUS_FILE = "api-keys-status.json"
AUDIT_FILE = "api-keys-audit.jsonl"
LIMITS_FILE = "api-keys-limits.json"
LOCK_FILE = ".api-keys.lock"
GATE_DIR = ".gates"

PROVIDERS = {
    "openalex": {
        "label": "OpenAlex",
        "env": "OPENALEX_API_KEY",
        "pattern": re.compile(r"^[A-Za-z0-9_-]{8,128}$"),
        "test": {"url": "https://api.openalex.org/works?per_page=1&select=id",
                 "header": "Authorization", "format": "Bearer {}", "hosts": ["api.openalex.org"]},
        "test_method": "works 목록 1건(per_page=1) 요청",
    },
    "data-go-kr": {
        "label": "공공데이터포털 (data.go.kr)",
        "env": "DATA_GO_KR_SERVICE_KEY",
        "test": None,
        # The key goes in the URL (serviceKey=): the test must keep that URL out of every log first.
        "test_method": "승인된 API 1건을 numOfRows=1 로 요청 (준비 중)",
    },
    "scienceon": {
        "label": "ScienceON",
        "env": "SCIENCEON_KEY",
        "test": None,
        "test_method": "client_id·MAC 주소로 토큰 발급 1회 (준비 중)",
    },
}


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _error(status, reason, detail):
    return JSONResponse(status_code=status, content={"reason": reason, "detail": detail})


def keys_dir():
    """The configured folder, or None when it is missing or others can write to it."""
    d = os.environ.get("API_KEYS_DIR", "")
    if not d or not os.path.isabs(d):
        return None
    try:
        st = os.stat(d)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode) or st.st_mode & 0o022:
        return None
    return d


def _open_own(path, flags, mode=0o600):
    """Open a file in the keys folder without following a link; only a regular file owned like the
    folder is used (anything else raises OSError and counts as absent)."""
    fd = os.open(path, flags | os.O_NOFOLLOW, mode)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.stat(os.path.dirname(path)).st_uid:
        os.close(fd)
        raise OSError("not a private regular file")
    return fd


def _own_file(d, name):
    """<d>/<name> as a private file owned like the folder: created if missing (O_EXCL, no race),
    replaced (empty) if it is a link or belongs to someone else. Returns its path."""
    path = os.path.join(d, name)
    try:
        os.close(_open_own(path, os.O_RDONLY))
        return path
    except OSError:
        pass
    st = os.stat(d)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        _write_private(d, name, "")
        return path
    try:
        if os.geteuid() == 0 and (st.st_uid, st.st_gid) != (0, 0):
            os.fchown(fd, st.st_uid, st.st_gid)
    finally:
        os.close(fd)
    return path


def _read_text(path):
    try:
        fd = _open_own(path, os.O_RDONLY)
    except OSError:
        return ""
    with os.fdopen(fd, encoding="utf-8", errors="replace") as f:
        return f.read()


def _read_json(path, default):
    try:
        value = json.loads(_read_text(path) or "null")
    except ValueError:
        return default
    return default if value is None else value


def _write_private(d, name, text):
    """Atomic replace of <d>/<name> with mode 600, owned like the folder."""
    st = os.stat(d)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        os.fchmod(fd, 0o600)
        if os.geteuid() == 0 and (st.st_uid, st.st_gid) != (0, 0):
            os.fchown(fd, st.st_uid, st.st_gid)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(d, name))
        tmp = None
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if fd is not None:
            os.close(fd)
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _env_lines(path):
    return _read_text(path).splitlines()


def configured(d, provider):
    env = PROVIDERS[provider]["env"]
    for line in _env_lines(os.path.join(d, provider + ".env")):
        name, sep, value = line.partition("=")
        if sep and name.strip() == env and value.strip():
            return True
    return False


def save_key(d, provider, key):
    env = PROVIDERS[provider]["env"]
    kept = [l for l in _env_lines(os.path.join(d, provider + ".env")) if l.partition("=")[0].strip() != env]
    _write_private(d, provider + ".env", "\n".join(kept + ["%s=%s" % (env, key)]) + "\n")


def audit(d, provider, result, reason, operator, tested):
    line = json.dumps({"at": _now(), "provider": provider, "result": result, "reason": reason,
                       "operator": operator, "tested": tested}, ensure_ascii=False)
    fd = _open_own(_own_file(d, AUDIT_FILE), os.O_WRONLY | os.O_APPEND)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _limits(d, provider, now):
    limits = _read_json(os.path.join(d, LIMITS_FILE), {})
    if not isinstance(limits, dict):
        limits = {}
    row = limits.get(provider) if isinstance(limits.get(provider), dict) else {}
    # a stamp from the future (clock set back) is kept only a few seconds ahead
    attempts = [t for t in row.get("attempts", []) if isinstance(t, (int, float)) and now - t < ATTEMPT_WINDOW_S
                and t <= now + 5]
    until = row.get("cooldown_until") if isinstance(row.get("cooldown_until"), (int, float)) else 0
    return limits, attempts, until


def recent_attempts(d, provider, now=None):
    """Tests that reached the provider in the last ATTEMPT_WINDOW_S (kept by time, not by log lines)."""
    now = time.time() if now is None else now
    return len(_limits(d, provider, now)[1])


def cooldown_until(d, provider, now=None):
    now = time.time() if now is None else now
    until = _limits(d, provider, now)[2]
    return until if until > now else None


def note_attempt(d, provider, now, cooldown=None):
    limits, attempts, until = _limits(d, provider, now)
    cooldown = min(cooldown or 0, now + MAX_COOLDOWN_S)
    limits[provider] = {"attempts": attempts + [now], "cooldown_until": min(max(until, cooldown), now + MAX_COOLDOWN_S)}
    _write_private(d, LIMITS_FILE, json.dumps(limits) + "\n")


def record_status(d, provider, result, reason):
    status = _read_json(os.path.join(d, STATUS_FILE), {})
    if not isinstance(status, dict):
        status = {}
    status[provider] = {"result": result, "reason": reason, "at": _now()}
    _write_private(d, STATUS_FILE, json.dumps(status, ensure_ascii=False, indent=1) + "\n")


def test_key(provider, key, transport_factory, gate_dir, interval=1.5):
    """One test of the key (no retry; the Fetcher also reads robots.txt first). Returns
    (result, reason, sent, retry_at): result "ok" or a failure kind, sent = a request reached the
    provider, retry_at = when a 429 allows the next test."""
    from crawler.enumeration.http import (AccessBlocked, EnumerationStop, Fetcher, FetchError, RateLimited,
                                          RequestsTransport, TransientFailure, TransportError)
    spec = PROVIDERS[provider]["test"]
    transport = transport_factory() if transport_factory else RequestsTransport(timeout=(5, 12))
    fetcher = Fetcher("api-key-test-" + provider, transport, gate_dir, interval=interval, retries=1,
                      max_requests=4, max_runtime_s=TEST_RUNTIME_S, max_retry_wait_s=5,
                      extra_headers={spec["header"]: spec["format"].replace("{}", key)}, header_hosts=spec["hosts"])
    sent = lambda: fetcher.requests_made > 0
    later = time.time() + DEFAULT_COOLDOWN_S
    try:
        resp = fetcher.get(spec["url"])
    except RateLimited as e:
        # host_cooldown: nothing was sent, the gate still holds an earlier 429 (or 503 Retry-After)
        retry_at = max(getattr(e, "retry_at", None) or 0, later)
        if getattr(e, "status", None) == 503:
            return "connect_failed", "unavailable", sent(), retry_at
        return "rate_limited", "http_429", sent(), retry_at
    except TransientFailure as e:
        if getattr(e, "status", None) == 429:
            return "rate_limited", "http_429", sent(), later
        return "connect_failed", str(getattr(e, "reason", "") or "transient").split(":")[0][:40], sent(), None
    except AccessBlocked as e:
        if getattr(e, "status", None) in (401, 407):
            return "bad_key", "authentication_required", sent(), None
        return "refused", str(getattr(e, "reason", "") or "access_refused")[:40], sent(), None
    except (EnumerationStop, FetchError, TransportError, OSError, ValueError) as e:
        return "connect_failed", type(e).__name__, sent(), None
    try:
        body = json.loads(resp.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "connect_failed", "unexpected_response", True, None
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        return "connect_failed", "unexpected_response", True, None
    return "ok", None, True, None


FAILURES = {
    "bad_key": (422, "키가 맞지 않습니다. 다시 입력해 주세요."),
    "rate_limited": (429, "한도 초과입니다. 잠시 뒤 다시 시도해 주세요."),
    "busy": (409, "다른 키를 시험하는 중입니다. 잠시 뒤 다시 시도해 주세요."),
    "refused": (502, "공급자가 요청을 거절했습니다. 잠시 뒤 다시 시도해 주세요."),
    "connect_failed": (502, "연결 실패입니다. 다시 시도해 주세요."),
}


async def _read_key(request):
    size = 0
    chunks = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            return None, "too_large"
        chunks.append(chunk)
    try:
        body = json.loads(b"".join(chunks).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, "invalid_body"
    if not isinstance(body, dict) or set(body) != {"key"} or not isinstance(body["key"], str):
        return None, "invalid_body"
    return body["key"].strip(), None


def build_router(transport_factory=None, gate_dir=None, interval=1.5) -> APIRouter:
    """transport_factory / gate_dir are for tests; by default the production transport and the
    gate folder <API_KEYS_DIR>/.gates (a 429 cooldown survives a restart)."""
    router = APIRouter(prefix="/settings/api-keys", dependencies=[Depends(require_operator)])

    @router.get("")
    def list_keys():
        d = keys_dir()
        status = _read_json(os.path.join(d, STATUS_FILE), {}) if d else {}
        if not isinstance(status, dict):
            status = {}
        rows = []
        for provider, p in PROVIDERS.items():
            last = status.get(provider)
            rows.append({"provider": provider, "label": p["label"], "testable": p["test"] is not None,
                         "test_method": p["test_method"],
                         "configured": configured(d, provider) if d else None,
                         "last_test": last if isinstance(last, dict) else None})
        return {"available": d is not None, "providers": rows}

    @router.post("/{provider}")
    async def put_key(provider: str, request: Request, operator: str = Depends(require_operator)):
        p = PROVIDERS.get(provider)
        if p is None:
            return _error(404, "unknown_provider", "알 수 없는 공급자입니다.")
        if p["test"] is None:
            return _error(409, "not_supported", "이 공급자는 아직 시험 방법이 준비되지 않아 저장할 수 없습니다.")
        d = keys_dir()
        if d is None:
            return _error(503, "unconfigured", "키 보관 폴더가 설정되지 않았습니다.")
        key, why = await _read_key(request)
        if why == "too_large":
            return _error(413, "too_large", "요청이 너무 큽니다.")
        if why:
            return _error(400, why, "요청 형식이 올바르지 않습니다.")
        if not p["pattern"].match(key):
            try:
                audit(d, provider, "invalid_format", None, operator, False)
            except OSError:
                pass
            return _error(422, "invalid_format", "키 형식이 맞지 않습니다. 다시 입력해 주세요.")
        return await run_in_threadpool(_test_and_save, d, provider, key, operator)

    def _test_and_save(d, provider, key, operator):
        try:
            lock_fd = _open_own(_own_file(d, LOCK_FILE), os.O_RDWR)
        except OSError:
            return _error(503, "unconfigured", "키 보관 폴더를 쓸 수 없습니다.")
        try:
            try:
                # One test at a time; a second one is refused instead of holding a worker thread.
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return _error(FAILURES["busy"][0], "busy", FAILURES["busy"][1])
            return _locked_test_and_save(d, provider, key, operator)
        finally:
            os.close(lock_fd)

    def _locked_test_and_save(d, provider, key, operator):
        now = time.time()
        if cooldown_until(d, provider, now):
            audit(d, provider, "rate_limited", "cooldown", operator, False)
            status, detail = FAILURES["rate_limited"]
            return _error(status, "rate_limited", detail)
        if recent_attempts(d, provider, now) >= MAX_ATTEMPTS:
            audit(d, provider, "too_many_attempts", None, operator, False)
            return _error(429, "too_many_attempts", "시도가 너무 많습니다. 10분 뒤 다시 시도해 주세요.")
        noted = False
        try:
            result, reason, sent, retry_at = test_key(provider, key, transport_factory,
                                                      gate_dir or os.path.join(d, GATE_DIR), interval)
            if sent:
                note_attempt(d, provider, now, retry_at)
                noted = True
            if result == "ok":
                save_key(d, provider, key)
                result = "saved"
        except Exception as e:  # never the message: it could carry the key
            try:
                if not noted:
                    note_attempt(d, provider, now)
                audit(d, provider, "error", type(e).__name__, operator, True)
            except Exception:
                pass
            return _error(500, "error", "저장하지 못했습니다. 다시 시도해 주세요.")
        try:
            audit(d, provider, result, reason, operator, sent)
            record_status(d, provider, result, reason)
        except Exception:
            pass  # the outcome stands (a saved key is saved); only its record is missing
        if result == "saved":
            return {"provider": provider, "result": "saved", "at": _now()}
        status, detail = FAILURES[result]
        return _error(status, result, detail)

    return router

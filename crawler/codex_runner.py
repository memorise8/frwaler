# -*- coding: utf-8 -*-
"""codex_runner.py — Tier-2 escalation: spawn `codex exec` to autonomously
write a custom crawler when the Tier-1 AutoAddAgent (GPT JSON-config path)
fails to produce a quality result.

Public API
----------
run_codex_crawler_build(url, site_id, site_name, *, project_root,
                        max_timeout_seconds, model, stream_cb) -> dict
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Task template
# ---------------------------------------------------------------------------

TASK_TEMPLATE = """
# Task

Write a working crawler for `{site_id}` at `crawler/sites/custom/{site_id}.py`.

## Target
- Starting URL: `{url}`

## Requirements

1. Subclass `crawler.base_crawler.BaseCrawler` (absolute import — spec_from_file_location has no package context).
2. Class attributes (NOT @property): `site_id="{site_id}"`, `site_name="{site_name}"`, `base_url="{base_url}"`.
3. Implement `crawl(self, limit=None)`:
   - Discover the real list/detail API endpoints (use curl, test responses, parse JSON or HTML).
   - Parse real records with title/abstract/date/url.
   - Save via `self._save_paper({{...}})` using these fields:
     - **관리/식별** — `id`, `site_id`, `external_id` (사이트의 native ID).
     - **글번호 (점진 수집용 핵심)** — `post_number`: 사이트의 게시글 번호. 가능하면 **숫자 문자열** ("12345"); 숫자 없으면 사이트의 native slug/uuid 사용. 없으면 None. 다음 수집 때 `MAX(post_number)` 기준으로 중지 위치 측정.
     - **기본** — `title`, `abstract`.
     - **날짜** — `published_date` (작성일/출판일, ISO `YYYY-MM-DD` 권장), **`listed_date`** (게시일/list 노출 일자, ISO `YYYY-MM-DD`). list 페이지에서 두 종류 날짜가 다르면 둘 다 채워야 함.
     - **저자/기관** — `authors` (`;` 로 여러 저자 구분), `publisher` (`;` 로 여러 기관 구분, 발행기관/출판사), `department` (선택, 부서/세부조직), `journal` (학술지명, 해당 시).
     - **URL** — `url` (= 메타정보 페이지 URL, 글의 detail 페이지), `pdf_url` (첨부파일/원문 PDF 다운로드 URL, 없으면 None).
     - **부가** — `keywords` (`,` 로 구분된 단일 string), `category` (분류), `doi` (있으면), **`original_filename`** (PDF 의 원래 파일명, 예: "2026-안전보고서.pdf"; URL 의 마지막 path segment 또는 Content-Disposition 헤더에서 추출).
     - **`metadata`** — JSON string. 위에 매핑되지 않은 모든 raw 데이터를 dict로 저장. 반드시 다음 키 포함 (있는 경우):
       - `posted_date` (listed_date 의 raw 형태, parser 가 자동 추출)
       - `originalFilename` (original_filename 보완)
       - `journal_raw`, `series`, `volume`, `issue` (학술지인 경우)
       - 사이트별 native field (예: `node_id`, `nttId`, `bbsSeq` 등)
   - **`limit` MUST work for any value** including small (limit=3) and large (limit=500). NEVER hard-code a small upper bound or assume `limit` is always 3.
   - **Pagination — full-depth crawl required**:
     - Walk pages until either (a) `saved >= limit`, (b) page returns 0 new records, or (c) safety cap of 200 pages reached (just in case — log when reaching cap).
     - Track and log progress every 10 pages: `print(f"[{site_id}] page {{p}}: saved {{saved}}/{{limit_or_inf}}")`.
     - Detect end-of-pagination correctly — empty list, "next page" link absent, or items already seen (URL deduplication).
     - Use a `seen_urls = set()` to skip duplicates across pages and prevent infinite loops where a paginator silently loops back to page 1.
     - Total wall-clock budget per crawl: do not run longer than 25 minutes; if approaching, log and exit cleanly.
4. TLS issues common on Korean gov sites: use `curl --tls-max 1.3 -sk`. subprocess.run is fine.
5. Robustness (hard requirement — real sites break in unexpected ways):
   - HTML parsing: prefer `BeautifulSoup(raw, "html5lib")` over `"html.parser"`. Fallback chain: `html5lib` → `lxml` → `html.parser`. Wrap `BeautifulSoup(...)` construction in try/except so a malformed page (embedded HWP JSON, stray CDATA, SGML declarations) never crashes the run.
   - Per-item failure isolation: the detail-page loop MUST wrap each item's fetch + parse + save in `try / except Exception as exc: print(f"[{site_id}] item X failed: {{exc}}"); continue`. One bad page MUST NOT abort the whole crawl.
   - Network errors: retry curl/requests up to 3 times with exponential backoff (1s, 3s, 9s). After 3 failures on ONE item, log and skip it; continue with the next.
   - Rate limiting: sleep ~1.0s between detail fetches by default (configurable).
   - Encoding: if response body is non-UTF8 or has mixed encoding, fall back to `errors='replace'` rather than raising.
   - Empty / short abstracts: if an item's abstract is <50 chars, skip the item (don't save) but log and continue — don't crash.
   - Keyboard interrupt: the outer loop should let `KeyboardInterrupt` propagate cleanly so the user can stop a long-running crawl with Ctrl+C.
   - Never use `assert` for runtime validation — asserts vanish under optimized runs. Use explicit `if not x: ...` instead.
6. Verify end-to-end by running this script yourself:

```bash
cd {project_root}
.venv/bin/python - <<'PY'
import sqlite3, sys, importlib.util
sys.path.insert(0, '.')
from crawler import db as dbm
spec = importlib.util.spec_from_file_location('c', 'crawler/sites/custom/{site_id}.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
cls = next(v for k,v in vars(m).items() if isinstance(v,type) and hasattr(v,'crawl') and v.__name__ != 'BaseCrawler')
conn = sqlite3.connect(':memory:'); conn.row_factory = sqlite3.Row
dbm.init_db(conn)
c = cls(db_conn=conn); dbm.register_site(conn, c.site_id, c.site_name, c.base_url)
n = c.crawl(limit=3)
assert n >= 1, f'expected >=1, got {{n}}'
rows = conn.execute('SELECT title, LENGTH(abstract) l FROM documents').fetchall()
assert all(r['l'] >= 100 for r in rows), 'all abstracts must be >=100 chars'
print('saved:', n)
for r in rows: print(r['title'][:60], r['l'])
PY
```

Iterate until success. Save only when the verification passes.

## Reference

- `crawler/base_crawler.py` — base class (`_save_paper`, `_session`).
- `crawler/db.py` — DB schema.

Work inside `{project_root}/`. When done, print a final summary with saved row count and sample abstract. **CRITICAL: never write to any other workspace path** — the file MUST be created at `{project_root}/crawler/sites/custom/{site_id}.py` and nowhere else.
""".strip()


# ---------------------------------------------------------------------------
# site_id generation
# ---------------------------------------------------------------------------

def _derive_site_id(url: str) -> str:
    """Derive a slug site_id from a URL's netloc + first path keyword.

    Mimics the pattern in cmd_smart_find: strip 'www.', replace dots with '-'.
    Sanitise to [a-z0-9][a-z0-9\\-_]{1,63}.
    """
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    netloc = re.sub(r"^www\.", "", netloc)
    netloc_slug = netloc.replace(".", "-")

    # Optionally append first non-empty path segment as a keyword
    path_parts = [p for p in parsed.path.split("/") if p and not p.startswith("?")]
    path_kw = ""
    if path_parts:
        kw = path_parts[0].lower()
        # keep only alphanumeric/dash/underscore, truncate
        kw = re.sub(r"[^a-z0-9\-_]", "", kw)[:20]
        if kw:
            path_kw = f"-{kw}"

    raw = f"{netloc_slug}{path_kw}"
    # Strip leading chars that aren't [a-z0-9]
    raw = re.sub(r"^[^a-z0-9]+", "", raw)
    # Collapse consecutive dashes/underscores
    raw = re.sub(r"[-_]{2,}", "-", raw)
    # Truncate to 64 chars total
    raw = raw[:64]
    # Ensure minimum length
    if len(raw) < 2:
        raw = f"site-{raw}" if raw else "site-unknown"
    return raw


# ---------------------------------------------------------------------------
# Repo root detection
# ---------------------------------------------------------------------------

def _find_repo_root(hint: Optional[str] = None) -> str:
    """Return the absolute project root (git toplevel or hint)."""
    if hint:
        return os.path.abspath(hint)
    # Walk up from this file's location
    here = Path(__file__).resolve().parent
    for candidate in [here.parent, here.parent.parent]:
        if (candidate / ".git").exists():
            return str(candidate)
    # Fallback: parent of crawler package
    return str(here.parent)


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def run_codex_crawler_build(
    url: str,
    site_id: Optional[str] = None,
    site_name: Optional[str] = None,
    *,
    project_root: Optional[str] = None,
    max_timeout_seconds: int = 1200,
    model: Optional[str] = None,
    codex_home: Optional[str] = None,
    stream_cb: Optional[Callable[[str], None]] = None,
) -> dict:
    """Spawn ``codex exec`` with a generated task prompt; return result dict.

    Parameters
    ----------
    url:
        Target URL for the crawler to harvest.
    site_id:
        Short slug identifier. Derived from *url* if not supplied.
    site_name:
        Human-readable site name. Defaults to "Custom: <site_id>".
    project_root:
        Path to the repo root. Auto-detected from this file's location if None.
    max_timeout_seconds:
        Hard wall-clock limit. Process is killed if exceeded.
    model:
        Optional model flag forwarded to ``codex -m <model>``.
    codex_home:
        Optional path to a directory used as CODEX_HOME for the spawned codex
        process. Useful for account rotation. Defaults to user's default ~/.codex.
    stream_cb:
        Called with each stdout line (str, no trailing newline) as codex runs.

    Returns
    -------
    dict with keys: success, file_path, site_id, test_crawl, elapsed_seconds,
    codex_log_path, error.
    """
    t0 = time.monotonic()

    # ---- resolve identifiers -------------------------------------------------
    site_id = site_id or _derive_site_id(url)
    site_name = site_name or f"Custom: {site_id}"
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    project_root = _find_repo_root(project_root)

    # ---- ensure .cache dir exists -------------------------------------------
    cache_dir = os.path.join(project_root, ".cache")
    os.makedirs(cache_dir, exist_ok=True)

    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    log_path = os.path.join(cache_dir, f"codex_{site_id}_{ts}.log")

    # ---- build task prompt --------------------------------------------------
    prompt = TASK_TEMPLATE.format(
        site_id=site_id,
        site_name=site_name,
        url=url,
        base_url=base_url,
        project_root=project_root,
    )

    # ---- build codex command -------------------------------------------------
    cmd = [
        "codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "--cd", project_root,
    ]
    if model:
        cmd += ["-m", model]
    cmd.append("-")  # read prompt from stdin

    # ---- spawn subprocess ---------------------------------------------------
    env = os.environ.copy()
    if codex_home:
        env["CODEX_HOME"] = codex_home

    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge stderr into stdout
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except FileNotFoundError:
        elapsed = time.monotonic() - t0
        return _failure(
            site_id=site_id,
            log_path=log_path,
            elapsed=elapsed,
            error="codex CLI not found — ensure `codex` is on PATH",
        )

    # ---- feed prompt via stdin, stream stdout --------------------------------
    log_lines: list[str] = []

    def _stream_output(process: subprocess.Popen, prompt_text: str) -> None:
        """Write prompt to stdin then stream stdout line by line."""
        try:
            process.stdin.write(prompt_text)
            process.stdin.close()
        except BrokenPipeError:
            pass

        for raw_line in process.stdout:
            line = raw_line.rstrip("\n")
            log_lines.append(line)
            if stream_cb:
                try:
                    stream_cb(line)
                except Exception:
                    pass

    import threading
    reader_thread = threading.Thread(
        target=_stream_output, args=(proc, prompt), daemon=True
    )
    reader_thread.start()

    # ---- wait with timeout --------------------------------------------------
    timed_out = False
    try:
        proc.wait(timeout=max_timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()

    reader_thread.join(timeout=10)

    elapsed = time.monotonic() - t0

    # ---- write log file -----------------------------------------------------
    try:
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(log_lines))
    except OSError:
        pass  # log write failure is non-fatal

    # ---- timeout case --------------------------------------------------------
    if timed_out:
        return _failure(
            site_id=site_id,
            log_path=log_path,
            elapsed=elapsed,
            error=f"codex exceeded {max_timeout_seconds}s timeout",
        )

    # ---- non-zero exit -------------------------------------------------------
    exit_code = proc.returncode
    if exit_code != 0:
        tail = "\n".join(log_lines[-40:]) if log_lines else "(no output)"
        # Still check if file was created despite non-zero exit (codex sometimes
        # exits non-zero even after writing the file successfully)
        custom_path = _custom_file_path(project_root, site_id)
        if not os.path.exists(custom_path):
            return _failure(
                site_id=site_id,
                log_path=log_path,
                elapsed=elapsed,
                error=f"codex exited with code {exit_code}. Last output:\n{tail[-500:]}",
            )
        # File exists despite non-zero exit — fall through to validation

    # ---- check if file was created ------------------------------------------
    custom_path = _custom_file_path(project_root, site_id)
    if not os.path.exists(custom_path):
        tail = "\n".join(log_lines[-40:]) if log_lines else "(no output)"
        return _failure(
            site_id=site_id,
            log_path=log_path,
            elapsed=elapsed,
            error=(
                f"codex finished (exit={exit_code}) but "
                f"crawler/sites/custom/{site_id}.py was not created.\n"
                f"Last output:\n{tail[-500:]}"
            ),
        )

    # ---- run test_crawl to validate quality ---------------------------------
    test_result: Optional[dict] = None
    test_error: Optional[str] = None
    try:
        from .agent_tools import test_crawl  # local import avoids circular deps
        test_result = test_crawl(site_id, limit=3)
    except Exception as exc:
        test_error = f"test_crawl raised: {exc}"

    quality_ok = False
    if test_result:
        q = test_result.get("quality", {})
        samples_with_content = q.get("samples_with_content", 0)
        quality_ok = test_result.get("success", False) and samples_with_content > 0

    if not quality_ok:
        reason = test_error or (
            f"test_crawl quality check failed: "
            f"success={test_result.get('success') if test_result else None}, "
            f"samples_with_content="
            f"{(test_result or {}).get('quality', {}).get('samples_with_content', 0)}"
        )
        return {
            "success": False,
            "file_path": custom_path,
            "site_id": site_id,
            "test_crawl": test_result,
            "elapsed_seconds": round(elapsed, 2),
            "codex_log_path": log_path,
            "error": reason,
        }

    return {
        "success": True,
        "file_path": custom_path,
        "site_id": site_id,
        "test_crawl": test_result,
        "elapsed_seconds": round(elapsed, 2),
        "codex_log_path": log_path,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _custom_file_path(project_root: str, site_id: str) -> str:
    return os.path.join(project_root, "crawler", "sites", "custom", f"{site_id}.py")


def _failure(
    *,
    site_id: Optional[str],
    log_path: str,
    elapsed: float,
    error: str,
) -> dict:
    return {
        "success": False,
        "file_path": None,
        "site_id": site_id,
        "test_crawl": None,
        "elapsed_seconds": round(elapsed, 2),
        "codex_log_path": log_path,
        "error": error,
    }

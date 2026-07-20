# -*- coding: utf-8 -*-
"""claude_runner.py — Tier-2 escalation: spawn `claude -p` to autonomously
write a custom crawler when the Tier-1 AutoAddAgent (GPT JSON-config path)
fails to produce a quality result.

Public API
----------
run_claude_crawler_build(url, site_id, site_name, *, project_root,
                         max_timeout_seconds, effort, claude_home,
                         stream_cb) -> dict

Interface is intentionally parallel to codex_runner.run_codex_crawler_build
so that both runners can be driven by a common harness.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Re-use helpers and TASK_TEMPLATE from codex_runner (no duplication)
# ---------------------------------------------------------------------------

from .codex_runner import (  # noqa: E402
    TASK_TEMPLATE,
    _custom_file_path,
    _derive_site_id,
    _failure as _codex_failure,
    _find_repo_root,
)


# ---------------------------------------------------------------------------
# _failure wrapper — same shape but uses "claude_log_path" key
# ---------------------------------------------------------------------------

def _failure(
    *,
    site_id: Optional[str],
    log_path: str,
    elapsed: float,
    error: str,
) -> dict:
    """Return a failure dict with claude_log_path key (mirrors codex_runner shape)."""
    return {
        "success": False,
        "file_path": None,
        "site_id": site_id,
        "test_crawl": None,
        "elapsed_seconds": round(elapsed, 2),
        "claude_log_path": log_path,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def run_claude_crawler_build(
    url: str,
    site_id: Optional[str] = None,
    site_name: Optional[str] = None,
    *,
    project_root: Optional[str] = None,
    max_timeout_seconds: int = 1800,
    effort: Optional[str] = "high",
    claude_home: Optional[str] = None,  # reserved for future multi-account use
    stream_cb: Optional[Callable[[str], None]] = None,
    extra_notes: Optional[str] = None,
) -> dict:
    """Spawn ``claude -p`` with a generated task prompt; return result dict.

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
        Hard wall-clock limit. Process is killed if exceeded (default 1800 = 30 min).
    effort:
        Reasoning depth passed to ``--effort``.  One of low/medium/high/xhigh/max.
        Defaults to "high".  Pass None to omit the flag.
    claude_home:
        Reserved for future multi-account rotation.  Currently unused — Claude Code
        OAuth lives in a single global session.
    stream_cb:
        Called with each stdout line (str, no trailing newline) as claude runs.

    Returns
    -------
    dict with keys: success, file_path, site_id, test_crawl, elapsed_seconds,
    claude_log_path, error.
    """
    t0 = time.monotonic()

    # ---- resolve identifiers ------------------------------------------------
    site_id = site_id or _derive_site_id(url)
    site_name = site_name or f"Custom: {site_id}"
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    project_root = _find_repo_root(project_root)

    # ---- ensure .cache dir exists ------------------------------------------
    cache_dir = os.path.join(project_root, ".cache")
    os.makedirs(cache_dir, exist_ok=True)

    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    log_path = os.path.join(cache_dir, f"claude_{site_id}_{ts}.log")

    # ---- build task prompt -------------------------------------------------
    prompt = TASK_TEMPLATE.format(
        site_id=site_id,
        site_name=site_name,
        url=url,
        base_url=base_url,
        project_root=project_root,
    )
    if extra_notes:
        prompt += f"\n\n## Site-specific notes (from prior probe — follow these)\n{extra_notes}\n"

    # ---- build claude command ----------------------------------------------
    # Prompt is passed as the last positional argument (argv) rather than stdin.
    # `claude -p "..."` is the canonical non-interactive invocation.
    # --add-dir ensures Claude can read/write inside the project tree.
    # --dangerously-skip-permissions actually bypasses permission prompts.
    # (Note: --allow-dangerously-skip-permissions only ENABLES the option
    # without forcing bypass — that earlier wrong flag caused 13 false
    # `claude_no_output` failures in round 1.)
    cmd = [
        "claude",
        "-p",
        "--model", "sonnet",
        "--dangerously-skip-permissions",
        "--add-dir", project_root,
    ]
    if effort:
        cmd += ["--effort", effort]
    # Prompt as last positional argument
    cmd.append(prompt)

    # ---- spawn subprocess --------------------------------------------------
    env = os.environ.copy()
    # claude_home is reserved for future use — no env override for now

    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,   # no stdin needed; prompt is in argv
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # merge stderr into stdout
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
            error="claude CLI not found — ensure `claude` is on PATH",
        )

    # ---- stream stdout line by line ----------------------------------------
    log_lines: list[str] = []

    def _stream_output(process: subprocess.Popen) -> None:
        for raw_line in process.stdout:
            line = raw_line.rstrip("\n")
            log_lines.append(line)
            if stream_cb:
                try:
                    stream_cb(line)
                except Exception:
                    pass

    reader_thread = threading.Thread(
        target=_stream_output, args=(proc,), daemon=True
    )
    reader_thread.start()

    # ---- wait with timeout -------------------------------------------------
    timed_out = False
    try:
        proc.wait(timeout=max_timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()

    reader_thread.join(timeout=10)

    elapsed = time.monotonic() - t0

    # ---- write log file ----------------------------------------------------
    try:
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(log_lines))
    except OSError:
        pass  # log write failure is non-fatal

    # ---- timeout case -------------------------------------------------------
    if timed_out:
        return _failure(
            site_id=site_id,
            log_path=log_path,
            elapsed=elapsed,
            error=f"claude exceeded {max_timeout_seconds}s timeout",
        )

    # ---- non-zero exit ------------------------------------------------------
    exit_code = proc.returncode
    if exit_code != 0:
        custom_path = _custom_file_path(project_root, site_id)
        if not os.path.exists(custom_path):
            tail = "\n".join(log_lines[-40:]) if log_lines else "(no output)"
            return _failure(
                site_id=site_id,
                log_path=log_path,
                elapsed=elapsed,
                error=f"claude exited with code {exit_code}. Last output:\n{tail[-500:]}",
            )
        # File exists despite non-zero exit — fall through to validation

    # ---- check if file was created -----------------------------------------
    custom_path = _custom_file_path(project_root, site_id)
    if not os.path.exists(custom_path):
        tail = "\n".join(log_lines[-40:]) if log_lines else "(no output)"
        return _failure(
            site_id=site_id,
            log_path=log_path,
            elapsed=elapsed,
            error=(
                f"claude finished (exit={exit_code}) but "
                f"crawler/sites/custom/{site_id}.py was not created.\n"
                f"Last output:\n{tail[-500:]}"
            ),
        )

    # ---- run test_crawl to validate quality --------------------------------
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
            "claude_log_path": log_path,
            "error": reason,
        }

    return {
        "success": True,
        "file_path": custom_path,
        "site_id": site_id,
        "test_crawl": test_result,
        "elapsed_seconds": round(elapsed, 2),
        "claude_log_path": log_path,
        "error": None,
    }

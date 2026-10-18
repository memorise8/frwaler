"""ext:pdf_text: PDF text for a crawler's pure detail method, extracted by the framework.

Crawlers made abstracts with ``pdftotext`` in a subprocess, which the adapter guard refuses.
Here the framework runs the same tool itself, outside the guard, on bytes that already came
through the gated fetcher: no network, no shell, a fixed set of flags, a time limit. The
detail method receives the text (``pdf_text`` argument) and keeps its own rules (strip,
minimum length) so the paper stays field-for-field what crawl() made.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

# Output-shaping flags seen in the registered crawlers; anything else is refused.
FLAGS = {"-layout", "-raw", "-nopgbrk"}
VALUED = {"-f": r"[1-9][0-9]{0,3}", "-l": r"[1-9][0-9]{0,3}", "-enc": r"UTF-8"}
MAX_TIMEOUT_S = 120


class PdfTextConfigError(ValueError):
    pass


def validate(spec):
    """``adapter.pdf_text``: {"args": [...], "timeout_s": 30, "min_bytes": 0}."""
    if not isinstance(spec, dict) or set(spec) - {"args", "timeout_s", "min_bytes"}:
        raise PdfTextConfigError("pdf_text must be an object with args, timeout_s, min_bytes")
    args = spec.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise PdfTextConfigError("pdf_text.args must be a list of strings")
    i = 0
    while i < len(args):
        flag = args[i]
        if flag in FLAGS:
            i += 1
        elif flag in VALUED and i + 1 < len(args) and re.fullmatch(VALUED[flag], args[i + 1]):
            i += 2
        else:
            raise PdfTextConfigError(f"pdf_text.args: {flag!r} is not an allowed pdftotext option")
    timeout = spec.get("timeout_s", 30)
    if type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT_S:
        raise PdfTextConfigError(f"pdf_text.timeout_s must be an integer in [1, {MAX_TIMEOUT_S}]")
    min_bytes = spec.get("min_bytes", 0)
    if type(min_bytes) is not int or min_bytes < 0:
        raise PdfTextConfigError("pdf_text.min_bytes must be a non-negative integer")


def available():
    return shutil.which("pdftotext") is not None


def extract(body, spec):
    """pdftotext stdout (utf-8, errors replaced, not stripped), or None when the body is shorter
    than min_bytes or the tool fails to run/times out -- what the crawlers' helpers returned."""
    if not body or len(body) < spec.get("min_bytes", 0):
        return None
    fd, path = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
        result = subprocess.run(["pdftotext", *spec.get("args", []), path, "-"], capture_output=True,
                                timeout=spec.get("timeout_s", 30), stdin=subprocess.DEVNULL,
                                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"})
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return result.stdout.decode("utf-8", errors="replace")

"""Bounded, content-free evidence for metadata probes, including interrupted ones."""
from __future__ import annotations

from collections import Counter
from functools import wraps
import inspect
from pathlib import Path
import re
import time
import threading
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
SOURCE_FAILURES = {"fetch_failed", "invalid_response", "item_failed", "authentication_failed",
                   "rate_limited", "configuration", "invalid_document_id"}
LISTING_STOPS = {"missing_list_configuration", "invalid_pagination_configuration", "new_document_limit",
                 "fetch_failed", "empty_listing_unverified", "repeated_listing", "pagination_unavailable",
                 "unchanged_pages", "single_page_unverified", "api_not_implemented"}


def source_location(frame):
    try:
        path = Path(frame.f_code.co_filename).resolve().relative_to(ROOT)
    except ValueError:
        return None
    if path.parts[0] != "crawler":
        return None
    return {"source": str(path), "function": frame.f_code.co_name, "line": frame.f_lineno}


def caller():
    frame = inspect.currentframe()
    try:
        while frame:
            location = source_location(frame)
            if location:
                return location
            frame = frame.f_back
    finally:
        del frame
    return None


def caller_chain():
    chain = []
    frame = inspect.currentframe()
    try:
        while frame and len(chain) < 6:
            location = source_location(frame)
            if location:
                chain.append(location)
            frame = frame.f_back
    finally:
        del frame
    return chain


def exception_locations(exc):
    frames = []
    tb = exc.__traceback__
    while tb:
        location = source_location(tb.tb_frame)
        if location:
            location["line"] = tb.tb_lineno
            frames.append(location)
        tb = tb.tb_next
    return frames[-8:]


def host_only(url):
    try:
        host = urlsplit(str(url)).hostname
        if host and len(host) <= 253 and re.fullmatch(r"[a-zA-Z0-9.:-]+", host):
            return host
    except (ValueError, TypeError):
        pass
    return None


def curl_header_probe(command, run, args, kwargs):
    """Add a private response-header sink without changing stdout or the request.

    Existing header output contracts and shell commands are left untouched.
    The caller must report the resulting status gap rather than guess success.
    """
    existing = any(v in ('-D', '--dump-header') or v.startswith(('--dump-header=', '-D'))
                   for v in map(str, command[1:]))
    if kwargs.get('shell') or existing:
        return run(command, *args, **kwargs), [], None
    with tempfile.TemporaryDirectory(prefix='probe-curl-headers-') as directory:
        target = Path(directory)/'headers'
        # Prepend options so a trailing '--' or URL remains in its original role.
        observed = [command[0], '--dump-header', str(target), *command[1:]]
        result = run(observed, *args, **kwargs)
        statuses = []
        retry_at = None
        if target.exists():
            with target.open('rb') as handle:
                raw = handle.read(256 * 1024)
            statuses = [int(x) for x in re.findall(rb'^HTTP/\S+\s+(\d{3})\b', raw, re.M)]
            blocks = re.split(rb'\r?\n\r?\n', raw)
            responses = [block for block in blocks if block.startswith(b'HTTP/')]
            if responses and statuses and statuses[-1] == 429:
                from .source_observer import retry_after
                values = re.findall(rb'^Retry-After:\s*([^\r\n]+)', responses[-1], re.I | re.M)
                retry_at = retry_after(values[-1].decode('ascii', errors='replace') if values else None)
        return result, statuses, retry_at


class Diagnostics:
    def __init__(self, flush, max_events=100):
        self._writer = flush
        self._lock = threading.RLock()
        self.max_events = max_events
        self.data = {"schema": 1, "scope": "metadata_only_no_persistence_or_completeness_proof",
                     "events": [], "event_counts": {}, "dropped_events": 0,
                     "stop_reason": "running", "source_failure": None, "listing_stop": None,
                     "request_status_coverage": "requests_curl_cffi_sync_playwright_goto_and_curl_without_existing_header_sink; browser_assets_wget_and_other_transports_unverified",
                     "content_or_credentials_recorded": False}
        self.counts = Counter()

    def flush(self):
        with self._lock:
            self._writer()

    def event(self, kind, **fields):
        with self._lock:
            self.counts[kind] += 1
            self.data["event_counts"] = dict(self.counts)
            if len(self.data["events"]) < self.max_events:
                row = {"kind": kind, **fields}
                self.data["events"].append(row)
            else:
                self.data["dropped_events"] += 1
                row = None
            self.flush()
            return row

    def update(self, row, **fields):
        with self._lock:
            if row is not None:
                row.update(fields)
            self.flush()

    def request(self, transport, method, url):
        method = str(method).upper()
        chain = caller_chain()
        return self.event("request", transport=transport,
                          method=method if method in {"GET", "POST", "HEAD", "PUT", "DELETE", "OPTIONS", "PATCH"} else "OTHER",
                          host=host_only(url), origin=chain[0] if chain else None, source_chain=chain, state="running")

    def finish_request(self, row, *, status=None, exc=None, returncode=None):
        fields = {'state': "exception" if exc else "returned"}
        if type(status) is int:
            fields["http_status"] = status
        if type(returncode) is int:
            fields["returncode"] = returncode
        if exc:
            fields["exception_type"] = type(exc).__name__
        self.update(row, **fields)

    def wrap_method(self, method, name):
        @wraps(method)
        def wrapped(*args, **kwargs):
            row = self.event("method", method=name, state="running")
            started = time.monotonic()
            try:
                value = method(*args, **kwargs)
                fields = dict(state="returned", result_type=type(value).__name__)
                if isinstance(value, (list, tuple)):
                    fields["result_count"] = len(value)
                elif value is None:
                    fields["result_is_none"] = True
                self.update(row, **fields)
                return value
            except BaseException as exc:
                self.update(row, state="interrupted" if not isinstance(exc, Exception) else "exception",
                            exception_type=type(exc).__name__, source_frames=exception_locations(exc))
                raise
            finally:
                self.update(row, elapsed_s=round(time.monotonic() - started, 3))
        return wrapped

    def install_methods(self, instance, stack):
        from unittest.mock import patch
        # Named methods preserve the observed function, rather than guessing
        # "list/detail" from URLs. Uninstrumented transports remain explicit.
        names = [n for n in dir(type(instance)) if n == "crawl" or n.startswith(("_fetch", "_extract", "_parse"))]
        for name in names:
            value = getattr(instance, name)
            if callable(value):
                stack.enter_context(patch.object(instance, name, self.wrap_method(value, name)))

    def stop(self, reason, instance=None, exc=None):
        self.data["stop_reason"] = reason
        if instance is not None:
            failure = getattr(instance, "delivery_source_failure", None)
            listing = getattr(instance, "delivery_listing_stop", None)
            self.data["source_failure"] = failure if failure in SOURCE_FAILURES else ("unrecognized" if failure else None)
            self.data["listing_stop"] = listing if listing in LISTING_STOPS else ("unrecognized" if listing else None)
            self.data["crawler_reported_exhausted"] = bool(getattr(instance, "delivery_exhausted", False))
        if exc:
            self.data["exception_type"] = type(exc).__name__
            self.data["source_frames"] = exception_locations(exc)
        self.flush()

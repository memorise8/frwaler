"""Listing page 1 health probe built from the collectors' own enumeration plans.

python -m delivery.health.plan_probe --plans-dir DIR [--site ID ...] [--exclude-file CSV]
    [--extra-holds-file FILE] --report-dir DIR --gate-dir DIR [--workers 1..4]
    [--interval-hours 0|>=48] [--dry-run]

Every request goes through ``crawler.enumeration.http.Fetcher``: robots.txt is read first, the
collector's own User-Agent is sent, and each host has one file-locked ``RequestGate`` under
``--gate-dir`` (the lock is held for the whole request, the next request on the host starts at
least ``request_interval_s`` >= 1.5 s after the previous response ended, across threads and
processes). Probe settings: one attempt per request (retries=1, no backoff), the first 429 stops
the site and writes a host cooldown into the gate (rate_limit_stop=1), no listing retry
(listing_retry.max 0). 401/403/407/451, 429, challenges, login pages and robots refusals hold the
host in <report-dir>/holds.json until an operator removes the entry; held hosts (and the read-only
--extra-holds-file hosts, e.g. collector holds) get no request: their sites are blocked/host_held.
With --interval-hours the next scan starts that long after the previous scan's start (read from
scan.json on restart). Only the plan's first listing request is made, parsed with the plan's parser;
a strategy that needs one prerequisite request (dynamic values, segment hub, robots sitemap
list) makes exactly that one, anything needing more is skipped as ``unsupported``.

Per site <report-dir>/<site_id>.json (results.read_result format), scan.json, and requests.jsonl
(one line per request: time, site, host, method, status, outcome, elapsed; never a URL, body,
cookie or header).
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import json
import re
import sqlite3
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
import xml.etree.ElementTree as ET
import zlib

from crawler.enumeration.adapters import AdapterConfigError
from crawler.enumeration.guard import AdapterUngatedNetwork, guarded
from crawler.enumeration.http import (DEFAULT_USER_AGENT, AccessBlocked, BudgetExhausted, EnumerationStop, Fetcher,
                                      LoginReview, PermanentFailure, PrivateDestination, RateLimited,
                                      REFUSAL_STATUSES, RequestsTransport, RobotsDisallowed, RobotsUnavailable,
                                      TransientFailure, TransportError)
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import (MIN_INTERVAL_S, PlanError, SitePlan, header_hosts, load_plan,
                                      resolve_headers, site_plan)
from crawler.enumeration.runner import Context
from crawler.enumeration.strategies import (STRATEGIES, PostUrl, StrategyStop, _SitemapTooLarge, _tag,
                                            generate_segments, today_from)

from .diagnostics import host_only
from .results import atomic_json, site_path, utc_now

SCOPE = "listing_page1_only"
PROBE = "plan_listing_page1_v1"
MAX_REQUESTS = 8      # robots (per origin) + one prerequisite + page 1 + redirect hops
MAX_RUNTIME_S = 300
MAX_REDIRECTS = 3
LOG_NAME = "requests.jsonl"
HOLDS_NAME = "holds.json"
_HOST = re.compile(r"[a-z0-9.:-]{1,253}")


def _code(value):
    """A short code from an exception reason (its part before ':'); never free text."""
    head = str(value or "").split(":", 1)[0]
    return head if re.fullmatch(r"[a-z0-9_]{1,40}", head) else "other"


class ScanHolds:
    """Refused hosts (Fetcher holds interface). New holds are written to ``path`` (holds.json) and
    kept until an operator removes the entry; ``extra`` hosts (e.g. collector holds) are only read.
    An unreadable holds file stops the scan rather than being read as "no holds"."""

    def __init__(self, path=None, extra=()):
        self._lock = threading.Lock()
        self.path = Path(path) if path is not None else None
        self._hosts = {host: {"reason": "extra_hold", "site_id": None} for host in extra}
        self._hosts.update(self._read())

    def _read(self):
        if self.path is None or not self.path.exists():
            return {}
        data = json.loads(self.path.read_text())
        hosts = data.get("hosts") if isinstance(data, dict) else None
        if not isinstance(hosts, dict):
            raise ValueError("invalid holds file")
        return {str(host).lower(): entry for host, entry in hosts.items()}

    def host_hold(self, host):
        with self._lock:
            return self._hosts.get((host or "").lower())

    def hold_host(self, host, reason, url=None, site_id=None):
        host = (host or "").lower()
        if not host:
            return
        with self._lock:
            if host in self._hosts:
                return
            entry = {"reason": _code(reason), "site_id": site_id, "held_at": utc_now()}
            self._hosts[host] = entry
            if self.path is not None:
                hosts = self._read()  # an entry the operator removed meanwhile stays removed
                hosts.setdefault(host, entry)
                atomic_json(self.path, {"hosts": hosts, "note": "remove a host to let the probe request it again"})

    def __len__(self):
        with self._lock:
            return len(self._hosts)


class _NullStore:
    """Strategies only report events here; the probe keeps no enumeration state."""

    def event(self, *args, **kwargs):
        pass

    def save_state(self, *args, **kwargs):
        pass


def _outcome(status):
    if status in (401, 403, 407, 451):
        return "blocked"
    if status == 429:
        return "429"
    if 200 <= status < 300:
        return "ok"
    if 300 <= status < 400:
        return "redirect"
    if status in (404, 410):
        return "not_found"
    return "http_5xx" if status >= 500 else "http_4xx"


class LoggedTransport:
    """Records every request the Fetcher sends (inside its host gate) and refuses a host that
    was held earlier in this scan before anything is sent."""

    def __init__(self, inner, site_id, holds):
        self.inner = inner
        self.site_id = site_id
        self.holds = holds
        self.resolve = getattr(inner, "resolve", None)  # Fetcher's destination check and pinning
        self.records = []
        self._robots_seen = set()

    def request(self, method, url, *, headers=None, **kwargs):
        host = host_only(url) or ""
        held = self.holds.host_hold(host)
        if held:
            raise AccessBlocked("host_held", url)
        parts = urlsplit(url)
        origin = (parts.scheme, host)
        kind = "page"
        if parts.path == "/robots.txt" and origin not in self._robots_seen:
            self._robots_seen.add(origin)
            kind = "robots"
        start = time.time()
        record = {"at": datetime.fromtimestamp(start, timezone.utc).isoformat(), "site_id": self.site_id,
                  "host": host, "method": method, "status": None, "outcome": "error", "elapsed_s": None,
                  "kind": kind}
        try:
            resp = self.inner.request(method, url, headers=headers, **kwargs)
        except TransportError as exc:
            record["outcome"] = "timeout" if "timeout" in str(exc).lower() else "error"
            raise
        else:
            record.update(status=resp.status, outcome=_outcome(resp.status))
            if resp.status in REFUSAL_STATUSES:
                self.holds.hold_host(host, f"http_{resp.status}", None, self.site_id)
            elif resp.status >= 500 and Fetcher._challenge(resp):
                # A challenge page sent with a 5xx status: the Fetcher alone would read it as a
                # server error. For the probe it is a challenge (host held, never retried).
                record["outcome"] = "challenge"
                self.holds.hold_host(host, "challenge", None, self.site_id)
                raise AccessBlocked("challenge", url, resp.status)
            return resp
        finally:
            end = time.time()
            record["elapsed_s"] = round(end - start, 3)
            record["_span"] = (start, end)
            self.records.append(record)


class RequestLog:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.spans = []  # (host, start, end)

    def write(self, records):
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                for record in records:
                    span = record.pop("_span")
                    self.spans.append((record["host"], *span))
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def min_gaps(self):
        """Per host: the smallest time between a response end and the next request start."""
        by_host = {}
        for host, start, end in sorted(self.spans, key=lambda s: s[1]):
            by_host.setdefault(host, []).append((start, end))
        out = {}
        for host, spans in sorted(by_host.items()):
            gaps = [b[0] - a[1] for a, b in zip(spans, spans[1:])]
            out[host] = round(min(gaps), 3) if gaps else None
        return out


def load_site_plan(plans_dir, site_id, registered=()):
    path = site_path(Path(plans_dir), site_id)  # <site_id>.json, site id validated
    plan = site_plan(load_plan(path), site_id, registered=registered)
    # One attempt only: a listing request failing transiently ends the probe of that site.
    return SitePlan(site_id, {**plan.raw, "listing_retry": {"wait_s": 0, "max": 0}})


def prerequisite(plan):
    """(kind, None) for the single request page 1 needs first, (None, None) when it needs none,
    (None, reason) when it needs more than one."""
    needs = []
    if plan.listing.get("dynamic"):
        needs.append("dynamic_values")
    if plan.strategy == "date_range_split" and plan.section("segments").get("mode") == "discover":
        if plan.section("segments").get("follow"):
            return None, "segment_discovery_multi_page"
        needs.append("segment_hub")
    if plan.strategy == "sitemap" and not plan.section("sitemap").get("sitemap_url"):
        needs.append("robots_sitemap_list")
    if len(needs) > 1:
        return None, "page1_needs_multiple_prerequisites"
    return (needs[0] if needs else None), None


def make_fetcher(plan, transport, gate_dir, holds):
    headers = resolve_headers(plan.raw)  # PlanError when a named environment variable is missing
    return Fetcher(plan.site_id, transport, gate_dir, holds=holds,
                   interval=max(MIN_INTERVAL_S, float(plan.get("request_interval_s"))),
                   max_requests=MAX_REQUESTS, max_runtime_s=MAX_RUNTIME_S, retries=1, backoff_s=0,
                   max_retry_wait_s=plan.get("max_retry_wait_s"), rate_limit_stop=1,
                   respect_robots=True, user_agent=DEFAULT_USER_AGENT, encoding=plan.get("encoding"),
                   extra_headers=headers, header_hosts=header_hosts(plan.raw) if headers else None,
                   max_redirects=MAX_REDIRECTS, clock=time.monotonic, sleep=time.sleep)


def default_transport(plan):
    options = {"max_bytes": int(plan.raw["max_response_bytes"])} if plan.raw.get("max_response_bytes") else {}
    return RequestsTransport(verify=plan.get("verify_ssl"), **options)


def _crawler(site_id, conn):
    from crawler.enumeration.run import registered_crawler
    with guarded():  # a constructor that connects or spawns is a stop, not an ungoverned request
        return registered_crawler(site_id, conn)


def plan_hosts(plan):
    """Every host the plan's page 1 (and its prerequisite) can be requested from."""
    urls = (plan.listing.get("url"), plan.listing.get("first_page_url"), plan.base_url,
            (plan.listing.get("dynamic") or {}).get("url"), plan.section("segments").get("hub_url"),
            plan.section("sitemap").get("sitemap_url"), plan.section("sitemap").get("robots_url"))
    return {host for host in map(host_only, filter(None, urls)) if host}


class ListingEnd(Exception):
    """Page 1 answered as an end of the listing (404/410, a plan end status, a blank body)."""

    def __init__(self, status):
        super().__init__("listing_end")
        self.status = status


def build_strategy(plan, fetcher):
    """The collector's parser and strategy objects for ``plan`` (as SiteRunner builds them),
    over a scratch in-memory documents DB (nothing is saved)."""
    from crawler import db_libertree
    from crawler.enumeration.run import make_adapter
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db_libertree.init_db(conn)
    adapter = make_adapter(plan, conn, _crawler)
    parser = ListingParser(plan.listing.get("parser"), total_cfg=plan.listing.get("total"), adapter=adapter)
    ctx = Context(plan.site_id, plan, plan.fingerprint, _NullStore(), fetcher, parser, today_from(None), {})
    return STRATEGIES[plan.strategy](ctx), conn


def page1_request(strategy, plan):
    """The plan's first listing request. date_range_split: the first explicit/discovered
    segment, else the newest generated date range (the one most likely to list items)."""
    if plan.strategy == "date_range_split":
        cfg = strategy.cfg
        mode = cfg.get("mode")
        keys = strategy.discover() if mode == "discover" else generate_segments(cfg, strategy.today)
        if not keys:
            raise StrategyStop("config_error", "no_segments")
        key = keys[0] if mode in ("explicit", "discover") else keys[-1]
        strategy.segment_first_page_url = strategy.segment_first(key)
        return strategy.page_url(strategy.start_page, strategy.filter_params(key), url=strategy.segment_url(key))
    return strategy.page_url(strategy.start_page)


def read_page1(strategy, plan):
    """(items, invalid) of the first listing page (sitemap: the first root sitemap)."""
    if plan.strategy == "sitemap":
        root = strategy._load(strategy.roots()[0])
        kind = _tag(root)
        if kind == "sitemapindex":
            return None, 0  # child sitemaps would need further requests
        if kind != "urlset":
            raise PermanentFailure("listing_parse_error:unknown_xml_root")
        locs = [next(((c.text or "").strip() for c in node if _tag(c) == "loc"), "") for node in root]
        return sum(1 for loc in locs if loc and strategy.wanted(loc)), sum(1 for loc in locs if not loc)
    page = strategy.fetch_page(page1_request(strategy, plan))
    if page.extra.get("end_status") is not None or (page.extra.get("explicit_empty") and page.status in (404, 410)):
        raise ListingEnd(page.status)  # an end page on page 1 is not an empty source
    return len(page.items), sum(1 for item in page.items if not item.url)


def classify(exc, phase="page1"):
    """(state, short reason code) for a probe that ended with ``exc`` in ``phase``
    (plan: loading the plan file, setup: fetcher/parser/adapter, page1: the requests)."""
    reason = _code(getattr(exc, "reason", None))
    if phase == "plan":
        return "configuration", "plan_missing" if isinstance(exc, FileNotFoundError) else "plan_invalid"
    if isinstance(exc, ListingEnd):
        return "error", f"listing_http_{exc.status}"
    if isinstance(exc, AdapterUngatedNetwork):
        return "configuration", "adapter_ungated_network"
    if isinstance(exc, PlanError):
        return "configuration", "plan_invalid"
    if isinstance(exc, PrivateDestination):
        return "configuration", "private_destination"
    if isinstance(exc, RobotsDisallowed):
        return "blocked", "robots_disallowed"
    if isinstance(exc, RateLimited):
        return ("blocked", reason) if exc.status == 429 else ("network_error", reason)
    if isinstance(exc, RobotsUnavailable):
        return ("timeout" if "timeout" in str(exc.reason).lower() else "network_error"), reason
    if isinstance(exc, LoginReview):
        return "blocked", "login_form"
    if isinstance(exc, AccessBlocked):
        return "blocked", "host_held" if reason in ("host_held", "host_access_hold") else reason
    if isinstance(exc, StrategyStop):
        return ("configuration" if exc.status == "config_error" else "error"), reason
    if isinstance(exc, TransientFailure):
        if exc.status == 429:
            return "blocked", "rate_limited"
        if "timeout" in str(exc.reason).lower():
            return "timeout", "timeout"
        return "network_error", f"http_{exc.status}" if exc.status else reason
    if isinstance(exc, PermanentFailure):
        if reason in ("listing_parse_error", "body_too_large"):
            return "invalid_data", reason
        if reason.startswith("invalid_"):
            return "configuration", reason
        return "error", f"listing_http_{exc.status}" if exc.status else reason
    if isinstance(exc, (ET.ParseError, EOFError, zlib.error, _SitemapTooLarge)):
        return "invalid_data", "sitemap_unreadable"
    if isinstance(exc, AdapterConfigError) or (isinstance(exc, ValueError) and phase == "setup"):
        return "configuration", "adapter_or_plan_config"
    if isinstance(exc, ValueError):  # broken JSON or markup in the page-1 response
        return "invalid_data", "response_unreadable"
    if isinstance(exc, BudgetExhausted):
        return "error", reason
    return "error", "unexpected"


def _finish_log(records, reason):
    """Name the response that ended the site where its status alone does not say it."""
    if reason == "robots_disallowed":
        kind, outcome = "robots", "robots"
    elif reason in ("challenge", "robots_challenge", "login_form", "login_redirect", "robots_login_redirect"):
        kind = "robots" if reason.startswith("robots") else "page"
        outcome = "login" if "login" in reason else "challenge"
    else:
        return
    if any(r["outcome"] == outcome for r in records):
        return  # already named by the transport (a challenge sent with a 5xx status)
    target = [r for r in records if r["kind"] == kind and r["outcome"] in ("ok", "redirect", "not_found")]
    if target:
        target[-1]["outcome"] = outcome


def probe_site(site_id, plans_dir, *, gate_dir, holds, log, transport_factory=default_transport,
               registered=(), source_hash=None):
    started = time.monotonic()
    result = {"site_id": site_id, "state": "error", "reason": None, "checked_at": None, "elapsed_s": None,
              "sample_count": 0, "invalid_count": 0, "http_errors": 0, "exception_type": None,
              "scope": SCOPE, "probe": PROBE, "source_hash": source_hash, "plan_fingerprint": None,
              "prerequisite": None, "requests": 0}
    transport, conn, phase = None, None, "plan"
    try:
        plan = load_site_plan(plans_dir, site_id, registered)
        phase = "setup"
        result["plan_fingerprint"] = plan.fingerprint
        needed, refusal = prerequisite(plan)
        result["prerequisite"] = needed
        if refusal:
            result.update(state="unsupported", reason=refusal)
        elif any(holds.host_hold(host) for host in plan_hosts(plan)):
            result.update(state="blocked", reason="host_held")  # no request at all
        else:
            transport = LoggedTransport(transport_factory(plan), site_id, holds)
            fetcher = make_fetcher(plan, transport, gate_dir, holds)
            strategy, conn = build_strategy(plan, fetcher)
            phase = "page1"
            items, invalid = read_page1(strategy, plan)
            if items is None:
                result.update(state="partial", reason="sitemap_index_children_not_fetched")
            elif items and invalid == items:
                result.update(sample_count=items, invalid_count=invalid, state="invalid_data", reason="items_invalid")
            else:
                result.update(sample_count=items, invalid_count=invalid,
                              state="healthy" if items else "empty", reason="items" if items else "no_items")
    except (Exception, EnumerationStop) as exc:
        state, reason = classify(exc, phase)
        # Only the exception type: its text may carry URLs or response content.
        result.update(state=state, reason=reason, exception_type=type(exc).__name__)
        if state == "blocked" and reason != "host_held":
            # Robots refusals and login pages are no host hold in the Fetcher; for the probe they are.
            holds.hold_host(host_only(getattr(exc, "url", None)), reason, None, site_id)
    finally:
        if conn is not None:
            conn.close()
        records = transport.records if transport is not None else []
        _finish_log(records, result["reason"] or "")
        result["requests"] = len(records)
        result["http_errors"] = sum(1 for r in records if r["kind"] == "page" and (r["status"] or 0) >= 400)
        log.write(records)
    result.update(checked_at=utc_now(), elapsed_s=round(time.monotonic() - started, 2))
    return result


def read_excludes(path):
    out = {}
    if path is None:
        return out
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.reader(handle):
            if not row or not row[0].strip() or row[0].strip().startswith("#") or row[0].strip() == "site_id":
                continue
            out[row[0].strip()] = (row[1].strip() if len(row) > 1 and row[1].strip() else "excluded")[:80]
    return out


def read_extra_holds(path):
    """One host per line (# comments allowed). Read only: never written back."""
    hosts = set()
    if path is None:
        return hosts
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        host = line.split("#", 1)[0].strip().lower()
        if not host:
            continue
        if not _HOST.fullmatch(host):
            raise ValueError("invalid host in extra holds file")
        hosts.add(host)
    return hosts


def due_in(report_dir, interval_s, now=None):
    """Seconds until the next scan: ``interval_s`` after the previous scan's start (scan.json),
    so a restart does not rescan at once. 0 without a readable previous scan."""
    try:
        started = datetime.fromisoformat(json.loads((Path(report_dir) / "scan.json").read_text())["started_at"])
        if started.tzinfo is None:
            return 0.0
        wait = started.timestamp() + interval_s - (time.time() if now is None else now)
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0
    return min(max(0.0, wait), interval_s)


def plan_site_ids(plans_dir):
    return sorted(p.stem for p in Path(plans_dir).glob("*.json")
                  if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", p.stem))


def inventory_hashes():
    """The inventory's source hash per site: read_result shows a probe only for that hash."""
    from .inventory import inventory
    return {s["site_id"]: s["source_hash"] for s in inventory(include_registry=True)["sites"]}


def registered_ids():
    from crawler.enumeration.run import registered_site_ids
    return registered_site_ids()


def run_scan(plans_dir, report_dir, gate_dir, *, sites=None, excludes=None, workers=2,
             transport_factory=default_transport, source_hashes=None, registered=None, extra_holds=()):
    report_dir, gate_dir = Path(report_dir), Path(gate_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    gate_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    excludes = excludes or {}
    selected = list(dict.fromkeys(sites)) if sites else plan_site_ids(plans_dir)
    skipped = {s: excludes[s] for s in selected if s in excludes}
    pending = [s for s in selected if s not in skipped]
    scan = {"started_at": utc_now(), "finished_at": None, "scope": SCOPE, "probe": PROBE,
            "total": len(pending), "completed": 0, "totals": {}, "skipped": dict(Counter(skipped.values())),
            "skipped_sites": skipped, "requests": 0, "per_host_min_gap_s": {}, "workers": workers}
    if source_hashes is None:
        try:
            source_hashes = inventory_hashes()
        except Exception as exc:  # results stay valid; the reader shows them as code_changed
            source_hashes, scan["source_hash_error"] = {}, type(exc).__name__
    registered = registered_ids() if registered is None else registered
    holds, log, counts = ScanHolds(report_dir / HOLDS_NAME, extra_holds), RequestLog(report_dir / LOG_NAME), Counter()
    scan["held_hosts_at_start"] = len(holds)
    atomic_json(report_dir / "scan.json", scan)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(probe_site, site_id, plans_dir, gate_dir=gate_dir, holds=holds, log=log,
                                   transport_factory=transport_factory, registered=registered,
                                   source_hash=source_hashes.get(site_id))
                   for site_id in pending]
        for future in as_completed(futures):
            result = future.result()
            atomic_json(site_path(report_dir, result["site_id"]), result)
            counts[result["state"]] += 1
            scan.update(completed=scan["completed"] + 1, totals=dict(counts), updated_at=utc_now(),
                        requests=len(log.spans))
            atomic_json(report_dir / "scan.json", scan)
            print(json.dumps({k: result[k] for k in ("site_id", "state", "reason", "requests")}), flush=True)
    scan.update(finished_at=utc_now(), totals=dict(counts), requests=len(log.spans),
                per_host_min_gap_s=log.min_gaps(), held_hosts=len(holds))
    atomic_json(report_dir / "scan.json", scan)
    return scan


def _describe(request):
    parts = urlsplit(str(request))
    out = {"method": "POST" if isinstance(request, PostUrl) else "GET", "host": parts.hostname,
           "path": parts.path or "/", "query_keys": sorted({k for k, _ in parse_qsl(parts.query, True)})}
    if isinstance(request, PostUrl) and isinstance(request.body, dict):
        out["body_keys"] = sorted(request.body)
    return out


class _NoNetwork:
    def __getattr__(self, name):
        raise RuntimeError("dry_run_no_network")


def dry_run(plans_dir, site_ids, registered=()):
    """Page-1 requests without any network: host, path and parameter names only."""
    for site_id in site_ids:
        line = {"site_id": site_id}
        try:
            plan = load_site_plan(plans_dir, site_id, registered)
            needed, refusal = prerequisite(plan)
            line.update(strategy=plan.strategy, prerequisite=needed)
            if refusal:
                line["skip"] = refusal
            elif plan.strategy == "sitemap":
                url = plan.section("sitemap").get("sitemap_url")
                line["request"] = _describe(url) if url else None
            elif needed == "segment_hub":
                line["request"] = None  # the segment comes from the hub page
            else:
                strategy, conn = build_strategy(plan, _NoNetwork())
                conn.close()
                dyn = plan.listing.get("dynamic")
                if dyn:
                    strategy._dyn_values = {name: f"{{dyn:{name}}}" for name in dyn["values"]}
                line["request"] = _describe(page1_request(strategy, plan))
        except (Exception, EnumerationStop) as exc:
            line["skip"] = classify(exc)[1]
        print(json.dumps(line, ensure_ascii=False), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m delivery.health.plan_probe", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plans-dir", type=Path, required=True, help="one-site plan JSONs named <site_id>.json")
    parser.add_argument("--site", action="append", help="probe only this site (repeatable)")
    parser.add_argument("--exclude-file", type=Path, help="CSV site_id,reason: never requested, no result file")
    parser.add_argument("--extra-holds-file", type=Path, help="hosts never requested, one per line (read only)")
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--gate-dir", type=Path, required=True, help="shared per-host request gates")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--interval-hours", type=float, default=0, help="0 runs once; otherwise >= 48")
    parser.add_argument("--dry-run", action="store_true", help="print page-1 requests, no network")
    args = parser.parse_args(argv)
    if not (1 <= args.workers <= 4 and (args.interval_hours == 0 or args.interval_hours >= 48)):
        parser.error("workers 1..4, interval-hours 0 or >= 48")
    try:
        for site_id in args.site or ():
            site_path(args.report_dir, site_id)
        excludes = read_excludes(args.exclude_file)
        extra_holds = read_extra_holds(args.extra_holds_file)
    except (OSError, ValueError) as exc:
        parser.error(f"invalid site selection, exclude or extra holds file: {type(exc).__name__}")
    if args.dry_run:
        sites = list(dict.fromkeys(args.site)) if args.site else plan_site_ids(args.plans_dir)
        dry_run(args.plans_dir, [s for s in sites if s not in excludes], registered_ids())
        return 0
    args.report_dir.mkdir(parents=True, exist_ok=True)
    with (args.report_dir / ".scan.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(2, "another health scan is running\n")
        interval = args.interval_hours * 3600
        while True:
            if interval:
                time.sleep(due_in(args.report_dir, interval))  # scheduled from the last scan's start
            run_scan(args.plans_dir, args.report_dir, args.gate_dir, sites=args.site, excludes=excludes,
                     workers=args.workers, extra_holds=extra_holds)
            if not interval:
                return 0


if __name__ == "__main__":
    raise SystemExit(main())

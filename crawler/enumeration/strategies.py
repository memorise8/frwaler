"""The four enumeration strategies.

Each strategy is a generator of ``Batch`` objects. A batch lists items in
oldest-first order and carries the cursor that becomes durable only after the
runner has saved (or ledgered) every item of the batch. Completion is reported
through ``self.outcome`` and only with evidence: source exhaustion or a
reconciled segment total. Budgets/caps never produce a completed outcome.
"""
from __future__ import annotations

import calendar
import hashlib
import re
import zlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime

from .http import LoginReview, PermanentFailure
from .parsing import Item, Page
from .urls import build_url, normalize_key_url


@dataclass
class Batch:
    segment: str
    items: list
    cursor: dict | None = None
    listed_keys: list = field(default_factory=list)
    segment_status: str | None = None
    evidence: str | None = None
    reported_total: int | None = None
    detail: dict | None = None


class StrategyStop(Exception):
    """Strategy cannot continue safely (configuration/contract problem)."""

    def __init__(self, status, reason):
        super().__init__(reason)
        self.status = status
        self.reason = reason


class Strategy:
    name = ""

    def __init__(self, ctx):
        self.ctx = ctx
        self.plan = ctx.plan
        self.listing = ctx.plan.listing
        self.outcome = ("incomplete", "not_finished")

    # -- shared helpers -----------------------------------------------------
    @property
    def page_param(self):
        return self.listing.get("page_param", "page")

    @property
    def start_page(self):
        return int(self.listing.get("start_page", 1))

    def page_value(self, n):
        if self.listing.get("page_mode") == "offset":
            return (n - self.start_page) * int(self.listing["page_size"]) + int(self.listing.get("offset_base", 0))
        return n

    def page_url(self, n, extra=None, *, url=None):
        params = dict(self.listing.get("params") or {})
        params.update(extra or {})
        params[self.page_param] = self.page_value(n)
        return build_url(url or self.listing["url"], params)

    def end_conditions(self):
        out = {}
        for entry in self.listing.get("end_statuses", ()):
            if isinstance(entry, dict):
                out[entry["status"]] = entry
            else:
                out[entry] = None
        return out

    def fetch_page(self, url):
        """Listing page. Only 404/410 and verified end statuses become empty
        end-candidate pages; any other 4xx propagates (never read as an end).
        A page that fails to parse (broken JSON, foreign HTML) is an error, not
        an empty page."""
        ends = self.end_conditions()
        try:
            resp = self.ctx.fetcher.get(url, accept_statuses=tuple(ends))
        except PermanentFailure as exc:
            if exc.status in (404, 410):
                return Page([], None, exc.status, {"error": exc.reason, "explicit_empty": True})
            raise
        if resp.status in ends:
            cond = ends[resp.status]
            if cond is not None and not self._end_body_matches(resp, cond):
                raise PermanentFailure(f"unverified_error_status_{resp.status}", url, resp.status)
            return Page([], None, resp.status, {"end_status": resp.status, "explicit_empty": True})
        page = self.ctx.parser.parse(resp)
        if not page.items and page.extra.get("login_suspect"):
            raise LoginReview("login_form", url, resp.status)
        if page.extra.get("parse_error"):
            raise PermanentFailure(f"listing_parse_error:{page.extra['parse_error']}", url, resp.status)
        return page

    @staticmethod
    def _end_body_matches(resp, cond):
        if cond.get("json_code"):
            try:
                data = resp.json()
            except ValueError:
                return False
            return isinstance(data, dict) and data.get("code") == cond["json_code"]
        return bool(re.search(cond["body_regex"], resp.text()))

    def known(self, segment, keys):
        """Keys already seen: in the item ledger or listed in this segment generation."""
        ctx = self.ctx
        return ctx.store.known_keys(ctx.site_id, keys) | ctx.store.listed_in_segment(
            ctx.site_id, ctx.plan_fp, segment, keys)

    def max_pages(self):
        value = self.plan.max_pages
        return None if value in (None, 0) else int(value)

    def segment_row(self, key, ordinal=0, parent=None):
        return self.ctx.store.ensure_segment(self.ctx.site_id, self.ctx.plan_fp, key, ordinal=ordinal, parent=parent)

    def batches(self):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# last_page_backward
# ---------------------------------------------------------------------------
class LastPageBackward(Strategy):
    name = "last_page_backward"
    SEGMENT = "all"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.cfg = ctx.plan.section("last_page")
        self.cache = {}
        self.real = {}  # page number -> fingerprint of pages judged real during the search

    def get(self, n):
        if n not in self.cache:
            self.cache[n] = self.fetch_page(self.page_url(n))
        return self.cache[n]

    def beyond(self, n, first):
        """Why page n lies beyond the last page (None when it is a real page)."""
        page = self.get(n)
        if not page.items:
            return "not_found" if page.status in (404, 410) else "empty"
        if n > self.start_page and page.fingerprint == first.fingerprint:
            return "wrap"
        if n > self.start_page and self.get(n - 1).fingerprint == page.fingerprint:
            return "clamp"
        for other, fp in self.real.items():
            if other != n and fp == page.fingerprint:
                # A real page's content repeating at another page number (e.g.
                # "page mod N") makes beyond() non-monotone: no end can be trusted.
                raise StrategyStop("incomplete", "repeated_page_content")
        self.real[n] = page.fingerprint
        return None

    def search_last_page(self, first):
        s = self.start_page
        cap = self.max_pages()
        cap_page = None if cap is None else s + cap - 1
        lo, step = s, 1
        while True:
            hi = s + step
            if cap_page is not None and hi > cap_page:
                hi = cap_page
                if hi <= lo:
                    return lo, "page_cap"
            if self.beyond(hi, first) is None:
                lo = hi
                if cap_page is not None and hi == cap_page:
                    return lo, "page_cap"
                step *= 2
                if step > 2 ** 40:
                    raise StrategyStop("incomplete", "last_page_search_unbounded")
                continue
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if self.beyond(mid, first) is None:
                    lo = mid
                else:
                    hi = mid
            return lo, self.beyond(lo + 1, first)

    def confirmed(self, last, kind, first):
        """Re-read L, L+1, L+2 fresh: L must be real and both neighbours beyond."""
        if kind == "page_cap":
            return True
        for n in (last, last + 1, last + 2):
            self.cache.pop(n, None)
            self.real.pop(n, None)
        if self.beyond(last, first) is not None:
            return False
        return self.beyond(last + 1, first) is not None and self.beyond(last + 2, first) is not None

    def find_last_page(self, first):
        for attempt in range(2):
            last, kind = self.search_last_page(first)
            if self.confirmed(last, kind, first):
                return last, kind
            self.ctx.store.event(self.ctx.site_id, "last_page_unconfirmed", {"candidate": last, "end_kind": kind,
                                                                            "attempt": attempt + 1})
            self.cache = {self.start_page: first}
            self.real = {}
        raise StrategyStop("incomplete", "last_page_unconfirmed")

    def read_in_range(self, n):
        """A page inside the known range must not be empty; re-read once before giving up."""
        page = self.get(n)
        if page.items:
            return page
        self.cache.pop(n, None)
        page = self.get(n)
        if not page.items:
            self.ctx.store.event(self.ctx.site_id, "empty_page_in_range", {"page": n, "status": page.status})
            raise StrategyStop("incomplete", f"empty_page_inside_range:{n}")
        return page

    def batches(self):
        ctx, seg = self.ctx, self.SEGMENT
        row = self.segment_row(seg)
        cursor = row["cursor"] or {}
        s = self.start_page
        resumed = "next_page" in cursor
        if not resumed:
            first = self.get(s)
            if not first.items:
                raise StrategyStop("incomplete", "empty_first_page_unverified")
            if self.cfg.get("check_unparameterized_first_page", True):
                params = dict(self.listing.get("params") or {})
                bare = self.fetch_page(build_url(self.listing["url"], params, drop={self.page_param}))
                if set(bare.keys) != set(first.keys):
                    ctx.store.event(ctx.site_id, "start_page_mismatch",
                                    {"start_page": s, "bare_keys": bare.keys[:5], "start_keys": first.keys[:5]})
                    raise StrategyStop("config_error", "start_page_mismatch")
            last, end_kind = self.find_last_page(first)
            cursor = {"phase": "backward", "last_page": last, "end_kind": end_kind, "next_page": last,
                      "first_total": first.total}
            ctx.store.event(ctx.site_id, "last_page_found", {"last_page": last, "end_kind": end_kind,
                                                            "probe_requests": len(self.cache)})
            ctx.store.update_segment(ctx.site_id, ctx.plan_fp, seg, status="running", cursor=cursor,
                                     reported_total=first.total)
            # Probe results may be stale by now; the backward pass reads fresh.
            self.cache.clear()
        last = cursor["last_page"]
        overlap = int(self.cfg.get("overlap_pages", 1))
        walk_limit = int(self.cfg.get("max_overlap_walk", 20))
        bounded_end = cursor["end_kind"] in ("wrap", "page_cap")
        k = cursor["next_page"]
        walk_limited = cursor.get("walk_limited", False)
        while k >= s:
            self.cache.clear()
            try:
                page = self.read_in_range(k)
            except StrategyStop as stop:
                if not (resumed and stop.reason.startswith("empty_page_inside_range")):
                    raise
                # Deletions between runs can remove the stored last pages. Find the
                # end again; items already saved are skipped through the ledger.
                resumed = False
                first = self.get(s)
                if not first.items:
                    raise
                new_last, kind = self.find_last_page(first)
                if new_last >= k:
                    raise
                ctx.store.event(ctx.site_id, "last_page_reprobed", {"stored_next_page": k, "last_page": new_last,
                                                                    "end_kind": kind})
                last, k = new_last, new_last
                cursor = dict(cursor, last_page=new_last, end_kind=kind, next_page=new_last)
                bounded_end = kind in ("wrap", "page_cap")
                continue
            resumed = False
            pages = [page]
            seen_now = set(page.keys)
            n = k + 1
            # Re-read the next-older page(s) after reading k: items pushed from k
            # to k+1 by insertions at the top are caught here. Keep walking while
            # a re-read page holds nothing already seen (shift > one page).
            while overlap:
                if n > last and bounded_end:
                    break
                if n - k > walk_limit:
                    # Shifted items may lie further up: never certify this pass.
                    walk_limited = True
                    ctx.store.event(ctx.site_id, "overlap_walk_limit", {"page": k, "walked_to": n - 1})
                    break
                older = self.read_in_range(n) if n <= last else self.get(n)
                pages.insert(0, older)
                seen_before = self.known(seg, older.keys) | (seen_now & set(older.keys))
                seen_now |= set(older.keys)
                if not older.items or (n - k >= overlap and seen_before):
                    break
                n += 1
            ordered = []
            for p in pages:
                ordered.extend(reversed(p.items))
            listed = [i.key for p in pages for i in p.items]
            cursor = dict(cursor, next_page=k - 1, walk_limited=walk_limited)
            if page.total is not None:
                # The total seen on the final (start) page read is the one that
                # matches the set of items listed by the end of the pass.
                cursor["last_total"] = page.total
            done = k == s and not walk_limited
            yield Batch(seg, ordered, cursor, listed,
                        segment_status="complete" if done else None,
                        evidence=f"last_page_{cursor['end_kind']}_reached_start" if done else None)
            k -= 1
        if cursor["end_kind"] == "page_cap":
            self.outcome = ("incomplete", "page_cap_reached")
            return
        if walk_limited:
            ctx.store.update_segment(ctx.site_id, ctx.plan_fp, seg, status="running", evidence="overlap_walk_limit")
            self.outcome = ("incomplete", "overlap_walk_limit")
            return
        total = cursor.get("last_total", cursor.get("first_total"))
        listed = ctx.store.listed_count(ctx.site_id, ctx.plan_fp)
        ctx.store.update_segment(ctx.site_id, ctx.plan_fp, seg, reported_total=total)
        if total is not None and total != listed:
            ctx.store.event(ctx.site_id, "total_mismatch", {"reported_total": total, "listed": listed})
            self.outcome = ("complete_with_mismatch", "reported_total_mismatch")
        else:
            self.outcome = ("complete", f"last_page_{cursor['end_kind']}_reached_start")


# ---------------------------------------------------------------------------
# forward paging shared by oldest_first_sort and date_range_split segments
# ---------------------------------------------------------------------------
class _ForwardPager(Strategy):
    def forward(self, seg, extra_params=None, *, url=None, reverse_within_page=False, overlap=0):
        """Read one segment forward from its cursor. Yields batches; returns (evidence, total)."""
        ctx = self.ctx
        row = self.segment_row(seg)
        cursor = row["cursor"] or {"next_page": self.start_page, "prev_fp": None, "first_fp": None,
                                   "total": None}
        cap = self.max_pages()
        while True:
            n = cursor["next_page"]
            if cap is not None and n - self.start_page >= cap:
                return "page_cap", cursor.get("total")
            page = self.fetch_page(self.page_url(n, extra_params, url=url))
            if not page.items:
                # An end candidate is re-read once; a transient empty/404 page is
                # not the end of the source.
                page = self.fetch_page(self.page_url(n, extra_params, url=url))
            if n == self.start_page and page.total is not None:
                cursor["total"] = page.total
            elif page.total is not None and cursor.get("total") is None:
                cursor["total"] = page.total
            if not page.items and n == self.start_page:
                return "empty_first_page", cursor.get("total")
            if not page.items:
                kind = (f"end_status_{page.status}" if page.extra.get("end_status") else
                        "not_found" if page.status in (404, 410) else "empty_page")
                after = self.fetch_page(self.page_url(n + 1, extra_params, url=url))
                if after.items:
                    ctx.store.event(ctx.site_id, "empty_page_in_range", {"segment": seg, "page": n})
                    raise StrategyStop("incomplete", f"empty_page_inside_range:{seg}:{n}")
                return kind, cursor.get("total")
            fp = page.fingerprint
            if n > self.start_page and fp in (cursor.get("prev_fp"), cursor.get("first_fp")):
                # A repeat is only an end (clamp/wrap) if it persists and the next
                # page brings nothing new; otherwise it is a transient or a hole.
                page = self.fetch_page(self.page_url(n, extra_params, url=url))
                fp = page.fingerprint
                if not page.items or fp in (cursor.get("prev_fp"), cursor.get("first_fp")):
                    after = self.fetch_page(self.page_url(n + 1, extra_params, url=url))
                    fresh = [k for k in after.keys if k not in self.known(seg, after.keys)]
                    if fresh:
                        ctx.store.event(ctx.site_id, "repeated_page_inside_range", {"segment": seg, "page": n})
                        raise StrategyStop("incomplete", f"repeated_page_inside_range:{seg}:{n}")
                    return "repeated_page", cursor.get("total")
            pages = [page]
            if overlap and n > self.start_page:
                # Re-read the previous page: deletions shift items toward lower pages.
                prev = self.fetch_page(self.page_url(n - 1, extra_params, url=url))
                if reverse_within_page:
                    pages.append(prev)
                else:
                    pages.insert(0, prev)
            ordered = []
            for p in pages:
                ordered.extend(reversed(p.items) if reverse_within_page else p.items)
            listed = [i.key for p in pages for i in p.items]
            cursor = dict(cursor, next_page=n + 1, prev_fp=fp,
                          first_fp=cursor.get("first_fp") or (fp if n == self.start_page else None))
            yield Batch(seg, ordered, cursor, listed, reported_total=cursor.get("total"))


# ---------------------------------------------------------------------------
# oldest_first_sort
# ---------------------------------------------------------------------------
PRESETS = {
    "wordpress": {
        "params": {"orderby": "date", "order": "asc", "per_page": 100},
        "page_param": "page", "start_page": 1,
        # Only WordPress's own "past the last page" error ends the listing.
        "end_statuses": [{"status": 400, "json_code": "rest_post_invalid_page_number"}],
        "parser": {"type": "json", "items_path": "", "key": "id", "url": "link", "date": "date", "title": "title"},
        "total": {"type": "header", "name": "X-WP-Total"},
    },
    "ckan": {
        "params": {"sort": "metadata_created asc", "rows": 100},
        "page_param": "start", "page_mode": "offset", "page_size": 100, "start_page": 0,
        "parser": {"type": "json", "items_path": "result.results", "key": "id", "url_template": "/dataset/{name}",
                   "date": "metadata_created", "title": "title"},
        "total": {"type": "json", "path": "result.count"},
    },
    "dspace7": {
        "params": {"sort": "dc.date.accessioned,ASC", "size": 100, "dsoType": "ITEM"},
        "page_param": "page", "start_page": 0,
        "parser": {"type": "json", "items_path": "_embedded.searchResult._embedded.objects",
                   "item_path": "_embedded.indexableObject", "key": "uuid", "url_template": "/handle/{handle}",
                   "date": "metadata.dc.date.accessioned.0.value", "title": "name"},
        "total": {"type": "json", "path": "_embedded.searchResult.page.totalElements"},
    },
}


def apply_preset(listing):
    preset = PRESETS.get(listing.get("preset")) if listing.get("preset") else None
    if listing.get("preset") and preset is None:
        raise ValueError(f"unknown listing preset: {listing['preset']}")
    if not preset:
        return listing
    merged = {**preset, **{k: v for k, v in listing.items() if k not in ("params", "parser", "total")}}
    merged["params"] = {**preset.get("params", {}), **(listing.get("params") or {})}
    merged["parser"] = {**preset.get("parser", {}), **(listing.get("parser") or {})}
    merged["total"] = listing.get("total", preset.get("total"))
    return merged


class OldestFirstSort(_ForwardPager):
    name = "oldest_first_sort"
    SEGMENT = "all"

    def batches(self):
        ctx = self.ctx
        cfg = ctx.plan.section("oldest_sort")
        gen = self.forward(self.SEGMENT, overlap=int(cfg.get("overlap_pages", 0)))
        allowed = int(cfg.get("max_order_violations", 0))
        violations, prev_date, seen = 0, None, set()
        while True:
            try:
                batch = next(gen)
            except StopIteration as stop:
                end, total = stop.value
                break
            for item in batch.items:
                if item.key in seen or not item.date:
                    continue
                seen.add(item.key)
                # Compare with the previous item only: one pinned newest item costs
                # one violation, not one per following item. Pinned items can be
                # excluded by the parser (html "exclude", json "exclude_if_true").
                if prev_date is not None and item.date < prev_date:
                    violations += 1
                prev_date = item.date
            if violations > allowed:
                # The sort parameter was ignored or does not mean "oldest first".
                ctx.store.event(ctx.site_id, "sort_order_not_ascending", {"violations": violations})
                raise StrategyStop("incomplete", "sort_order_not_ascending")
            yield batch
        if end == "page_cap":
            self.outcome = ("incomplete", "page_cap_reached")
            return
        if end == "empty_first_page" and total != 0:
            # An empty first page is not evidence of an empty source (parser
            # drift, soft block or a rejected sort parameter look the same).
            self.outcome = ("incomplete", "empty_first_page_unverified")
            return
        listed = ctx.store.listed_count(ctx.site_id, ctx.plan_fp)
        ctx.store.update_segment(ctx.site_id, ctx.plan_fp, self.SEGMENT, status="complete",
                                 evidence=end, reported_total=total)
        if total is not None and total != listed:
            ctx.store.event(ctx.site_id, "total_mismatch", {"reported_total": total, "listed": listed})
            self.outcome = ("complete_with_mismatch", "reported_total_mismatch")
        else:
            self.outcome = ("complete", f"exhausted_{end}")


# ---------------------------------------------------------------------------
# date_range_split
# ---------------------------------------------------------------------------
_FINER = {"year": "month", "month": "day"}


def _bounds(key):
    parts = [int(p) for p in key.split("-")]
    if len(parts) == 1:
        return date(parts[0], 1, 1), date(parts[0], 12, 31), "year"
    if len(parts) == 2:
        last = calendar.monthrange(parts[0], parts[1])[1]
        return date(parts[0], parts[1], 1), date(parts[0], parts[1], last), "month"
    return date(*parts), date(*parts), "day"


def _children(key, upper):
    start, end, gran = _bounds(key)
    if gran == "year":
        keys = [f"{start.year:04d}-{m:02d}" for m in range(1, 13)]
    elif gran == "month":
        keys = [f"{start.year:04d}-{start.month:02d}-{d:02d}" for d in range(1, end.day + 1)]
    else:
        return []
    return [k for k in keys if _bounds(k)[0] <= upper]


_PREFIX_KEY = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*):([0-9]+)-([0-9:]+)")
# ':' is the character after '9'. [lo, hi) are half-open string ranges compared in
# lexicographic (ASCII) order, which is how the source orders ids.
_PREFIX_ROOT = ("0", ":")


def _prefix_bounds(key):
    """(field, lo, hi) of a ``field:lo-hi`` key; the range is [lo, hi), compared as strings."""
    m = _PREFIX_KEY.fullmatch(key)
    if not m:
        raise ValueError(f"not a prefix range key: {key!r}")
    return m.group(1), m.group(2), m.group(3)


def _prefix_children(key, max_depth):
    """Disjoint, complete split of [lo, hi) by the next digit; [] when it cannot be split.

    The root [0, :) splits by first digit. Any other [p, hi) becomes
    [p, p0), [p0, p1), ..., [p8, p9), [p9, hi): the first child holds only the id p itself,
    the last one keeps the parent's upper bound. A range whose lo has max_depth digits, or
    that already is a first child [p, p0), is not split further."""
    field, lo, hi = _prefix_bounds(key)
    if (lo, hi) == _PREFIX_ROOT:
        pairs = [(str(d), str(d + 1) if d < 9 else ":") for d in range(10)]
    elif len(lo) >= max_depth or hi == lo + "0":
        return []
    else:
        pairs = [(lo, lo + "0")] + [(lo + str(d), lo + str(d + 1)) for d in range(9)] + [(lo + "9", hi)]
    return [f"{field}:{a}-{b}" for a, b in pairs]


def generate_segments(cfg, today):
    if (cfg.get("filter") or {}).get("type") == "query_prefix_range":
        # One root covering every id that starts with a digit; the rest comes from splitting.
        return [f"{cfg['filter']['field']}:{_PREFIX_ROOT[0]}-{_PREFIX_ROOT[1]}"]
    mode = cfg.get("mode", "range")
    if mode == "explicit":
        return [str(s["key"]) for s in cfg["items"]]
    gran = cfg.get("granularity", "year")
    lower = str(cfg["from"])
    upper_raw = str(cfg.get("to", "now"))
    upper = today if upper_raw == "now" else _bounds(upper_raw)[1]
    start = _bounds(lower)[0]
    keys = []
    if gran == "year":
        keys = [f"{y:04d}" for y in range(start.year, upper.year + 1)]
    elif gran == "month":
        y, m = start.year, start.month
        while (y, m) <= (upper.year, upper.month):
            keys.append(f"{y:04d}-{m:02d}")
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    elif gran == "day":
        d = start
        while d <= upper:
            keys.append(d.isoformat())
            d = date.fromordinal(d.toordinal() + 1)
    else:
        raise ValueError(f"unknown granularity: {gran}")
    return keys


class DateRangeSplit(_ForwardPager):
    name = "date_range_split"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.cfg = ctx.plan.section("segments")
        self.today = ctx.today

    def filter_params(self, key):
        flt = self.cfg.get("filter", {})
        if self.cfg.get("mode") == "explicit":
            spec = next(s for s in self.cfg["items"] if str(s["key"]) == key)
            return dict(spec.get("params") or {})
        if flt.get("type") == "query_prefix_range":
            field, lo, hi = _prefix_bounds(key)
            return {flt["param"]: f"{field}:[{lo} TO {hi}}}"}
        start, end, gran = _bounds(key)
        end = min(end, self.today)
        if flt.get("type") == "query_range":
            # urlencode at the normal page_url boundary performs the only encoding.
            # Date-only upper-bound semantics must be checked on the source before use.
            return {flt["param"]: f"{flt['field']}:[{start.isoformat()} TO {end.isoformat()}]"}
        if flt.get("type", "range") == "range":
            if flt.get("to_inclusive", True) is False:
                # Half-open [from, to): the upper bound is the day after the range.
                end = date.fromordinal(end.toordinal() + 1)
            fmt = flt.get("date_format", "%Y-%m-%d")
            return {flt["from_param"]: start.strftime(fmt), flt["to_param"]: end.strftime(fmt)}
        if flt["type"] == "parts":
            out = {flt["year_param"]: f"{start.year:04d}"}
            if gran in ("month", "day"):
                out[flt["month_param"]] = f"{start.month:{flt.get('month_format', '02d')}}"
            if gran == "day":
                out[flt["day_param"]] = f"{start.day:{flt.get('day_format', '02d')}}"
            return out
        raise ValueError(f"unknown filter type: {flt.get('type')}")

    def can_split(self, key):
        if self.cfg.get("mode") == "explicit":
            return False
        if self.cfg.get("filter", {}).get("type") == "query_prefix_range":
            return bool(_prefix_children(key, self.cfg["max_depth"]))
        gran = _bounds(key)[2]
        finer = _FINER.get(gran)
        allowed = self.cfg.get("split_to", ["month", "day"])
        flt = self.cfg.get("filter", {})
        if finer is None or finer not in allowed:
            return False
        if flt.get("type") == "parts" and not flt.get(f"{finer}_param"):
            return False
        return True

    def segment_url(self, key):
        url = self.listing["url"]
        return url.replace("{segment}", key) if "{segment}" in url else url

    def split(self, key, ordinal):
        ctx = self.ctx
        prefix = self.cfg.get("filter", {}).get("type") == "query_prefix_range"
        for child in (_prefix_children(key, self.cfg["max_depth"]) if prefix else _children(key, self.today)):
            ctx.store.ensure_segment(ctx.site_id, ctx.plan_fp, child, ordinal=ordinal, parent=key)
        ctx.store.update_segment(ctx.site_id, ctx.plan_fp, key, status="split")
        ctx.store.event(ctx.site_id, "segment_split", {"segment": key})

    def batches(self):
        ctx = self.ctx
        # The full deterministic manifest is upserted on every run: a crash in the
        # middle of a previous initialisation can never leave a range out.
        for i, key in enumerate(generate_segments(self.cfg, self.today)):
            ctx.store.ensure_segment(ctx.site_id, ctx.plan_fp, key, ordinal=i)
        state = ctx.state
        check, boundary = self.overall_check()
        cap = self.cfg.get("hidden_cap")
        reverse = self.cfg.get("within_segment_order", "newest_first") == "newest_first"
        overlap = int(self.cfg.get("overlap_pages", 0))
        while True:
            pending = [r for r in ctx.store.segments(ctx.site_id, ctx.plan_fp) if r["status"] in ("pending", "running")]
            if not pending:
                break
            row = pending[0]
            key = row["segment_key"]
            ctx.store.update_segment(ctx.site_id, ctx.plan_fp, key, status="running")
            gen = self.forward(key, self.filter_params(key), url=self.segment_url(key),
                               reverse_within_page=reverse, overlap=overlap)
            split_now = False
            while True:
                try:
                    batch = next(gen)
                except StopIteration as stop:
                    end, total = stop.value
                    break
                total = batch.reported_total
                if (self.cfg.get("filter", {}).get("type") in ("query_range", "query_prefix_range")
                        and cap and total is not None and total > cap and not self.can_split(key)):
                    # This query syntax is capped at the source. At the finest supported
                    # granularity, do not walk into the window or claim a successful leaf.
                    ctx.store.update_segment(ctx.site_id, ctx.plan_fp, key, status="cap_unsplittable",
                                             reported_total=total,
                                             evidence="reported_total_over_cap_at_finest_granularity",
                                             detail={"cap": cap})
                    ctx.store.event(ctx.site_id, "segment_cap_unsplittable", {"segment": key, "total": total})
                    split_now = True
                    break
                if cap and total is not None and total > cap and self.can_split(key) and row["cursor"] is None \
                        and batch.cursor["next_page"] == self.start_page + 1:
                    # Over the hidden cap before reading: split without saving this page.
                    ctx.store.update_segment(ctx.site_id, ctx.plan_fp, key, reported_total=total,
                                             detail={"split_reason": "reported_total_over_cap", "cap": cap})
                    self.split(key, row["ordinal"])
                    split_now = True
                    break
                yield batch
            if split_now:
                continue
            listed = ctx.store.segment(ctx.site_id, ctx.plan_fp, key)["listed"]
            detail = {"end": end, "listed": listed, "reported_total": total}
            if end == "page_cap":
                ctx.store.update_segment(ctx.site_id, ctx.plan_fp, key, status="running", detail=detail)
                self.outcome = ("incomplete", "page_cap_reached")
                return
            if total is None and listed == 0:
                # Nothing listed and nothing reported: indistinguishable from a
                # rejected filter or a broken parser. Not completion evidence.
                status, evidence = "unverified", f"empty_without_total_{end}"
            elif total is None:
                status, evidence = "complete", f"exhausted_no_total_{end}"
            elif listed == total:
                status, evidence = "complete", "total_matched"
            elif listed < total and self.can_split(key):
                ctx.store.update_segment(ctx.site_id, ctx.plan_fp, key, reported_total=total,
                                         detail=dict(detail, split_reason="listed_below_total"))
                self.split(key, row["ordinal"])
                continue
            elif cap and total > cap:
                status, evidence = "cap_unsplittable", "reported_total_over_cap_at_finest_granularity"
            else:
                status, evidence = "mismatch", f"listed_{listed}_vs_total_{total}"
            if status != "complete":
                ctx.store.event(ctx.site_id, "segment_" + status, dict(detail, segment=key))
            ctx.store.update_segment(ctx.site_id, ctx.plan_fp, key, status=status, evidence=evidence,
                                     reported_total=total, detail=detail)
        if check and "overall_total" not in state:
            # Read after the segments, so the total describes the same moment as the listed set.
            try:
                page = self.fetch_page(self.page_url(self.start_page))
            except PermanentFailure as exc:
                # A site that rejects the unfiltered listing (4xx, unparseable page) leaves only the
                # per-segment evidence. Refusals, robots denial and rate limits are not
                # PermanentFailure: they still stop the site. Not saved: a later run asks again.
                ctx.store.event(ctx.site_id, "overall_total_unavailable",
                                {"reason": exc.reason, "url": exc.url, "status": exc.status})
            else:
                state["overall_total"] = page.total
                ctx.store.save_state(ctx.site_id, state)
        rows = ctx.store.segments(ctx.site_id, ctx.plan_fp)
        bad = [r["segment_key"] for r in rows if r["status"] in ("mismatch", "cap_unsplittable")]
        leaves = [r for r in rows if r["status"] != "split"]
        # Ranges are a partition: the same key in two leaves means the filter is
        # ignored or the ranges overlap; equal first pages mean the filter may not work.
        allow_overlap = bool(self.cfg.get("allow_overlap"))
        shared = ctx.store.keys_in_several_segments(ctx.site_id, ctx.plan_fp, [r["segment_key"] for r in leaves])
        if shared and not allow_overlap:
            ctx.store.event(ctx.site_id, "segment_overlap", {"shared_keys": shared})
            bad.append("segment_overlap")
        by_fp = {}
        for r in leaves:
            first_fp = (r["cursor"] or {}).get("first_fp")
            if first_fp:
                by_fp.setdefault(first_fp, []).append(r["segment_key"])
        same_first = [keys for keys in by_fp.values() if len(keys) > 1]
        if same_first:
            ctx.store.event(ctx.site_id, "filter_not_effective", {"segments_with_identical_first_page": same_first[:10]})
            for keys in same_first:
                for key in keys:
                    ctx.store.update_segment(ctx.site_id, ctx.plan_fp, key, status="unverified",
                                             evidence="identical_first_page_across_ranges")
            rows = ctx.store.segments(ctx.site_id, ctx.plan_fp)
        totals = [r["reported_total"] for r in leaves]
        if leaves and all(t is not None for t in totals) and not allow_overlap:
            unique = ctx.store.listed_count(ctx.site_id, ctx.plan_fp)
            if sum(totals) != unique:
                ctx.store.event(ctx.site_id, "leaf_total_sum_mismatch", {"leaf_sum": sum(totals), "unique": unique})
                bad.append("leaf_total_sum_vs_unique")
        overall = state.get("overall_total")
        listed = ctx.store.listed_count(ctx.site_id, ctx.plan_fp)
        if listed == 0 and (overall != 0 or self.plan.raw.get("overall_total_check") is not True):
            # An empty source is accepted only on an explicitly requested overall total of 0.
            self.outcome = ("incomplete", "all_segments_empty_unverified")
            return
        if overall is not None and overall != listed:
            ctx.store.event(ctx.site_id, "overall_total_mismatch", {"overall_total": overall, "listed": listed})
            bad.append("overall_total")
        unverified = [r["segment_key"] for r in rows if r["status"] == "unverified"]
        if bad:
            self.outcome = ("complete_with_mismatch", "segments:" + ",".join(bad[:20]))
        elif unverified:
            self.outcome = ("complete_unverified", "unverified_segments:" + ",".join(unverified[:20]))
        elif check and overall is None:
            self.outcome = ("complete_unverified", "overall_total_unavailable")
        elif boundary and not check:
            # Per-segment totals cannot reveal items lost at range boundaries (inclusive vs
            # exclusive end) or outside every range; only the unfiltered total can.
            self.outcome = ("complete_unverified", "overall_total_check_disabled"
                            if self.plan.raw.get("overall_total_check") is False else "overall_total_not_checked")
        else:
            self.outcome = ("complete", "all_segments_reconciled")

    def overall_check(self):
        """(check, boundary): ``boundary`` when range filters plus a reported total make the
        overall total the only evidence against boundary losses. The check defaults to on
        there (unless the listing URL needs a segment); an explicit plan value wins."""
        flt = self.cfg.get("filter") or {}
        boundary = (self.cfg.get("mode", "range") != "explicit" and flt.get("type", "range") == "range"
                    and bool(self.listing.get("total")))
        explicit = self.plan.raw.get("overall_total_check")
        if explicit is not None:
            return bool(explicit), boundary
        return boundary and "{segment}" not in self.listing["url"], boundary


# ---------------------------------------------------------------------------
# sitemap
# ---------------------------------------------------------------------------
def _tag(element):
    return element.tag.rsplit("}", 1)[-1]


SITEMAP_MAX_BYTES = 64 * 1024 * 1024  # uncompressed; the protocol limit is 50 MB
# A 200 answer that is not a sitemap (maintenance/error HTML, truncated body) may be
# fixed by the next run: such segments are re-queued, at most ``max_attempts`` times.
_RETRYABLE_SITEMAP = ("invalid_xml", "unknown_xml_root")


class _SitemapTooLarge(Exception):
    pass


class Sitemap(Strategy):
    name = "sitemap"
    CHUNK = 200

    def __init__(self, ctx):
        super().__init__(ctx)
        self.cfg = ctx.plan.section("sitemap")
        self.include = [re.compile(p) for p in self.cfg.get("include", [])]
        self.exclude = [re.compile(p) for p in self.cfg.get("exclude", [])]
        self.index_include = [re.compile(p) for p in self.cfg.get("index_include", [])]
        self.order_re = re.compile(self.cfg["order_by_url_number"]) if self.cfg.get("order_by_url_number") else None

    def _load(self, url):
        resp = self.ctx.fetcher.get(url)
        body = resp.body
        if body[:2] == b"\x1f\x8b":
            cap = int(self.cfg.get("max_uncompressed_bytes") or SITEMAP_MAX_BYTES)
            out, data = b"", body
            while data:  # every gzip member (concatenated members are valid gzip)
                inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
                out += inflater.decompress(data, cap + 1 - len(out))
                if len(out) > cap:
                    raise _SitemapTooLarge()
                if not inflater.eof:
                    raise EOFError("truncated gzip sitemap")
                data = inflater.unused_data
            body = out
        return ET.fromstring(body)

    def roots(self):
        if self.cfg.get("sitemap_url"):
            return [self.cfg["sitemap_url"]]
        if self.cfg.get("from_robots"):
            base = self.cfg.get("robots_url") or self.plan.base_url.rstrip("/") + "/robots.txt"
            text = self.ctx.fetcher.get(base).text()
            urls = re.findall(r"(?im)^\s*Sitemap:\s*(\S+)", text)
            if not urls:
                raise StrategyStop("config_error", "robots_has_no_sitemap")
            return urls
        raise StrategyStop("config_error", "sitemap_url_missing")

    def wanted(self, loc):
        if self.include and not any(p.search(loc) for p in self.include):
            return False
        return not any(p.search(loc) for p in self.exclude)

    def batches(self):
        ctx = self.ctx
        # Roots are re-derived and upserted on every run (crash-safe initialisation).
        for i, url in enumerate(self.roots()):
            ctx.store.ensure_segment(ctx.site_id, ctx.plan_fp, url, ordinal=i)
        max_attempts = int(self.cfg.get("max_attempts", 3))
        for r in ctx.store.segments(ctx.site_id, ctx.plan_fp):
            if r["status"] == "failed" and (r["evidence"] or "").startswith(_RETRYABLE_SITEMAP) \
                    and int((r["detail"] or {}).get("attempts", 1)) < max_attempts:
                ctx.store.update_segment(ctx.site_id, ctx.plan_fp, r["segment_key"], status="pending")
        while True:
            rows = ctx.store.segments(ctx.site_id, ctx.plan_fp)
            pending = [r for r in rows if r["status"] in ("pending", "running")]
            if not pending:
                break
            row = pending[0]
            url = row["segment_key"]
            try:
                root = self._load(url)
            except PermanentFailure as exc:
                ctx.store.update_segment(ctx.site_id, ctx.plan_fp, url, status="failed", evidence=exc.reason)
                ctx.store.event(ctx.site_id, "sitemap_failed", {"sitemap": url, "reason": exc.reason})
                continue
            except _SitemapTooLarge:
                ctx.store.update_segment(ctx.site_id, ctx.plan_fp, url, status="failed", evidence="sitemap_too_large")
                ctx.store.event(ctx.site_id, "sitemap_failed", {"sitemap": url, "reason": "sitemap_too_large"})
                continue
            except (ET.ParseError, OSError, EOFError, zlib.error) as exc:
                # Retried by later runs (bounded): a maintenance page is not a permanent answer.
                attempts = int((row["detail"] or {}).get("attempts", 0)) + 1
                ctx.store.update_segment(ctx.site_id, ctx.plan_fp, url, status="failed",
                                         evidence=f"invalid_xml:{type(exc).__name__}",
                                         detail=dict(row["detail"] or {}, attempts=attempts))
                ctx.store.event(ctx.site_id, "sitemap_failed", {"sitemap": url, "reason": "invalid_xml",
                                                                "attempts": attempts})
                continue
            if _tag(root) == "sitemapindex":
                base = max((r["ordinal"] for r in rows), default=0) + 1
                children, listed_children = [], 0
                for node in root:
                    loc = next(((c.text or "").strip() for c in node if _tag(c) == "loc"), "")
                    listed_children += bool(loc)
                    if loc and (not self.index_include or any(p.search(loc) for p in self.index_include)):
                        children.append(loc)
                for i, child in enumerate(children):
                    ctx.store.ensure_segment(ctx.site_id, ctx.plan_fp, child, ordinal=base + i, parent=url)
                # An index naming no sitemap at all is not evidence of an empty source.
                ctx.store.update_segment(ctx.site_id, ctx.plan_fp, url,
                                         status="complete" if listed_children else "unverified",
                                         evidence="sitemap_index" if listed_children else "sitemap_index_empty",
                                         reported_total=len(children), detail={"children": len(children)})
                continue
            if _tag(root) != "urlset":
                attempts = int((row["detail"] or {}).get("attempts", 0)) + 1
                ctx.store.update_segment(ctx.site_id, ctx.plan_fp, url, status="failed", evidence="unknown_xml_root",
                                         detail=dict(row["detail"] or {}, attempts=attempts))
                ctx.store.event(ctx.site_id, "sitemap_failed", {"sitemap": url, "reason": "unknown_xml_root",
                                                                "attempts": attempts})
                continue
            items, locs = [], 0
            for node in root:
                fields = {_tag(c): (c.text or "").strip() for c in node}
                loc = fields.get("loc")
                locs += bool(loc)
                if loc and self.wanted(loc):
                    # lastmod is a modification time, never a publication order.
                    items.append(Item(key=normalize_key_url(loc), url=loc, date=None,
                                      payload={"detail_url": loc, "sitemap_lastmod": fields.get("lastmod")}))
            if self.order_re:
                def number(item):
                    m = self.order_re.search(item.url)
                    return (0, int(m.group(1))) if m else (1, 0)
                items.sort(key=number)
            total = len(items)
            content_fp = hashlib.sha256("\n".join(i.key for i in items).encode()).hexdigest()
            cursor = row["cursor"] or {}
            offset = cursor.get("offset", 0)
            if cursor and cursor.get("fp") != content_fp:
                # The sitemap changed since the stored offset: an offset into the new
                # list is meaningless. Restart; saved URLs are skipped via the ledger.
                ctx.store.event(ctx.site_id, "sitemap_changed_restart", {"sitemap": url, "offset": offset,
                                                                         "total": total})
                offset = 0
            ctx.store.update_segment(ctx.site_id, ctx.plan_fp, url, status="running", reported_total=total)
            while offset < total:
                chunk = items[offset:offset + self.CHUNK]
                offset += len(chunk)
                done = offset >= total
                yield Batch(url, chunk, {"offset": offset, "fp": content_fp}, [i.key for i in chunk],
                            segment_status="complete" if done else None,
                            evidence="sitemap_urls_listed" if done else None, reported_total=total)
            if total:
                listed = ctx.store.listed_in_segment(ctx.site_id, ctx.plan_fp, url, [i.key for i in items])
                if len(listed) != len({i.key for i in items}):
                    ctx.store.update_segment(ctx.site_id, ctx.plan_fp, url, status="mismatch",
                                             evidence=f"listed_{len(listed)}_of_{total}")
            if total == 0:
                # URLs present but none wanted: a filtering result. No URL at all: unverified
                # (an emptied or placeholder sitemap is not evidence of an empty source).
                ctx.store.update_segment(ctx.site_id, ctx.plan_fp, url, status="complete" if locs else "unverified",
                                         evidence="sitemap_no_matching_urls" if locs else "sitemap_empty_urlset",
                                         reported_total=0)
        rows = ctx.store.segments(ctx.site_id, ctx.plan_fp)
        failed = [r["segment_key"] for r in rows if r["status"] == "failed"]
        mismatched = [r["segment_key"] for r in rows if r["status"] == "mismatch"]
        unverified = [r["segment_key"] for r in rows if r["status"] == "unverified"]
        if failed:
            self.outcome = ("incomplete", f"sitemaps_failed:{len(failed)}")
        elif mismatched:
            self.outcome = ("complete_with_mismatch", f"sitemaps_mismatch:{len(mismatched)}")
        elif ctx.store.listed_count(ctx.site_id, ctx.plan_fp) == 0:
            self.outcome = ("complete_unverified", "sitemaps_listed_nothing")
        elif unverified:
            self.outcome = ("complete_unverified", f"sitemaps_unverified:{len(unverified)}")
        else:
            self.outcome = ("complete", "all_sitemaps_listed")


STRATEGIES = {cls.name: cls for cls in (LastPageBackward, DateRangeSplit, OldestFirstSort, Sitemap)}


def today_from(value=None):
    if value is None:
        return date.today()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)).date()

"""The four enumeration strategies.

Each strategy is a generator of ``Batch`` objects. A batch lists items in
oldest-first order and carries the cursor that becomes durable only after the
runner has saved (or ledgered) every item of the batch. Completion is reported
through ``self.outcome`` and only with evidence: source exhaustion or a
reconciled segment total. Budgets/caps never produce a completed outcome.
"""
from __future__ import annotations

import calendar
import gzip
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime

from .http import PermanentFailure
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

    def fetch_page(self, url):
        try:
            resp = self.ctx.fetcher.get(url, accept_statuses=tuple(self.listing.get("end_statuses", ())))
        except PermanentFailure as exc:
            return Page([], None, exc.status or 0, {"error": exc.reason})
        if resp.status in tuple(self.listing.get("end_statuses", ())):
            return Page([], None, resp.status, {"end_status": resp.status})
        return self.ctx.parser.parse(resp)

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
        return None

    def find_last_page(self, first):
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

    def batches(self):
        ctx, seg = self.ctx, self.SEGMENT
        row = self.segment_row(seg)
        cursor = row["cursor"] or {}
        s = self.start_page
        if "next_page" not in cursor:
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
        while k >= s:
            self.cache.clear()
            page = self.get(k)
            if not page.items:
                ctx.store.event(ctx.site_id, "empty_page_in_range", {"page": k, "status": page.status})
            pages = [page]
            seen_now = set(page.keys)
            n = k + 1
            # Re-read the next-older page(s) after reading k: items pushed from k
            # to k+1 by insertions at the top are caught here. Keep walking while
            # a re-read page holds nothing already seen (shift > one page).
            while overlap and n - k <= walk_limit:
                if n > last and bounded_end:
                    break
                older = self.get(n)
                pages.insert(0, older)
                seen_before = self.known(seg, older.keys) | (seen_now & set(older.keys))
                seen_now |= set(older.keys)
                if not older.items or (n - k >= overlap and seen_before):
                    break
                if n - k == walk_limit:
                    ctx.store.event(ctx.site_id, "overlap_walk_limit", {"page": k, "walked_to": n})
                n += 1
            ordered = []
            for p in pages:
                ordered.extend(reversed(p.items))
            listed = [i.key for p in pages for i in p.items]
            cursor = dict(cursor, next_page=k - 1)
            if page.total is not None:
                # The total seen on the final (start) page read is the one that
                # matches the set of items listed by the end of the pass.
                cursor["last_total"] = page.total
            done = k == s
            yield Batch(seg, ordered, cursor, listed,
                        segment_status="complete" if done else None,
                        evidence=f"last_page_{cursor['end_kind']}_reached_start" if done else None)
            k -= 1
        if cursor["end_kind"] == "page_cap":
            self.outcome = ("incomplete", "page_cap_reached")
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
            if n == self.start_page and page.total is not None:
                cursor["total"] = page.total
            elif page.total is not None and cursor.get("total") is None:
                cursor["total"] = page.total
            if not page.items and n == self.start_page:
                return "empty_first_page", cursor.get("total")
            if not page.items:
                kind = (f"end_status_{page.status}" if page.extra.get("end_status") else
                        "not_found" if page.status in (404, 410) else "empty_page")
                return kind, cursor.get("total")
            fp = page.fingerprint
            if n > self.start_page and fp in (cursor.get("prev_fp"), cursor.get("first_fp")):
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
        "page_param": "page", "start_page": 1, "end_statuses": [400],
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
        while True:
            try:
                yield next(gen)
            except StopIteration as stop:
                end, total = stop.value
                break
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


def generate_segments(cfg, today):
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
        start, end, gran = _bounds(key)
        end = min(end, self.today)
        if flt.get("type", "range") == "range":
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
        for child in _children(key, self.today):
            ctx.store.ensure_segment(ctx.site_id, ctx.plan_fp, child, ordinal=ordinal, parent=key)
        ctx.store.update_segment(ctx.site_id, ctx.plan_fp, key, status="split")
        ctx.store.event(ctx.site_id, "segment_split", {"segment": key})

    def batches(self):
        ctx = self.ctx
        if not ctx.store.segments(ctx.site_id, ctx.plan_fp):
            for i, key in enumerate(generate_segments(self.cfg, self.today)):
                ctx.store.ensure_segment(ctx.site_id, ctx.plan_fp, key, ordinal=i)
        state = ctx.state
        if self.plan.raw.get("overall_total_check") and state.get("overall_total") is None:
            page = self.fetch_page(self.page_url(self.start_page))
            state["overall_total"] = page.total
            ctx.store.save_state(ctx.site_id, state)
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
            if total is None:
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
        rows = ctx.store.segments(ctx.site_id, ctx.plan_fp)
        bad = [r["segment_key"] for r in rows if r["status"] in ("mismatch", "cap_unsplittable")]
        overall = state.get("overall_total")
        listed = ctx.store.listed_count(ctx.site_id, ctx.plan_fp)
        if listed == 0 and overall != 0:
            self.outcome = ("incomplete", "all_segments_empty_unverified")
            return
        if overall is not None and overall != listed:
            ctx.store.event(ctx.site_id, "overall_total_mismatch", {"overall_total": overall, "listed": listed})
            bad.append("overall_total")
        self.outcome = ("complete_with_mismatch", "segments:" + ",".join(bad[:20])) if bad else \
            ("complete", "all_segments_reconciled")


# ---------------------------------------------------------------------------
# sitemap
# ---------------------------------------------------------------------------
def _tag(element):
    return element.tag.rsplit("}", 1)[-1]


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
            body = gzip.decompress(body)
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
        if not ctx.store.segments(ctx.site_id, ctx.plan_fp):
            for i, url in enumerate(self.roots()):
                ctx.store.ensure_segment(ctx.site_id, ctx.plan_fp, url, ordinal=i)
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
            except (ET.ParseError, OSError, EOFError) as exc:
                ctx.store.update_segment(ctx.site_id, ctx.plan_fp, url, status="failed",
                                         evidence=f"invalid_xml:{type(exc).__name__}")
                ctx.store.event(ctx.site_id, "sitemap_failed", {"sitemap": url, "reason": "invalid_xml"})
                continue
            if _tag(root) == "sitemapindex":
                base = max((r["ordinal"] for r in rows), default=0) + 1
                children = []
                for node in root:
                    loc = next(((c.text or "").strip() for c in node if _tag(c) == "loc"), "")
                    if loc and (not self.index_include or any(p.search(loc) for p in self.index_include)):
                        children.append(loc)
                for i, child in enumerate(children):
                    ctx.store.ensure_segment(ctx.site_id, ctx.plan_fp, child, ordinal=base + i, parent=url)
                ctx.store.update_segment(ctx.site_id, ctx.plan_fp, url, status="complete",
                                         evidence="sitemap_index", reported_total=len(children),
                                         detail={"children": len(children)})
                continue
            if _tag(root) != "urlset":
                ctx.store.update_segment(ctx.site_id, ctx.plan_fp, url, status="failed", evidence="unknown_xml_root")
                continue
            items = []
            for node in root:
                fields = {_tag(c): (c.text or "").strip() for c in node}
                loc = fields.get("loc")
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
            offset = (row["cursor"] or {}).get("offset", 0)
            ctx.store.update_segment(ctx.site_id, ctx.plan_fp, url, status="running", reported_total=total)
            while offset < total:
                chunk = items[offset:offset + self.CHUNK]
                offset += len(chunk)
                done = offset >= total
                yield Batch(url, chunk, {"offset": offset}, [i.key for i in chunk],
                            segment_status="complete" if done else None,
                            evidence="sitemap_urls_listed" if done else None, reported_total=total)
            if total == 0:
                ctx.store.update_segment(ctx.site_id, ctx.plan_fp, url, status="complete",
                                         evidence="sitemap_no_matching_urls", reported_total=0)
        rows = ctx.store.segments(ctx.site_id, ctx.plan_fp)
        failed = [r["segment_key"] for r in rows if r["status"] == "failed"]
        if failed:
            self.outcome = ("incomplete", f"sitemaps_failed:{len(failed)}")
        else:
            self.outcome = ("complete", "all_sitemaps_listed")


STRATEGIES = {cls.name: cls for cls in (LastPageBackward, DateRangeSplit, OldestFirstSort, Sitemap)}


def today_from(value=None):
    if value is None:
        return date.today()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)).date()

"""Incremental mode: collect only new items from a newest-first listing.

The listing is read from the front (start page onward) in the served order.
New items are saved through the normal ``SiteRunner.process_item`` path (same
ledger, adapter, dedup rule and file queue as a full run). The walk stops once
``known_run`` consecutive items are already known, then reads ``overlap_pages``
more pages (a new item there resets the boundary), the same rule as
``crawler.incremental.Policy`` (known_run / overlap_pages / max_pages).

"Known" means: the run's own item ledger already holds the listing key
(saved, or a final failure a rerun would skip), the documents DB returned the
existing row (``existing_document``), or the operator's known-keys file lists
the item (post_number / meta_url exported from the delivery DB).

An incremental run never certifies a full enumeration: it does not touch the
full run's segments/cursor or its site status, except that refusals (access
holds, login review, private destinations, contract stops) are persisted
exactly as in the retry-only path. Its own result is kept in the site state
under ``incremental`` and as an ``incremental_run`` event.

Only listings whose order is newest-first are supported (a leading run of pinned/notice
rows on the first page, older than the row after them, is left out of that check). A plan declares it
with ``"incremental": {"order": "newest_first"}``; a ``last_page_backward``
plan (whose contract is "new items appear on the start page") is accepted
without a declaration only while its listing dates prove the order at run time.
Every other order is refused before any request.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from .strategies import Strategy, StrategyStop
from .urls import normalize_key_url, url_identity

ORDERS = ("newest_first", "oldest_first", "unknown")
SECTION_KEYS = {"known_run", "max_pages", "overlap_pages", "order", "max_order_violations",
                "listing_key_is_post_number"}
# A leading run of pinned/notice rows on the first page (older than the row after them) is
# left out of the order check; at most this many.
MAX_PINNED_PREFIX = 10
# Strategies whose listing is one front-pageable list. date_range_split reads many
# filtered segments and a sitemap has no publication order: neither has a "front".
FRONT_PAGEABLE = ("last_page_backward", "oldest_first_sort")


class IncrementalRefused(ValueError):
    """The plan's listing cannot be read incrementally (nothing was requested)."""


@dataclass
class IncrementalPolicy:
    known_run: int = 50
    max_pages: int = 50
    overlap_pages: int = 1
    max_order_violations: int = 0

    def __post_init__(self):
        if (type(self.known_run) is not int or not 1 <= self.known_run <= 10000
                or type(self.max_pages) is not int or not 1 <= self.max_pages <= 1000
                or type(self.overlap_pages) is not int or not 0 <= self.overlap_pages <= 5
                or type(self.max_order_violations) is not int or self.max_order_violations < 0):
            raise ValueError("invalid incremental policy")

    @classmethod
    def from_plan(cls, plan, *, known_run=None, max_pages=None):
        cfg = plan.section("incremental")
        values = {k: cfg[k] for k in ("known_run", "max_pages", "overlap_pages", "max_order_violations") if k in cfg}
        if known_run is not None:
            values["known_run"] = known_run
        if max_pages is not None:
            values["max_pages"] = max_pages
        return cls(**values)


def validate_section(site_id, raw):
    """plan.json ``incremental`` section (optional). Raises ValueError with the reason."""
    cfg = raw.get("incremental")
    if cfg is None:
        return
    if not isinstance(cfg, dict):
        raise ValueError(f"{site_id}: incremental must be an object")
    unknown = sorted(set(cfg) - SECTION_KEYS)
    if unknown:
        raise ValueError(f"{site_id}: unknown incremental keys {unknown}")
    if "order" in cfg and cfg["order"] not in ORDERS:
        raise ValueError(f"{site_id}: incremental.order must be one of {ORDERS}")
    if not isinstance(cfg.get("listing_key_is_post_number", False), bool):
        raise ValueError(f"{site_id}: incremental.listing_key_is_post_number must be true or false")
    try:
        IncrementalPolicy(**{k: v for k, v in cfg.items() if k not in ("order", "listing_key_is_post_number")})
    except (TypeError, ValueError):
        raise ValueError(f"{site_id}: incremental needs known_run 1..10000, max_pages 1..1000, "
                         f"overlap_pages 0..5, max_order_violations >= 0 (integers)") from None


def support(plan):
    """(order_basis, None) when the plan may run incrementally, else (None, refusal reason).

    order_basis: "declared" (plan says newest_first) or "strategy" (last_page_backward,
    must be proven by listing dates while running)."""
    declared = plan.section("incremental").get("order")
    strategy = plan.strategy
    if strategy not in FRONT_PAGEABLE:
        return None, f"strategy_not_front_pageable:{strategy}"
    if strategy == "oldest_first_sort":
        return None, "listing_order_oldest_first"
    if declared == "newest_first":
        return "declared", None
    if declared in ("oldest_first", "unknown"):
        return None, f"listing_order_{declared}"
    return "strategy", None


class KnownKeys:
    """Operator-provided identities already delivered (exported from the delivery DB).

    File: CSV with a header (site_id optional, post_number, meta_url) or JSON lines
    with the same fields. Rows for other site ids are ignored.

    Matching follows the store's identity: a meta_url (URL identity) or the exact
    (post_number, meta_url) pair. A bare post_number never matches a listing key
    unless the plan declares ``incremental.listing_key_is_post_number`` (page-local or
    numeric listing keys could otherwise collide with another document's number)."""

    def __init__(self, post_numbers=(), meta_urls=(), *, rows=None):
        rows = list(rows) if rows is not None else list(zip(post_numbers, meta_urls))
        clean = [((str(p).strip() if p is not None else ""), (u or "").strip()) for p, u in rows]
        self.post_numbers = {p for p, _ in clean if p}
        self.urls = {url_identity(u) for _, u in clean if u}
        self.pairs = {(p, url_identity(u)) for p, u in clean if p and u}
        self.key_is_post_number = False

    def __len__(self):
        return len(self.post_numbers) + len(self.urls)

    @classmethod
    def load(cls, path, site_id):
        path = Path(path)
        rows = []
        with open(path, encoding="utf-8-sig", newline="") as fh:
            if path.suffix.lower() in (".jsonl", ".ndjson"):
                for n, line in enumerate(fh, 1):
                    if line.strip():
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            raise ValueError(f"known keys line {n}: not an object")
                        rows.append(row)
            else:
                reader = csv.DictReader(fh)
                if not reader.fieldnames or not {"post_number", "meta_url"} & set(reader.fieldnames):
                    raise ValueError("known keys CSV needs a post_number and/or meta_url column")
                rows = list(reader)
        mine = [r for r in rows if not r.get("site_id") or r.get("site_id") == site_id]
        return cls(rows=[(r.get("post_number"), r.get("meta_url")) for r in mine])

    def listing_match(self, item):
        """Known from the listing alone (no detail request): its URL (or a URL key);
        its key as a post_number only when the plan declares that is what the key is."""
        if not len(self):
            return False
        key = str(item.key)
        if url_identity(item.url) in self.urls:
            return True
        if key.startswith(("http://", "https://")) and url_identity(normalize_key_url(key)) in self.urls:
            return True
        return self.key_is_post_number and key in self.post_numbers

    def identity_match(self, post_number, meta_url):
        """Known after the detail page: the stored meta_url, or the exact (post_number, meta_url)
        pair; post_number alone only under listing_key_is_post_number."""
        url = url_identity(meta_url) if meta_url else None
        pn = str(post_number).strip() if post_number is not None else ""
        if url and (url in self.urls or (pn, url) in self.pairs):
            return True
        return self.key_is_post_number and bool(pn) and pn in self.post_numbers


class FrontWalk(Strategy):
    """Pages of the listing from the start page, in served (newest-first) order.
    Uses the plan's own paging (page numbers, offsets, path paging, POST, next-cursor)."""

    name = "incremental_front"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.end = None

    def pages(self, max_pages):
        cursor_mode = bool(self.listing.get("next_cursor") or self.listing.get("next_link"))
        token, first_fp, prev_fp = None, None, None
        for i in range(max_pages):
            n = self.start_page + i
            if cursor_mode and i and token is None:
                self.end = "source_exhausted"
                return
            request = self.page_url(n, token=token if cursor_mode else None)
            page = self.fetch_page(request)
            if not page.items:
                page = self.fetch_page(request)  # an end candidate is re-read once
            if not page.items:
                if i == 0:
                    # Parser drift, a soft block or a changed listing look the same.
                    raise StrategyStop("incomplete", "empty_first_page_unverified")
                self.end = "source_exhausted"
                return
            fp = page.fingerprint
            if i and fp in (first_fp, prev_fp):
                self.end = "source_exhausted"  # clamp/wrap past the last page
                return
            first_fp = first_fp or fp
            prev_fp = fp
            token = page.extra.get("next_cursor") if cursor_mode else None
            yield page
        self.end = "page_cap"


def known_in_ledger(runner, item):
    row = runner.store.item(runner.site_id, item.key)
    if row is None:
        return False
    if row["status"] in ("saved", "failed_permanent", "failed_retries_exhausted"):
        return True
    return row["status"] == "failed_transient" and row["attempts"] >= int(runner.plan.get("item_max_attempts"))


def _saved_identity(runner, seq_id):
    conn = runner.adapter.conn
    import sqlite3
    sql = "SELECT post_number, meta_url FROM documents WHERE seq_id=?"
    if not isinstance(conn, sqlite3.Connection):
        sql = sql.replace("?", "%s")
    row = conn.execute(sql, (seq_id,)).fetchone()
    if row is None:
        return None, None
    return (row["post_number"], row["meta_url"]) if hasattr(row, "keys") else (row[0], row[1])


def pinned_prefix(items):
    """Length of a leading run of pinned/notice rows on the first page: every row before the
    newest-dated row is dated and older than it, the run is at most MAX_PINNED_PREFIX long and
    shorter than the rest of the page, and at least two dated rows remain to be checked.
    Those rows are still collected; they are only left out of the newest-first order check."""
    dated = [(i, it.date) for i, it in enumerate(items) if it.date]
    if len(dated) < 2:
        return 0
    newest_index = max(dated, key=lambda d: d[1])[0]
    # Fewer pinned rows than ordinary ones, and at least two dated ordinary rows left to check:
    # an oldest-first page (newest row last) can never pass as "all pinned".
    if not 0 < newest_index <= MAX_PINNED_PREFIX or newest_index * 2 >= len(items) \
            or sum(1 for i, _ in dated if i >= newest_index) < 2:
        return 0
    head = items[:newest_index]
    if all(it.date and it.date < items[newest_index].date for it in head):
        return newest_index
    return 0


def walk(runner, ctx, policy, known_keys, order_basis):
    """Read the front of the listing; returns (stop_reason, detail)."""
    st, site = runner.store, runner.site_id
    stats = runner.stats
    stats.update(incremental_pages=0, known_streak_max=0, seed_known=0, seed_known_after_detail=0)
    if known_keys is not None:
        known_keys.key_is_post_number = bool(runner.plan.section("incremental").get("listing_key_is_post_number"))
    walker = FrontWalk(ctx)
    streak, overlap, violations, dated, prev_date = 0, None, 0, 0, None
    for page in walker.pages(policy.max_pages):
        stats["incremental_pages"] += 1
        checked = page.items
        if stats["incremental_pages"] == 1:
            pinned = pinned_prefix(page.items)
            if pinned:
                stats["pinned_prefix"] = pinned
                checked = page.items[pinned:]
        # Order evidence before anything on the page is saved: dated items must not get newer.
        for item in checked:
            if not item.date:
                continue
            dated += 1
            if prev_date is not None and item.date > prev_date:
                violations += 1
            prev_date = item.date
        if violations > policy.max_order_violations:
            st.event(site, "incremental_order_violation", {"violations": violations, "page": stats["incremental_pages"]})
            raise StrategyStop("config_error", "incremental_order_not_newest_first")
        if order_basis == "strategy" and stats["incremental_pages"] == 1 and dated < 2:
            # Undeclared order and no dates to prove it: refuse rather than guess.
            raise StrategyStop("config_error", "incremental_order_unverified")
        found_new = False
        for item in page.items:
            stats["listed"] += 1
            if known_keys is not None and known_keys.listing_match(item):
                stats["seed_known"] += 1
                known = True
            elif known_in_ledger(runner, item):
                known = True
                runner.process_item(item, "incremental")  # counted as skipped_known; URL conflicts still ledgered
            else:
                runner.process_item(item, "incremental")
                row = st.item(site, item.key) or {}
                known = row.get("status") == "saved" and row.get("reason") == "existing_document"
                if not known and known_keys is not None and row.get("status") == "saved" and row.get("seq_id"):
                    if known_keys.identity_match(*_saved_identity(runner, row["seq_id"])):
                        stats["seed_known_after_detail"] += 1
                        known = True
            streak = streak + 1 if known else 0
            found_new = found_new or not known
            stats["known_streak_max"] = max(stats["known_streak_max"], streak)
        stats["batches"] += 1
        runner.log(f"[{site}] incremental page {stats['incremental_pages']}: streak {streak}, "
                   f"saved_new {stats['saved_new']}")
        if found_new:
            overlap = None
        elif overlap is not None:
            overlap -= 1
        if overlap is None and streak >= policy.known_run:
            overlap = policy.overlap_pages
        if overlap is not None and overlap <= 0:
            return "known_boundary_reached", {"streak": streak}
    if walker.end == "page_cap":
        # More new items may lie beyond the cap: a resume (full) run closes that gap.
        st.event(site, "incremental_page_cap", {"max_pages": policy.max_pages, "streak": streak})
        return "page_cap_reached", {"streak": streak}
    return "source_exhausted", {"streak": streak}

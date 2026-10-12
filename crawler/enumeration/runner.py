"""SiteRunner: drives one strategy, saves page by page, ledgers failures."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from delivery.scripts.discovery_structure import date_value

from .adapters import AdapterConfigError, AdapterUngatedNetwork
from .http import (AccessBlocked, BudgetExhausted, EnumerationStop, LoginReview, PermanentFailure, RateLimited,
                   TransientFailure)
from .parsing import Item, ListingParser
from .strategies import STRATEGIES, StrategyStop, today_from
from .urls import normalize_key_url


class SaveBroken(EnumerationStop):
    """Consecutive save failures: the save path itself is broken, not one item."""


@dataclass
class Context:
    site_id: str
    plan: object
    plan_fp: str
    store: object
    fetcher: object
    parser: object
    today: object
    state: dict = field(default_factory=dict)


def _rows(conn, sql, args):
    if not isinstance(conn, sqlite3.Connection):
        sql = sql.replace("?", "%s")
    out = []
    for row in conn.execute(sql, args).fetchall():
        out.append(tuple(row[k] for k in row.keys()) if hasattr(row, "keys") else tuple(row))
    return out


def duplicate_rows(conn, site_id):
    """Extra rows per (site_id, post_number): the vendor's
    ``group by site_id, post_number having count(*)>1`` summed as count-1."""
    rows = _rows(conn, "SELECT COUNT(*) FROM documents WHERE site_id=? AND post_number IS NOT NULL "
                       "GROUP BY post_number HAVING COUNT(*)>1", (site_id,))
    return sum(int(r[0]) - 1 for r in rows)


class SiteRunner:
    def __init__(self, plan, store, fetcher, adapter, *, today=None, log=None, reference_conn=None,
                 identity_required=False, identity_sample=5, identity_threshold=1.0):
        self.plan = plan
        self.site_id = plan.site_id
        self.store = store
        self.fetcher = fetcher
        self.adapter = adapter
        self.today = today_from(today)
        self.log = log or (lambda msg: None)
        self.reference_conn = reference_conn
        self.identity_required = identity_required
        self.identity_sample = int(identity_sample)
        self.identity_threshold = float(identity_threshold)
        self.stats = {"batches": 0, "listed": 0, "skipped_known": 0, "saved_new": 0, "saved_existing": 0,
                      "existing_post_number": 0, "failed_permanent": 0, "failed_transient": 0, "retried": 0}
        self._save_errors = 0
        self._attempted = set()

    # -- items ---------------------------------------------------------------
    def process_item(self, item, segment):
        st, site = self.store, self.site_id
        if item.key in self._attempted:
            # Overlap re-reads list items twice; the fetcher already retried.
            self.stats["skipped_known"] += 1
            return
        self._attempted.add(item.key)
        row = st.item(site, item.key)
        max_attempts = int(self.plan.get("item_max_attempts"))
        if row and (row["status"] in ("saved", "failed_permanent", "failed_retries_exhausted")
                    or (row["status"] == "failed_transient" and row["attempts"] >= max_attempts)):
            self.stats["skipped_known"] += 1
            return
        common = dict(url=item.url, segment=segment, date=item.date,
                      payload={"url": item.url, "date": item.date, "title": item.title, "payload": item.payload})
        try:
            paper = self.adapter.materialize(item, self.fetcher)
        except PermanentFailure as exc:
            st.record_item(site, item.key, "failed_permanent", reason=exc.reason, attempt=True, **common)
            self.stats["failed_permanent"] += 1
            return
        except TransientFailure as exc:
            st.record_item(site, item.key, "failed_transient", reason=f"transient:{exc.reason}", attempt=True, **common)
            self.stats["failed_transient"] += 1
            return
        except Exception as exc:  # parser errors never stop the site
            st.record_item(site, item.key, "failed_permanent", reason=f"parse_error:{type(exc).__name__}",
                           attempt=True, **common)
            self.stats["failed_permanent"] += 1
            return
        if not paper or not str(paper.get("title") or "").strip() or not (paper.get("url") or paper.get("meta_url")):
            st.record_item(site, item.key, "failed_permanent", reason="empty_item", attempt=True, **common)
            self.stats["failed_permanent"] += 1
            return
        try:
            seq_id, created, note = self.adapter.save(paper)
        except EnumerationStop:
            raise
        except Exception as exc:
            st.record_item(site, item.key, "failed_permanent", reason=f"save_error:{type(exc).__name__}",
                           attempt=True, **common)
            self.stats["failed_permanent"] += 1
            self._save_errors += 1
            if self._save_errors >= int(self.plan.get("max_consecutive_save_errors")):
                raise SaveBroken(f"save_failures_consecutive:{type(exc).__name__}")
            return
        self._save_errors = 0
        if note == "existing_post_number":
            st.event(site, "duplicate_post_number", {"item": item.key, "existing_seq_id": seq_id,
                                                     "url": paper.get("url") or paper.get("meta_url")})
            self.stats["existing_post_number"] += 1
        else:
            self.stats["saved_new" if created else "saved_existing"] += 1
        # Files are queued before the item is marked saved: a crash in between
        # re-processes the item (dedup returns the same row) instead of losing the PDF.
        if isinstance(seq_id, int) and note is None:
            for url, role in self.adapter.file_urls(paper):
                st.enqueue_file(site, seq_id, url, item_key=item.key, role=role)
        when = item.date or date_value(paper.get("published_date") or "")
        st.record_item(site, item.key, "saved", seq_id=seq_id, reason=note, attempt=True,
                       **dict(common, date=when, payload=None))

    def retry_pass(self):
        max_attempts = int(self.plan.get("item_max_attempts"))
        for row in self.store.retryable_items(self.site_id, max_attempts):
            data = row["payload"] or {}
            item = Item(key=row["item_key"], url=data.get("url") or row["url"], date=data.get("date"),
                        title=data.get("title") or "", payload=data.get("payload") or {})
            self.stats["retried"] += 1
            self.process_item(item, row["segment_key"])
        self.store.exhaust_items(self.site_id, max_attempts)

    # -- identity check against existing rows ---------------------------------
    def identity_precheck(self, strategy):
        """Recompute (post_number, meta_url) for a sample with this framework's code
        path and compare with rows the legacy crawler saved (reference DB)."""
        if not self.identity_required:
            return {"mode": "not_required"}
        ref = self.reference_conn
        if ref is None:
            raise StrategyStop("config_error", "identity_reference_required")
        total = _rows(ref, "SELECT COUNT(*) FROM documents WHERE site_id=?", (self.site_id,))[0][0]
        if not total:
            return {"mode": "no_reference_rows", "reference_rows": 0}
        if self.plan.strategy == "sitemap":
            sample = _rows(ref, "SELECT meta_url FROM documents WHERE site_id=? ORDER BY seq_id DESC LIMIT ?",
                           (self.site_id, self.identity_sample))
            items = [Item(key=normalize_key_url(u), url=u, payload={"detail_url": u}) for (u,) in sample]
        else:
            items = strategy.fetch_page(strategy.page_url(strategy.start_page)).items[: self.identity_sample]
        compared = matched = errors = 0
        examples = []
        for item in items:
            try:
                ident = self.adapter.identity(item, self.fetcher)
            except (PermanentFailure, TransientFailure, ValueError, KeyError, TypeError, AttributeError):
                errors += 1
                continue
            if ident is None:
                errors += 1
                continue
            post_number, meta_url = ident
            rows = _rows(ref, "SELECT post_number, meta_url FROM documents WHERE site_id=? AND (meta_url=? OR "
                              "post_number=?)", (self.site_id, meta_url, post_number))
            if not rows:
                continue
            compared += 1
            if any(str(p) == str(post_number) and m == meta_url for p, m in rows):
                matched += 1
            elif len(examples) < 3:
                examples.append({"framework": [post_number, meta_url], "reference": [list(r) for r in rows[:2]]})
        result = {"mode": "sampled", "reference_rows": total, "sampled": len(items), "compared": compared,
                  "matched": matched, "errors": errors, "threshold": self.identity_threshold,
                  "match_rate": (matched / compared) if compared else None, "mismatch_examples": examples}
        self.store.event(self.site_id, "identity_precheck", result)
        if not compared:
            raise _IdentityStop("identity_unverifiable_no_overlap", result)
        if result["match_rate"] < self.identity_threshold:
            raise _IdentityStop("identity_mismatch", result)
        return result

    # -- run ------------------------------------------------------------------
    def _result(self, status, reason, **extra):
        out = {"site_id": self.site_id, "status": status, "stop_reason": reason, "stats": dict(self.stats),
               "requests": self.fetcher.requests_made}
        out.update(extra)
        return out

    def run(self, *, refresh=False, retry_only=False):
        """Enumerate (or resume). A completed site is only re-enumerated with ``refresh``;
        otherwise (and with ``retry_only``) just the transient item failures are retried."""
        plan, st, site = self.plan, self.store, self.site_id
        current = st.site(site)
        if current and current["status"] == "access_blocked" and not refresh:
            # Refusals persist across runs; a person must review before the host is contacted again.
            return self._result("access_blocked", "access_hold_requires_review")
        cooldown = (current or {}).get("state", {}).get("cooldown_until")
        if cooldown and cooldown > time.time():
            return self._result("incomplete", "cooling_down", cooldown_until=cooldown)
        finished = current and current["plan_fp"] == plan.fingerprint and \
            current["status"] in ("complete", "complete_with_mismatch", "complete_unverified")
        if retry_only or (finished and not refresh):
            try:
                self.retry_pass()
            except EnumerationStop as exc:
                st.event(site, "retry_pass_stopped", {"reason": exc.reason})
                return self._result(current["status"] if current else "not_started",
                                    f"retry_pass_stopped:{exc.reason}")
            return self._result(current["status"] if current else "not_started",
                                "retry_only" if retry_only else "already_complete_retry_only")
        doc_conn = self.adapter.conn
        duplicates_before = duplicate_rows(doc_conn, site)
        state = st.start_site(site, plan.fingerprint, plan.strategy)
        state.pop("cooldown_until", None)
        parser = ListingParser(plan.listing.get("parser"), total_cfg=plan.listing.get("total"), adapter=self.adapter)
        ctx = Context(site, plan, plan.fingerprint, st, self.fetcher, parser, self.today, state)
        strategy = STRATEGIES[plan.strategy](ctx)
        status, reason, identity = "incomplete", None, None
        try:
            identity = self.identity_precheck(strategy)
            self.retry_pass()
            for batch in strategy.batches():
                self.stats["batches"] += 1
                for item in batch.items:
                    self.process_item(item, batch.segment)
                # Items are saved/ledgered above; only now does progress become durable.
                count = st.mark_listed(site, plan.fingerprint, batch.segment, batch.listed_keys)
                self.stats["listed"] += len(batch.listed_keys)
                fields = {"cursor": batch.cursor}
                if batch.reported_total is not None:
                    fields["reported_total"] = batch.reported_total
                if batch.segment_status:
                    fields.update(status=batch.segment_status, evidence=batch.evidence)
                st.update_segment(site, plan.fingerprint, batch.segment, **fields)
                self.log(f"[{site}] {batch.segment}: listed {count}, saved_new {self.stats['saved_new']}")
            status, reason = strategy.outcome
        except _IdentityStop as exc:
            status, reason, identity = "config_error", exc.reason, exc.detail
        except StrategyStop as exc:
            status, reason = exc.status, exc.reason
        except BudgetExhausted as exc:
            status, reason = "incomplete", exc.reason
        except RateLimited as exc:
            status, reason = "incomplete", exc.reason
            ctx.state["cooldown_until"] = exc.retry_at
            st.event(site, "rate_limited_cooldown", {"url": exc.url, "retry_at": exc.retry_at})
        except LoginReview as exc:
            # Not a permanent hold: recorded for review, the next run may proceed.
            status, reason = "review_pending", exc.reason
            st.event(site, "login_form_review", {"url": exc.url})
        except AccessBlocked as exc:
            status, reason = "access_blocked", exc.reason
            st.event(site, "access_blocked", {"reason": exc.reason, "url": exc.url, "status": exc.status})
        except (AdapterUngatedNetwork, SaveBroken) as exc:
            status, reason = "config_error", exc.reason
            st.event(site, "config_error", {"reason": exc.reason})
        except AdapterConfigError as exc:
            status, reason = "config_error", str(exc)
        except TransientFailure as exc:
            status, reason = "incomplete", f"listing_transient:{exc.reason}"
            st.event(site, "listing_transient", {"reason": exc.reason, "url": exc.url})
        except PermanentFailure as exc:
            status, reason = "incomplete", f"listing_failed:{exc.reason}"
            st.event(site, "listing_failed", {"reason": exc.reason, "url": exc.url})
        except KeyboardInterrupt:
            st.finish_site(site, "incomplete", "interrupted", ctx.state)
            raise
        except BaseException as exc:
            st.finish_site(site, "failed", f"runner_error:{type(exc).__name__}", ctx.state)
            raise
        rollback = getattr(doc_conn, "rollback", None)
        if rollback and not isinstance(doc_conn, sqlite3.Connection):
            rollback()  # never leave an aborted PostgreSQL transaction behind
        duplicates = {"before": duplicates_before, "after": duplicate_rows(doc_conn, site)}
        duplicates["new"] = max(0, duplicates["after"] - duplicates["before"])
        ctx.state["duplicates"] = duplicates
        if duplicates["new"] > 0:
            st.event(site, "new_duplicates", duplicates)
            status, reason = "failed_duplicates", f"new_duplicate_rows:{duplicates['new']}"
        if identity is not None:
            ctx.state["identity"] = identity
        st.finish_site(site, status, reason, ctx.state)
        return self._result(status, reason, identity=identity, duplicates=duplicates)


class _IdentityStop(Exception):
    def __init__(self, reason, detail):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail

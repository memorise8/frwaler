"""SiteRunner: drives one strategy, saves page by page, ledgers failures."""
from __future__ import annotations

from dataclasses import dataclass, field

from delivery.scripts.discovery_structure import date_value

from .adapters import AdapterConfigError, AdapterUngatedNetwork
from .http import AccessBlocked, BudgetExhausted, EnumerationStop, PermanentFailure, TransientFailure
from .parsing import Item, ListingParser
from .strategies import STRATEGIES, StrategyStop, today_from


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


class SiteRunner:
    def __init__(self, plan, store, fetcher, adapter, *, today=None, log=None):
        self.plan = plan
        self.site_id = plan.site_id
        self.store = store
        self.fetcher = fetcher
        self.adapter = adapter
        self.today = today_from(today)
        self.log = log or (lambda msg: None)
        self.stats = {"batches": 0, "listed": 0, "skipped_known": 0, "saved_new": 0, "saved_existing": 0,
                      "failed_permanent": 0, "failed_transient": 0, "retried": 0}
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
            seq_id, created = self.adapter.save(paper)
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
        when = item.date or date_value(paper.get("published_date") or "")
        st.record_item(site, item.key, "saved", seq_id=seq_id, reason=None, attempt=True,
                       **dict(common, date=when, payload=None))
        self.stats["saved_new" if created else "saved_existing"] += 1
        if isinstance(seq_id, int):
            for url, role in self.adapter.file_urls(paper):
                st.enqueue_file(site, seq_id, url, item_key=item.key, role=role)

    def retry_pass(self):
        max_attempts = int(self.plan.get("item_max_attempts"))
        for row in self.store.retryable_items(self.site_id, max_attempts):
            data = row["payload"] or {}
            item = Item(key=row["item_key"], url=data.get("url") or row["url"], date=data.get("date"),
                        title=data.get("title") or "", payload=data.get("payload") or {})
            self.stats["retried"] += 1
            self.process_item(item, row["segment_key"])
        self.store.exhaust_items(self.site_id, max_attempts)

    # -- run ------------------------------------------------------------------
    def run(self, *, refresh=False, retry_only=False):
        """Enumerate (or resume). A completed site is only re-enumerated with ``refresh``;
        otherwise (and with ``retry_only``) just the transient item failures are retried."""
        plan, st, site = self.plan, self.store, self.site_id
        current = st.site(site)
        finished = current and current["plan_fp"] == plan.fingerprint and \
            current["status"] in ("complete", "complete_with_mismatch")
        if retry_only or (finished and not refresh):
            try:
                self.retry_pass()
            except EnumerationStop as exc:
                st.event(site, "retry_pass_stopped", {"reason": exc.reason})
                return {"site_id": site, "status": current["status"] if current else "not_started",
                        "stop_reason": f"retry_pass_stopped:{exc.reason}", "stats": dict(self.stats),
                        "requests": self.fetcher.requests_made}
            return {"site_id": site, "status": current["status"] if current else "not_started",
                    "stop_reason": "retry_only" if retry_only else "already_complete_retry_only",
                    "stats": dict(self.stats), "requests": self.fetcher.requests_made}
        state = st.start_site(site, plan.fingerprint, plan.strategy)
        parser = ListingParser(plan.listing.get("parser"), total_cfg=plan.listing.get("total"), adapter=self.adapter)
        ctx = Context(site, plan, plan.fingerprint, st, self.fetcher, parser, self.today, state)
        strategy = STRATEGIES[plan.strategy](ctx)
        status, reason = "incomplete", None
        try:
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
            # Transient failures recorded during this pass get their next attempt next run.
        except StrategyStop as exc:
            status, reason = exc.status, exc.reason
        except BudgetExhausted as exc:
            status, reason = "incomplete", exc.reason
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
        st.finish_site(site, status, reason, ctx.state)
        return {"site_id": site, "status": status, "stop_reason": reason, "stats": dict(self.stats),
                "requests": self.fetcher.requests_made}

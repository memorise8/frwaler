"""SiteRunner: drives one strategy, saves page by page, ledgers failures."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from delivery.scripts.discovery_structure import date_value

from .adapters import (AdapterConfigError, AdapterUngatedNetwork, IdentityCollision, IdentityMissing,
                       IdentityReview, IdentityRuleConfig)
from .http import (AccessBlocked, BudgetExhausted, EnumerationStop, LoginReview, PermanentFailure, PrivateDestination,
                   RateLimited, ROBOTS_RETRY_S, RobotsDisallowed, RobotsUnavailable, TransientFailure)
from .parsing import Item, ListingParser
from .strategies import STRATEGIES, StrategyStop, today_from
from .urls import host_of, normalize_key_url, url_identity


# Ledger reasons of items a contract site's registered methods failed on.
CONTRACT_FAILURE_PREFIXES = ("failed_permanent:parse_error:", "failed_permanent:empty_item",
                             "failed_permanent:adapter_contract:")
# Contract items with no identity/URL: always identity_missing review, never tolerated
# by max_contract_failure_ratio (as IdentityMissing is on the non-contract path).
CONTRACT_IDENTITY_MISSING = ("failed_permanent:adapter_contract:paper_identity_missing",
                             "failed_permanent:adapter_contract:paper_url_missing")

COMPLETED = ("complete", "complete_with_mismatch", "complete_unverified", "complete_with_review")

# A consecutive contract stop. Its items are failed_permanent in the ledger, so a plain
# rerun would skip them as known and move the checkpoint past them: held until requeued.
CONTRACT_STOP = "adapter_contract_failures_consecutive:"


def transient_db_error(exc):
    """Database-side trouble (locks, deadlocks, lost connections), not a bad document."""
    if isinstance(exc, sqlite3.OperationalError):
        return True
    try:
        import psycopg
        from psycopg import errors
    except ImportError:
        return False
    return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError, errors.DeadlockDetected,
                            errors.SerializationFailure, errors.LockNotAvailable))


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
    # NULL post_numbers form one group, exactly like GROUP BY site_id, post_number.
    rows = _rows(conn, "SELECT COUNT(*) FROM documents WHERE site_id=? "
                       "GROUP BY post_number HAVING COUNT(*)>1", (site_id,))
    return sum(int(r[0]) - 1 for r in rows)


class SiteRunner:
    def __init__(self, plan, store, fetcher, adapter, *, today=None, log=None, reference_conn=None,
                 identity_required=False, identity_sample=5, identity_threshold=1.0, allow_empty_reference=False):
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
        self.allow_empty_reference = allow_empty_reference
        self.ignore_params = {plan.listing.get("page_param", "page"),
                              *(plan.raw.get("url_identity_ignore_params") or ())}
        self.warnings = []
        self._login_details = 0
        self._robots_details = 0
        self._identity_reviews = 0
        self.stats = {"batches": 0, "listed": 0, "skipped_known": 0, "saved_new": 0, "saved_existing": 0,
                      "existing_document": 0, "identity_collisions": 0, "listing_identity_conflicts": 0,
                      "identity_missing": 0,
                      "identity_reviews": 0, "failed_permanent": 0, "failed_transient": 0, "retried": 0}
        self._save_errors = 0
        self._db_errors = 0
        self._attempted = {}
        # Opt-in adapter contract only: registered-method failures are counted so a
        # broken binding can never end as a plain "complete".
        self.contract = getattr(adapter, "contract", None) is not None
        self._contract_failures = 0
        self._contract_streak = []

    def contract_failure(self, item, reason):
        """A contract site's registered method failed for one item (already ledgered)."""
        if not self.contract:
            return
        self.store.event(self.site_id, "adapter_contract_item_failed", {"item": item.key, "reason": reason})
        self._contract_failures += 1
        self._contract_streak.append(item.key)
        if self._contract_failures >= int(self.plan.get("max_consecutive_contract_failures") or 5):
            raise StrategyStop("config_error", f"adapter_contract_failures_consecutive:{reason}")

    # -- items ---------------------------------------------------------------
    def segment_info(self, key):
        """{key, label, params} of the segment an item was listed in (explicit or discovered)."""
        seg = self.plan.section("segments") if self.plan.strategy == "date_range_split" else {}
        if seg.get("mode") == "explicit":
            spec = next((s for s in seg.get("items") or () if str(s["key"]) == key), {})
        elif seg.get("mode") == "discover":
            if getattr(self, "_discovered", None) is None:
                current = self.store.site(self.site_id) or {}
                self._discovered = (current.get("state") or {}).get("discovered_segments") or {}
            spec = self._discovered.get(key, {})
        else:
            spec = {}
        return {"key": key, "label": spec.get("label"), "params": dict(spec.get("params") or {})}

    def process_item(self, item, segment):
        st, site = self.store, self.site_id
        seen_url = self._attempted.get(item.key)
        row = st.item(site, item.key)
        other = seen_url if seen_url is not None else (row or {}).get("url")
        if other and not self.same_document_url(other, item.url):
            # Same listing identity, different document URL: keep the candidate in
            # the ledger for review instead of silently dropping it.
            self.listing_conflict(item, segment, other)
            return
        if seen_url is not None:
            # Overlap re-reads list items twice; the fetcher already retried.
            self.stats["skipped_known"] += 1
            return
        self._attempted[item.key] = item.url
        max_attempts = int(self.plan.get("item_max_attempts"))
        if row and (row["status"] in ("saved", "failed_permanent", "failed_retries_exhausted")
                    or (row["status"] == "failed_transient" and row["attempts"] >= max_attempts)):
            self.stats["skipped_known"] += 1
            return
        common = dict(url=item.url, segment=segment, date=item.date,
                      payload={"url": item.url, "date": item.date, "title": item.title, "payload": item.payload})
        item.segment = self.segment_info(segment)  # ext:multi_listing ("segment" argument)
        try:
            paper = self.adapter.materialize(item, self.fetcher)
        except RobotsDisallowed as exc:
            # One detail URL the site's robots.txt excludes: skip that item, keep the site going.
            # Consecutive exclusions mean robots.txt covers the detail pages: stop the site.
            st.record_item(site, item.key, "failed_permanent", reason=exc.reason, attempt=True, **common)
            self.stats["failed_permanent"] += 1
            self._robots_details += 1
            if self._robots_details >= int(self.plan.get("max_consecutive_robots_disallowed")):
                # Not "robots_disallowed": that names a listing URL robots.txt itself denies.
                raise AccessBlocked("robots_disallowed_consecutive", item.url) from None
            return
        except PermanentFailure as exc:
            st.record_item(site, item.key, "failed_permanent", reason=exc.reason, attempt=True, **common)
            self.stats["failed_permanent"] += 1
            if exc.reason == "login_required_suspect":
                self._login_details += 1
                limit = int(self.plan.get("max_consecutive_login_details") or 5)
                if self._login_details >= limit:
                    raise LoginReview("login_required_details", item.url)
            return
        except TransientFailure as exc:
            st.record_item(site, item.key, "failed_transient", reason=f"transient:{exc.reason}", attempt=True, **common)
            self.stats["failed_transient"] += 1
            return
        except AdapterConfigError as exc:
            if not (self.contract and getattr(exc, "data_error", False)):
                raise  # a configuration mistake stops the site; never ledgered per item
            # One item lacks a declared value (contract sites only): ledger it for review;
            # the consecutive/ratio rules decide whether the whole site is broken.
            st.record_item(site, item.key, "failed_permanent", reason=str(exc), attempt=True, **common)
            self.stats["failed_permanent"] += 1
            self.contract_failure(item, str(exc))
            return
        except Exception as exc:  # parser errors never stop the site
            st.record_item(site, item.key, "failed_permanent", reason=f"parse_error:{type(exc).__name__}",
                           attempt=True, **common)
            self.stats["failed_permanent"] += 1
            self.contract_failure(item, f"parse_error:{type(exc).__name__}")
            return
        if not paper or not str(paper.get("title") or "").strip() or not (paper.get("url") or paper.get("meta_url")):
            st.record_item(site, item.key, "failed_permanent", reason="empty_item", attempt=True, **common)
            self.stats["failed_permanent"] += 1
            self.contract_failure(item, "empty_item")
            return
        self._contract_failures = 0
        self._contract_streak = []
        self._login_details = 0
        self._robots_details = 0
        try:
            seq_id, created, note = self.adapter.save(paper)
        except EnumerationStop:
            raise
        except IdentityReview as exc:
            st.record_item(site, item.key, "failed_permanent", reason=f"identity_review:{exc}", attempt=True, **common)
            st.event(site, "identity_review_required", {"item": item.key, "rule": str(exc),
                                                        "url": paper.get("url") or paper.get("meta_url")})
            self.stats["identity_reviews"] += 1
            self.stats["failed_permanent"] += 1
            self._identity_reviews += 1
            if self._identity_reviews >= int(self.plan.get("max_consecutive_identity_reviews") or 5):
                raise LoginReview("identity_reviews_consecutive", paper.get("url") or paper.get("meta_url"))
            return
        except IdentityMissing:
            st.record_item(site, item.key, "failed_permanent", reason="identity_missing", attempt=True, **common)
            st.event(site, "identity_missing", {"item": item.key, "url": paper.get("url") or paper.get("meta_url")})
            self.stats["identity_missing"] += 1
            self.stats["failed_permanent"] += 1
            return
        except IdentityCollision as exc:
            st.record_item(site, item.key, "failed_permanent", reason="identity_collision", attempt=True, **common)
            st.event(site, "identity_collision", {"item": item.key, "post_number": str(exc),
                                                  "url": paper.get("url") or paper.get("meta_url")})
            self.stats["identity_collisions"] += 1
            self.stats["failed_permanent"] += 1
            return
        except Exception as exc:
            if transient_db_error(exc):
                # Lock/deadlock/connection trouble is about the database, not the
                # document: retry later instead of freezing a permanent failure.
                st.record_item(site, item.key, "failed_transient", reason=f"save_transient:{type(exc).__name__}",
                               attempt=True, **common)
                self.stats["failed_transient"] += 1
                self._db_errors += 1
                if self._db_errors >= int(self.plan.get("max_consecutive_save_errors")):
                    raise StrategyStop("incomplete", f"database_unavailable:{type(exc).__name__}")
                return
            st.record_item(site, item.key, "failed_permanent", reason=f"save_error:{type(exc).__name__}",
                           attempt=True, **common)
            self.stats["failed_permanent"] += 1
            self._save_errors += 1
            if self._save_errors >= int(self.plan.get("max_consecutive_save_errors")):
                raise SaveBroken(f"save_failures_consecutive:{type(exc).__name__}")
            return
        self._save_errors = 0
        self._db_errors = 0
        self._identity_reviews = 0
        if note == "existing_document":
            self.stats["existing_document"] += 1
        else:
            self.stats["saved_new" if created else "saved_existing"] += 1
        # Files are queued before the item is marked saved. A crash anywhere
        # before the ledger write re-processes the item: the dedup rule returns
        # the same row and its files are queued again (ON CONFLICT DO NOTHING).
        if isinstance(seq_id, int):
            for url, role in self.adapter.file_urls(paper):
                st.enqueue_file(site, seq_id, url, item_key=item.key, role=role)
        when = item.date or date_value(paper.get("published_date") or "")
        st.record_item(site, item.key, "saved", seq_id=seq_id, reason=note, attempt=True,
                       **dict(common, date=when, payload=None))

    def same_document_url(self, a, b):
        """URL equality for one listing identity, ignoring list-state parameters
        (page numbers etc.) that boards embed in detail links."""
        return url_identity(a, self.ignore_params) == url_identity(b, self.ignore_params)

    def listing_conflict(self, item, segment, other_url):
        import hashlib
        key = f"{item.key}#conflict:{hashlib.sha256(item.url.encode()).hexdigest()[:12]}"
        if self.store.item(self.site_id, key) is None:
            self.store.record_item(self.site_id, key, "failed_permanent", reason="listing_identity_conflict",
                                   url=item.url, segment=segment, date=item.date, attempt=True,
                                   payload={"url": item.url, "listing_key": item.key, "other_url": other_url,
                                            "title": item.title, "payload": item.payload})
            self.store.event(self.site_id, "listing_identity_conflict",
                             {"listing_key": item.key, "url": item.url, "other_url": other_url})
        self.stats["listing_identity_conflicts"] += 1

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
        path and check it against rows the legacy crawler saved (reference DB),
        using the backend's dedup rule (KRIVET/KEI/CSO aware)."""
        if not self.identity_required:
            return {"mode": "not_required"}
        ref = self.reference_conn
        if ref is None:
            raise StrategyStop("config_error", "identity_reference_required")
        total = _rows(ref, "SELECT COUNT(*) FROM documents WHERE site_id=?", (self.site_id,))[0][0]
        if not total:
            if not self.allow_empty_reference:
                # A wrong reference DB or a site_id spelling difference must not pass silently.
                raise _IdentityStop("identity_reference_empty", {"mode": "no_reference_rows", "reference_rows": 0})
            self.warnings.append("identity_reference_empty_allowed")
            self.store.event(self.site_id, "identity_reference_empty_allowed", {})
            return {"mode": "no_reference_rows_allowed", "reference_rows": 0}
        from crawler.db_backend import get_backend
        backend = get_backend("sqlite" if isinstance(ref, sqlite3.Connection) else "postgres")
        # Sample from the legacy rows themselves (newest first): the first page of an
        # oldest-first listing holds items the legacy crawler never saved.
        sample = _rows(ref, "SELECT seq_id, post_number, meta_url FROM documents WHERE site_id=? "
                            "ORDER BY seq_id DESC LIMIT ?", (self.site_id, self.identity_sample))
        items = [(seq, Item(key=normalize_key_url(url), url=url, payload={"detail_url": url}))
                 for seq, _post, url in sample]
        source = "reference_rows"
        compared = matched = errors = contract_data_errors = 0
        examples = []
        list_field = self.adapter.identity_uses_list_fields()

        def compare(expected_seq, item, reference=False):
            nonlocal compared, matched, errors, contract_data_errors
            try:
                ident = self.adapter.identity(item, self.fetcher)
            except AdapterConfigError as exc:
                if not self.contract:
                    errors += 1  # non-contract sites: a sample error, exactly as before the contract
                    return
                # ContractError is a ValueError: never let it pass as a mere sample error.
                if not getattr(exc, "data_error", False):
                    raise
                contract_data_errors += 1
                if reference and list_field:
                    errors += 1  # a reference URL cannot rebuild the listing fields
                    return
                compared += 1  # a comparable item the contract cannot identify: a mismatch
                if len(examples) < 3:
                    examples.append({"framework": None, "contract_error": str(exc), "reference_seq": expected_seq})
                return
            except (PermanentFailure, TransientFailure, RobotsDisallowed, ValueError, KeyError, TypeError,
                    AttributeError):
                errors += 1
                return
            if ident is None:
                errors += 1
                return
            post_number, meta_url = ident
            try:
                found = backend.find_by_dedup_key(ref, self.site_id, post_number, meta_url)
            except ValueError as exc:
                found = f"review:{exc}"
            if expected_seq is None and found is None:
                # No dedup match: comparable (as a mismatch) only if a stored row shares
                # the meta_url or the post_number; otherwise the legacy crawler never saved it.
                near = _rows(ref, "SELECT seq_id FROM documents WHERE site_id=? AND (meta_url=? OR post_number=?) "
                                  "LIMIT 1", (self.site_id, meta_url, post_number))
                if not near:
                    return
            compared += 1
            # Only a real row id is a match; a "review:..." outcome never is.
            if isinstance(found, int) and (expected_seq is None or found == expected_seq):
                matched += 1
            elif len(examples) < 3:
                examples.append({"framework": [post_number, meta_url], "reference_seq": expected_seq,
                                 "dedup_match": found})

        for seq, item in items:
            compare(seq, item, reference=True)
        listing_items = []
        if not compared and self.plan.strategy != "sitemap":
            # Crawlers whose identity needs list fields cannot rebuild it from a URL.
            source = "listing_start_page"
            listing_items = strategy.fetch_page(strategy.page_url(strategy.start_page)).items[: self.identity_sample]
            for item in listing_items:
                compare(None, item)
        result = {"mode": "sampled", "source": source, "reference_rows": total,
                  "sampled_reference": len(items), "sampled_listing": len(listing_items),
                  "sampled": len(items) + len(listing_items), "list_field_identity": list_field,
                  "compared": compared, "matched": matched, "errors": errors, "threshold": self.identity_threshold,
                  "match_rate": (matched / compared) if compared else None, "mismatch_examples": examples}
        if self.contract:
            result["contract_data_errors"] = contract_data_errors
        self.store.event(self.site_id, "identity_precheck", result)
        if not compared:
            raise _IdentityStop("identity_unverifiable_no_overlap", result)
        if result["match_rate"] < self.identity_threshold:
            # When identity depends on listing fields, rebuilding it from a reference
            # URL alone can be wrong: say so, so a false alarm can be told apart.
            raise _IdentityStop("identity_mismatch:sample_recompute_may_be_wrong" if list_field
                                else "identity_mismatch", result)
        return result

    # -- run ------------------------------------------------------------------
    def _result(self, status, reason, **extra):
        out = {"site_id": self.site_id, "status": status, "stop_reason": reason, "stats": dict(self.stats),
               "requests": self.fetcher.requests_made}
        out.update(extra)
        return out

    def _stop_status(self, exc, state):
        """Map a stop to (status, reason), recording holds/cooldowns. Used by every path
        (full run and retry-only) so a refusal is never forgotten."""
        st, site = self.store, self.site_id
        if isinstance(exc, _IdentityStop):
            return "config_error", exc.reason
        if isinstance(exc, StrategyStop):
            if self.contract and str(exc.reason).startswith(CONTRACT_STOP):
                # The items that caused the stop; the hold lasts until they are requeued.
                state["adapter_contract_stop_items"] = list(self._contract_streak)
            return exc.status, exc.reason
        if isinstance(exc, BudgetExhausted):
            return "incomplete", exc.reason
        if isinstance(exc, RateLimited):
            state["cooldown_until"] = exc.retry_at
            st.event(site, "rate_limited_cooldown", {"url": exc.url, "retry_at": exc.retry_at, "reason": exc.reason})
            return "incomplete", exc.reason
        if isinstance(exc, RobotsUnavailable):
            # No permission can be read: stop (resumable), never a host hold.
            st.event(site, "robots_unavailable", {"reason": exc.reason, "url": exc.url, "status": exc.status})
            # Persisted, so the next run does not ask a failing robots.txt again right away.
            state["cooldown_until"] = time.time() + ROBOTS_RETRY_S
            return "incomplete", exc.reason
        if isinstance(exc, LoginReview):
            st.event(site, "login_form_review", {"url": exc.url, "reason": exc.reason})
            return "review_pending", exc.reason
        if isinstance(exc, PrivateDestination):
            # Not a host hold: an internal address says nothing about the source's consent.
            st.event(site, "private_destination_blocked", {"reason": exc.reason, "url": exc.url, "host": exc.host,
                                                           "address": exc.address})
            return "private_destination_blocked", exc.reason
        if isinstance(exc, AccessBlocked):
            st.event(site, "access_blocked", {"reason": exc.reason, "url": exc.url, "status": exc.status})
            return "access_blocked", exc.reason
        if isinstance(exc, (AdapterUngatedNetwork, SaveBroken, IdentityRuleConfig)):
            st.event(site, "config_error", {"reason": exc.reason})
            return "config_error", exc.reason
        if isinstance(exc, AdapterConfigError):
            return "config_error", str(exc)
        if isinstance(exc, TransientFailure):
            st.event(site, "listing_transient", {"reason": exc.reason, "url": exc.url})
            return "incomplete", f"listing_transient:{exc.reason}"
        if isinstance(exc, PermanentFailure):
            st.event(site, "listing_failed", {"reason": exc.reason, "url": exc.url})
            return "incomplete", f"listing_failed:{exc.reason}"
        return None

    def record_timing(self, state, *, save=True):
        """One aggregated request-timing event per run (and the latest copy in the site state),
        so slow sites (Crawl-delay vs slow responses) can be told apart from the state DB."""
        summary = self.fetcher.timing_summary(reset=True) if hasattr(self.fetcher, "timing_summary") else None
        if not summary or not summary["hosts"]:
            return
        state["request_timing"] = summary
        self.store.event(self.site_id, "request_timing", summary)
        if save:
            self.store.save_state(self.site_id, state)

    def review_counts(self):
        saved, failed = self.store.item_counts(self.site_id)
        counts = {
            "identity_collisions": failed.get("failed_permanent:identity_collision", 0),
            "identity_reviews": sum(v for k, v in failed.items() if k.startswith("failed_permanent:identity_review:")),
            "listing_identity_conflicts": failed.get("failed_permanent:listing_identity_conflict", 0),
            "identity_missing": failed.get("failed_permanent:identity_missing", 0),
        }
        if self.contract:
            # Items a registered method could not turn into a document need a person
            # unless the plan explicitly tolerates that share (default: none).
            counts["identity_missing"] += sum(failed.get(k, 0) for k in CONTRACT_IDENTITY_MISSING)
            bad = sum(v for k, v in failed.items()
                      if k.startswith(CONTRACT_FAILURE_PREFIXES) and k not in CONTRACT_IDENTITY_MISSING)
            seen = saved + sum(failed.values())
            ratio = float(self.plan.get("max_contract_failure_ratio") or 0.0)
            if bad and bad / seen > ratio:
                counts["adapter_contract_failures"] = bad
        # Detail URLs robots.txt excludes were never collected: a person decides unless the plan
        # explicitly tolerates that share (default: none).
        robots = failed.get("failed_permanent:robots_disallowed", 0)
        ratio = float(self.plan.get("max_robots_disallowed_ratio") or 0.0)
        if robots and robots / (saved + sum(failed.values())) > ratio:
            counts["robots_disallowed"] = robots
        return {k: v for k, v in counts.items() if v}

    def contract_stop_held(self, current):
        """True while the items of an earlier consecutive contract stop are still
        failed_permanent (not requeued with --reprocess-failed)."""
        if not (self.contract and current and current["status"] == "config_error"
                and str(current.get("stop_reason") or "").startswith(CONTRACT_STOP)):
            return False
        keys = (current.get("state") or {}).get("adapter_contract_stop_items")
        if not keys:
            return True  # nothing recorded to check: keep holding
        rows = [self.store.item(self.site_id, k) for k in keys]
        return any(r is None or r["status"] == "failed_permanent" for r in rows)

    def run(self, *, refresh=False, retry_only=False):
        """Enumerate (or resume). A completed site is only re-enumerated with ``refresh``;
        otherwise (and with ``retry_only``) just the transient item failures are retried."""
        plan, st, site = self.plan, self.store, self.site_id
        current = st.site(site)
        if current and current["status"] == "access_blocked" and not refresh:
            # Refusals persist across runs and passes; a person must review first.
            return self._result("access_blocked", "access_hold_requires_review")
        if not refresh and self.contract_stop_held(current):
            # Like the access hold: no request, no checkpoint move, the stop reason is kept.
            return self._result("config_error", "adapter_contract_stop_requires_reprocess")
        for url in (plan.listing.get("url"), plan.base_url, plan.section("sitemap").get("sitemap_url")):
            held = self.store.host_hold(host_of(url)) if url and host_of(url) else None
            if held:
                # A held host (refused earlier, or seeded) ends the site before any request,
                # including the identity precheck.
                reason = f"host_access_hold:{held['reason']}"
                if current is None:
                    st.start_site(site, plan.fingerprint, plan.strategy)
                st.event(site, "access_blocked", {"reason": reason, "url": url, "status": None})
                st.finish_site(site, "access_blocked", reason)
                return self._result("access_blocked", reason)
        cooldown = (current or {}).get("state", {}).get("cooldown_until")
        if cooldown and cooldown > time.time():
            return self._result("incomplete", "cooling_down", cooldown_until=cooldown)
        finished = current and current["plan_fp"] == plan.fingerprint and \
            current["status"] in COMPLETED
        if retry_only or (finished and not refresh):
            state = dict((current or {}).get("state") or {})
            try:
                self.retry_pass()
            except BaseException as exc:
                mapped = self._stop_status(exc, state)
                if mapped is None:
                    raise
                status, reason = mapped
                # The same transitions as a full run: refusals become the site
                # status (held), cooldowns are persisted in the site state.
                if current is None:
                    st.start_site(site, plan.fingerprint, plan.strategy)
                if status in ("access_blocked", "review_pending", "config_error", "private_destination_blocked"):
                    st.finish_site(site, status, reason, state)
                else:
                    st.save_state(site, state)
                st.event(site, "retry_pass_stopped", {"reason": reason})
                self.record_timing(state)
                return self._result(status, reason)
            if current is not None and self.fetcher.requests_made:
                self.record_timing(state)
            return self._result(current["status"] if current else "not_started",
                                "retry_only" if retry_only else "already_complete_retry_only")
        doc_conn = self.adapter.conn
        duplicates_before = duplicate_rows(doc_conn, site)
        state = st.start_site(site, plan.fingerprint, plan.strategy)
        state.pop("cooldown_until", None)
        state.pop("adapter_contract_stop_items", None)
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
        except KeyboardInterrupt:
            st.finish_site(site, "incomplete", "interrupted", ctx.state)
            raise
        except BaseException as exc:
            mapped = self._stop_status(exc, ctx.state)
            if mapped is None:
                st.finish_site(site, "failed", f"runner_error:{type(exc).__name__}", ctx.state)
                raise
            status, reason = mapped
            if isinstance(exc, _IdentityStop):
                identity = exc.detail
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
        review = self.review_counts()
        self.warnings.extend(f"{k}:{v}" for k, v in review.items())
        robots = st.item_counts(site)[1].get("failed_permanent:robots_disallowed", 0)
        if robots and "robots_disallowed" not in review:
            self.warnings.append(f"robots_disallowed:{robots}")  # tolerated by the plan, still shown
        if status == "complete" and review:
            # Enumeration finished, but items need a person: never a plain "complete".
            status = "complete_with_review"
            reason = ";".join(f"{k}:{v}" for k, v in review.items())
        ctx.state["warnings"] = list(self.warnings)
        self.record_timing(ctx.state, save=False)
        st.finish_site(site, status, reason, ctx.state)
        return self._result(status, reason, identity=identity, duplicates=duplicates)


class _IdentityStop(Exception):
    def __init__(self, reason, detail):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail

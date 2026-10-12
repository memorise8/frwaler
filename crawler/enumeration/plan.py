"""plan.json loading and validation."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .http import DEFAULT_USER_AGENT

STRATEGY_NAMES = ("last_page_backward", "date_range_split", "oldest_first_sort", "sitemap")
MIN_INTERVAL_S = 1.5

DEFAULTS = {
    "request_interval_s": 1.5,
    "max_requests": 20000,
    "max_runtime_s": 6 * 3600,
    "max_pages": None,
    "retries": 3,
    "backoff_s": 2.0,
    "max_retry_wait_s": 120,
    "item_max_attempts": 3,
    "max_consecutive_save_errors": 5,
    "max_consecutive_robots_disallowed": 5,
    "respect_robots": True,
    "user_agent": DEFAULT_USER_AGENT,
    "encoding": None,
    "verify_ssl": True,
    "file_max_attempts": 3,
    "file_max_bytes": 200 * 1024 * 1024,
}

# Keys that define *what* is enumerated. Budgets, pacing and adapters do not
# change the enumeration contract, so changing them keeps progress.
# The destination address check (no private/loopback/link-local/reserved targets)
# is not configurable: a plan naming any such switch is refused.
_FORBIDDEN_KEYS = ("allow_private_destinations", "allow_private_addresses", "allow_private",
                   "private_destination_check", "destination_check", "resolver", "dns_resolver",
                   "pinned_addresses", "allow_internal")

_CONTRACT_KEYS = ("strategy", "listing", "segments", "sitemap", "last_page", "oldest_sort", "overall_total_check")


class PlanError(ValueError):
    pass


@dataclass
class SitePlan:
    site_id: str
    raw: dict

    @property
    def strategy(self):
        return self.raw["strategy"]

    @property
    def listing(self):
        return self.raw.get("listing") or {}

    @property
    def base_url(self):
        return self.raw.get("base_url") or ""

    @property
    def max_pages(self):
        return self.raw.get("max_pages")

    def section(self, name):
        return self.raw.get(name) or {}

    def get(self, name):
        return self.raw.get(name, DEFAULTS.get(name))

    @property
    def fingerprint(self):
        contract = {k: self.raw.get(k) for k in _CONTRACT_KEYS}
        adapter = self.raw.get("adapter") or {}
        if "contract_version" in adapter:
            contract["adapter_contract"] = adapter
        return hashlib.sha256(json.dumps(contract, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def validate_end_statuses(site_id, entries):
    """End-of-listing statuses: 404/410 as plain ints; anything else only with a
    verified body condition ({"status": 400, "json_code": ...} or "body_regex").
    Refusals (401/403/407/429/451) can never mark an end."""
    from .http import REFUSAL_STATUSES
    for entry in entries or ():
        status = entry.get("status") if isinstance(entry, dict) else entry
        if not isinstance(status, int) or isinstance(status, bool):
            raise PlanError(f"{site_id}: invalid end status {entry!r}")
        if status in REFUSAL_STATUSES or status < 300:
            raise PlanError(f"{site_id}: status {status} can never be an end condition")
        if isinstance(entry, dict):
            if not (entry.get("json_code") or entry.get("body_regex")):
                raise PlanError(f"{site_id}: end status {status} needs json_code or body_regex")
        elif status not in (404, 410):
            raise PlanError(f"{site_id}: end status {status} needs a verified body condition")


def site_plan(plan, site_id, *, min_interval=MIN_INTERVAL_S, registered=()):
    """Validate one site. ``registered``: site ids with an existing crawler; such sites
    must reuse that crawler's listing parser and identity rules (no duplicate rows)."""
    if not isinstance(plan, dict) or not isinstance(plan.get("sites"), dict):
        raise PlanError("plan must contain a 'sites' object")
    if site_id not in plan["sites"]:
        raise PlanError(f"site not in plan: {site_id}")
    from .strategies import apply_preset
    raw = {**DEFAULTS, **(plan.get("defaults") or {}), **plan["sites"][site_id]}
    relaxing = sorted(k for k in raw if k in _FORBIDDEN_KEYS or "private" in k.lower())
    if relaxing:
        raise PlanError(f"{site_id}: destination address check cannot be configured by a plan ({relaxing})")
    if raw.get("strategy") not in STRATEGY_NAMES:
        raise PlanError(f"{site_id}: strategy must be one of {STRATEGY_NAMES}")
    # Access policy is an execution ceiling: a plan can tighten, never relax it.
    if raw.get("respect_robots") is not True:
        raise PlanError(f"{site_id}: respect_robots cannot be disabled by a plan")
    interval = raw.get("request_interval_s")
    if not isinstance(interval, (int, float)) or not min_interval <= interval <= 10:
        raise PlanError(f"{site_id}: request_interval_s must be within [{min_interval}, 10]")
    if raw.get("max_pages") is not None and (not isinstance(raw["max_pages"], int) or raw["max_pages"] < 1):
        raise PlanError(f"{site_id}: max_pages must be null or a positive integer")
    if raw["strategy"] != "sitemap":
        listing = apply_preset(dict(raw.get("listing") or {}))
        if not listing.get("url"):
            raise PlanError(f"{site_id}: listing.url is required")
        start = listing.get("start_page", 1)
        if not isinstance(start, int) or start < 0:
            raise PlanError(f"{site_id}: listing.start_page must be a non-negative integer (0 or 1)")
        validate_end_statuses(site_id, listing.get("end_statuses", ()))
        if listing.get("page_mode") == "offset" and not isinstance(listing.get("page_size"), int):
            raise PlanError(f"{site_id}: offset paging requires listing.page_size")
        raw["listing"] = listing
    if raw["strategy"] == "date_range_split":
        seg = raw.get("segments") or {}
        if seg.get("mode", "range") == "explicit":
            if not seg.get("items"):
                raise PlanError(f"{site_id}: explicit segments require items")
        elif not seg.get("from") or not (seg.get("filter") or {}).get("type", "range"):
            raise PlanError(f"{site_id}: range segments require 'from' and 'filter'")
    if site_id in set(registered):
        adapter = raw.get("adapter") or {}
        if adapter.get("type") != "crawler":
            raise PlanError(f"{site_id}: registered_crawler_must_be_reused (adapter.type must be 'crawler')")
        if adapter.get("id_regex"):
            raise PlanError(f"{site_id}: registered_crawler_must_be_reused (no plan id_regex)")
        if raw["strategy"] != "sitemap" and (raw["listing"].get("parser") or {}).get("type") != "adapter":
            raise PlanError(f"{site_id}: registered_crawler_must_be_reused (listing.parser.type must be 'adapter')")
    if raw["strategy"] == "sitemap" and (raw.get("adapter") or {}).get("detail_method") and \
            (raw.get("adapter") or {}).get("type") == "crawler":
        detail_args = (raw.get("adapter") or {}).get("detail_args", ["html", "item"])
        if "item" in detail_args:
            # Sitemap items carry no listing payload, so identity could not be checked.
            raise PlanError(f"{site_id}: sitemap with a detail_method needing list items is not verifiable")
    if raw["strategy"] == "sitemap":
        sm = raw.get("sitemap") or {}
        if not sm.get("sitemap_url") and not sm.get("from_robots"):
            raise PlanError(f"{site_id}: sitemap requires sitemap_url or from_robots")
    robots_limit = raw.get("max_consecutive_robots_disallowed")
    if type(robots_limit) is not int or robots_limit < 1:
        raise PlanError(f"{site_id}: max_consecutive_robots_disallowed must be a positive integer")
    robots_ratio = raw.get("max_robots_disallowed_ratio", 0.0)
    if type(robots_ratio) not in (int, float) or not 0 <= robots_ratio < 1:
        raise PlanError(f"{site_id}: max_robots_disallowed_ratio must be within [0, 1)")
    from .contracts import validate_contract, uses_item, ContractError
    adapter = raw.get("adapter") or {}
    try:
        has_contract = validate_contract(adapter)
    except ContractError as exc:
        raise PlanError(f"{site_id}: {exc}") from None
    if has_contract and raw["strategy"] == "sitemap" and uses_item(adapter):
        raise PlanError(f"{site_id}: sitemap with a detail_method needing list items is not verifiable")
    if has_contract:
        limit = raw.get("max_consecutive_contract_failures", 5)
        if type(limit) is not int or limit < 1:
            raise PlanError(f"{site_id}: max_consecutive_contract_failures must be a positive integer")
        ratio = raw.get("max_contract_failure_ratio", 0.0)
        if type(ratio) not in (int, float) or not 0 <= ratio < 1:
            raise PlanError(f"{site_id}: max_contract_failure_ratio must be within [0, 1)")
    return SitePlan(site_id, raw)


def load_plan(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("version", 1) != 1:
        raise PlanError("unsupported plan version")
    return data

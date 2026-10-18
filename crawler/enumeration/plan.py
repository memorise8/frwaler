"""plan.json loading and validation."""
from __future__ import annotations

import hashlib
import json
import re
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


# Request headers a plan may never set: identity/transport headers, cookies (a session is a
# login) and anything that would let a request pose as coming through another hop.
_FORBIDDEN_HEADERS = re.compile(r"^(host|cookie2?|set-cookie|user-agent|content-length|content-type|transfer-encoding|"
                                r"connection|te|upgrade|forwarded|via|proxy-.*|x-forwarded-.*|x-real-ip|"
                                r"x-http-method(-override)?|x-method-override)$", re.I)
# Secret-bearing names: only through request_headers_env (never a literal in a plan file).
_SECRET_HEADERS = re.compile(r"^authorization$|token|key|secret|auth|session|password", re.I)
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_LITERAL = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")


def _validate_headers(site_id, raw):
    """ext:headers. Literal values in ``request_headers``; secrets only as the *name* of an
    environment variable in ``request_headers_env`` ({"Header": "ENV"} or {"Header": {"env":
    "ENV", "format": "Bearer {}"}}). Resolved at run time by ``resolve_headers``."""
    lit, env = raw.get("request_headers") or {}, raw.get("request_headers_env") or {}
    if not isinstance(lit, dict) or not isinstance(env, dict):
        raise PlanError(f"{site_id}: request_headers/request_headers_env must be objects")
    for name in list(lit) + list(env):
        if not isinstance(name, str) or not _HEADER_NAME.match(name) or _FORBIDDEN_HEADERS.match(name):
            raise PlanError(f"{site_id}: request header not allowed: {name!r}")
    if set(map(str.lower, lit)) & set(map(str.lower, env)):
        raise PlanError(f"{site_id}: a request header is given twice")
    for name in lit:
        if _SECRET_HEADERS.search(name):
            raise PlanError(f"{site_id}: {name} carries a secret: use request_headers_env (variable name only)")
    hosts = raw.get("request_headers_hosts")
    if hosts is not None and (not isinstance(hosts, list) or not hosts
                              or not all(isinstance(h, str) and re.fullmatch(r"[a-z0-9.-]+", h) for h in hosts)):
        raise PlanError(f"{site_id}: request_headers_hosts must be a non-empty list of host names")
    for name, value in lit.items():
        if not isinstance(value, str) or re.search(r"[\r\n\x00]", value) or len(value) > 512:
            raise PlanError(f"{site_id}: request header {name} needs a short single-line string value")
    for name, spec in env.items():
        spec = {"env": spec} if isinstance(spec, str) else spec
        if not isinstance(spec, dict) or not _ENV_NAME.match(str(spec.get("env") or "")):
            raise PlanError(f"{site_id}: request_headers_env.{name} must name an environment variable")
        fmt = spec.get("format", "{}")
        if not isinstance(fmt, str) or fmt.count("{}") != 1 or re.search(r"[\r\n{}]", fmt.replace("{}", "")):
            raise PlanError(f"{site_id}: request_headers_env.{name}.format must contain one '{{}}'")


def resolve_headers(raw, environ=None):
    """Header dict for the Fetcher. A missing environment variable is a PlanError (the site
    does not run without its key, and never runs with an empty one)."""
    import os
    environ = os.environ if environ is None else environ
    out = dict(raw.get("request_headers") or {})
    for name, spec in (raw.get("request_headers_env") or {}).items():
        spec = {"env": spec} if isinstance(spec, str) else spec
        value = environ.get(spec["env"])
        if not value or re.search(r"[\r\n\x00]", value):
            raise PlanError(f"request header {name}: environment variable {spec['env']} is not set")
        out[name] = spec.get("format", "{}").replace("{}", value)
    return out


def header_hosts(raw):
    """Hosts that receive the plan's request headers: request_headers_hosts, else the listing
    (or sitemap/base) host only."""
    from urllib.parse import urlsplit
    if raw.get("request_headers_hosts"):
        return list(raw["request_headers_hosts"])
    url = (raw.get("listing") or {}).get("url") or (raw.get("sitemap") or {}).get("sitemap_url") or raw.get("base_url") or ""
    host = urlsplit(url.replace("{page}", "1").replace("{segment}", "x")).hostname
    return [host] if host else []


def _validate_listing_extensions(site_id, raw, listing):
    """ext:post_listing, ext:path_paging, ext:json_page, ext:http204_end. Absent keys keep the
    original behaviour (and the plan fingerprint) unchanged."""
    strategy = raw["strategy"]
    method = listing.get("method", "GET")
    if method not in ("GET", "POST"):
        raise PlanError(f"{site_id}: listing.method must be GET or POST")
    if method == "POST":
        if listing.get("idempotent") is not True:
            raise PlanError(f"{site_id}: POST listing requires idempotent=true (read-only search APIs only)")
        if listing.get("body_type", "form") not in ("form", "json"):
            raise PlanError(f"{site_id}: listing.body_type must be form or json")
    elif any(k in listing for k in ("idempotent", "body_type", "body", "content_type")):
        raise PlanError(f"{site_id}: idempotent/body_type/body/content_type apply to POST listings only")
    _validate_post_body(site_id, listing)
    if "{page}" in listing["url"]:
        if method == "POST":
            raise PlanError(f"{site_id}: path paging and POST listing cannot be combined")
        if "{segment}" in listing["url"] or strategy == "date_range_split":
            if "first_page_url" in listing and "{segment}" not in str(listing["first_page_url"]):
                # One first page for every segment would read the same page in each of them.
                raise PlanError(f"{site_id}: with segments first_page_url needs {{segment}}")
        first = listing.get("first_page_url")
        if first is not None and (not isinstance(first, str) or not re.match(r"https?://", first) or "{page}" in first):
            raise PlanError(f"{site_id}: listing.first_page_url must be an absolute URL without {{page}}")
    elif "first_page_url" in listing:
        raise PlanError(f"{site_id}: first_page_url needs a {{page}} listing url")
    cursor = listing.get("next_cursor")
    if cursor is not None:
        if strategy not in ("oldest_first_sort", "date_range_split"):
            raise PlanError(f"{site_id}: next_cursor paging is forward-only (oldest_first_sort, date_range_split)")
        if not isinstance(cursor, dict) or not _LITERAL.match(str(cursor.get("path") or "")) \
                or not _LITERAL.match(str(cursor.get("param") or "")):
            raise PlanError(f"{site_id}: next_cursor needs a literal path and param")
        overlap = (raw.get("oldest_sort") or {}).get("overlap_pages") or (raw.get("segments") or {}).get("overlap_pages")
        if overlap:
            raise PlanError(f"{site_id}: next_cursor paging cannot re-read earlier pages (overlap_pages)")
        if "{page}" in listing["url"]:
            raise PlanError(f"{site_id}: next_cursor and path paging cannot be combined")
    link = listing.get("next_link")
    if link is not None:
        # ext:next_link: the next page's address is read from the page itself (a signed
        # "load more" URL). Forward only, GET, on the listing host (checked again at run time).
        if strategy not in ("oldest_first_sort", "date_range_split"):
            raise PlanError(f"{site_id}: next_link paging is forward-only (oldest_first_sort, date_range_split)")
        if cursor is not None or "{page}" in listing["url"] or method != "GET":
            raise PlanError(f"{site_id}: next_link cannot be combined with next_cursor, path paging or POST")
        if not isinstance(link, dict) or not (
                (set(link) <= {"selector", "attr"} and isinstance(link.get("selector"), str) and link["selector"].strip()
                 and _LITERAL.match(str(link.get("attr", "href"))))
                or (set(link) == {"json_path"} and _LITERAL.match(str(link["json_path"])))):
            raise PlanError(f"{site_id}: next_link is {{selector, attr}} or {{json_path}}")
        overlap = (raw.get("oldest_sort") or {}).get("overlap_pages") or (raw.get("segments") or {}).get("overlap_pages")
        if overlap:
            raise PlanError(f"{site_id}: next_link paging cannot re-read earlier pages (overlap_pages)")
    last = listing.get("last_page_path")
    if last is not None:
        if strategy != "last_page_backward":
            raise PlanError(f"{site_id}: last_page_path applies to last_page_backward only")
        if not _LITERAL.match(str(last)):
            raise PlanError(f"{site_id}: last_page_path must be a literal JSON path")
    if "end_on_204" in listing and not isinstance(listing["end_on_204"], bool):
        raise PlanError(f"{site_id}: end_on_204 must be true or false")


_DYN = re.compile(r"\{dyn:([A-Za-z_][A-Za-z0-9_]{0,31})\}")
_MEDIA_TYPE = re.compile(r"^[a-z]+/[a-z0-9.+-]+(; ?charset=[A-Za-z0-9-]+)?$", re.I)
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def _walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _walk_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_strings(v)


def _validate_post_body(site_id, listing):
    """ext:post_listing_nested_page (a JSON body template; "{page}" is the page value wherever
    it sits) and ext:dynamic_token (values read from one public page of the listing host, used
    only in the listing POST's body or one header; never in URLs, files or logs)."""
    from urllib.parse import urlsplit
    body = listing.get("body")
    if body is not None:
        if listing.get("body_type") != "json" or not isinstance(body, dict):
            raise PlanError(f"{site_id}: listing.body is a JSON object template (body_type json)")
        if "params" in listing:
            raise PlanError(f"{site_id}: listing.body replaces params (give one of them)")
        pages = sum(1 for s in _walk_strings(body) if s == "{page}")
        if pages != (0 if listing.get("next_cursor") else 1):
            raise PlanError(f"{site_id}: listing.body needs exactly one \"{{page}}\" value"
                            " (none with next_cursor)")
        if any("{page}" in s and s != "{page}" for s in _walk_strings(body)):
            raise PlanError(f"{site_id}: \"{{page}}\" must be a whole value in listing.body")
    ctype = listing.get("content_type")
    if ctype is not None:
        if listing.get("body_type") != "json" or not isinstance(ctype, str) or not _MEDIA_TYPE.match(ctype) \
                or re.search(r"form|multipart", ctype, re.I):
            raise PlanError(f"{site_id}: listing.content_type is a JSON body's media type (no form/multipart)")
    used = set()
    for s in list(_walk_strings(body)) + list(_walk_strings(listing.get("params") or {})):
        used |= set(_DYN.findall(s))
    dyn = listing.get("dynamic")
    if dyn is None:
        if used:
            raise PlanError(f"{site_id}: {{dyn:...}} values need listing.dynamic")
        return
    if not isinstance(dyn, dict) or set(dyn) - {"url", "values", "header"}:
        raise PlanError(f"{site_id}: listing.dynamic has url, values and an optional header")
    url = dyn.get("url")
    if not isinstance(url, str) or not re.match(r"https?://", url) or \
            urlsplit(url).hostname != urlsplit(listing["url"]).hostname:
        raise PlanError(f"{site_id}: listing.dynamic.url must be an absolute URL on the listing host")
    values = dyn.get("values")
    if not isinstance(values, dict) or not values:
        raise PlanError(f"{site_id}: listing.dynamic.values must name at least one value")
    for name, rule in values.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,31}", str(name)) or not isinstance(rule, dict) \
                or len(rule) != 1 or not set(rule) <= {"regex", "json_path"}:
            raise PlanError(f"{site_id}: listing.dynamic.values.{name} is {{regex}} or {{json_path}}")
        if "regex" in rule:
            try:
                groups = re.compile(rule["regex"]).groups
            except (re.error, TypeError):
                raise PlanError(f"{site_id}: listing.dynamic.values.{name}.regex does not compile") from None
            if groups != 1:
                raise PlanError(f"{site_id}: listing.dynamic.values.{name}.regex needs exactly one group")
        elif not _LITERAL.match(str(rule["json_path"])):
            raise PlanError(f"{site_id}: listing.dynamic.values.{name}.json_path must be a literal path")
    header = dyn.get("header")
    if header is not None and listing.get("method", "GET") != "POST":
        raise PlanError(f"{site_id}: listing.dynamic.header applies to POST listings only")
    if header is not None:
        if not isinstance(header, dict) or set(header) != {"name", "value"} or \
                not all(isinstance(header[k], str) for k in header):
            raise PlanError(f"{site_id}: listing.dynamic.header is {{name, value}}")
        if not _DYN.fullmatch(header["name"]) and (not _HEADER_NAME.match(header["name"])
                                                    or _FORBIDDEN_HEADERS.match(header["name"])):
            raise PlanError(f"{site_id}: listing.dynamic.header.name is not an allowed header")
        if not _DYN.fullmatch(header["value"]):
            raise PlanError(f"{site_id}: listing.dynamic.header.value must be a {{dyn:...}} value")
        used |= set(_DYN.findall(header["name"] + header["value"]))
    missing = used - set(values)
    if missing:
        raise PlanError(f"{site_id}: undeclared dynamic values {sorted(missing)}")
    if not used:
        raise PlanError(f"{site_id}: listing.dynamic values are not used")


def _same_listing_host(listing, url):
    from urllib.parse import urlsplit
    return isinstance(url, str) and bool(re.match(r"https?://", url)) and \
        urlsplit(url).hostname == urlsplit(listing["url"].replace("{page}", "1").replace("{segment}", "x")).hostname


def _validate_segment_items(site_id, listing, items):
    """ext:multi_listing: an explicit segment may be its own listing (url, first_page_url on the
    listing host, same paging form) and carry a label for the crawler (``segment`` argument)."""
    if not isinstance(items, list) or not all(isinstance(s, dict) and "key" in s for s in items):
        raise PlanError(f"{site_id}: explicit segment items are objects with a key")
    keys = [str(s["key"]) for s in items]
    if len(set(keys)) != len(keys):
        raise PlanError(f"{site_id}: explicit segment keys must be unique")
    paged = "{page}" in listing["url"]
    for spec in items:
        if "url" in spec:
            url = spec["url"]
            if not _same_listing_host(listing, url) or "{segment}" in url or ("{page}" in url) != paged:
                raise PlanError(f"{site_id}: segment {spec['key']}: url must be on the listing host"
                                " with the listing's paging form")
        if "first_page_url" in spec and (not paged or not _same_listing_host(listing, spec["first_page_url"])
                                         or "{page}" in spec["first_page_url"]):
            raise PlanError(f"{site_id}: segment {spec['key']}: first_page_url needs a {{page}} listing"
                            " and a URL on the listing host")
        if "label" in spec and (not isinstance(spec["label"], str) or len(spec["label"]) > 200):
            raise PlanError(f"{site_id}: segment {spec['key']}: label must be a short string")


def _validate_discover(site_id, listing, seg):
    """ext:multi_listing: segments found on a hub page of the listing host on every run."""
    allowed = {"mode", "hub_url", "selector", "attr", "key_regex", "params", "label", "min_segments",
               "within_segment_order", "overlap_pages", "hidden_cap"}
    if set(seg) - allowed:
        raise PlanError(f"{site_id}: discover segments do not take {sorted(set(seg) - allowed)}")
    if not _same_listing_host(listing, seg.get("hub_url")):
        raise PlanError(f"{site_id}: segments.hub_url must be an absolute URL on the listing host")
    if not isinstance(seg.get("selector"), str) or not seg["selector"].strip():
        raise PlanError(f"{site_id}: segments.selector is required")
    if not _LITERAL.match(str(seg.get("attr", "href"))):
        raise PlanError(f"{site_id}: segments.attr must be an attribute name")
    try:
        groups = re.compile(seg.get("key_regex", "")).groups
    except (re.error, TypeError):
        groups = 0
    if groups != 1:
        raise PlanError(f"{site_id}: segments.key_regex needs exactly one group")
    params = seg.get("params", {})
    if not isinstance(params, dict) or not all(isinstance(v, str) for v in params.values()):
        raise PlanError(f"{site_id}: segments.params maps names to strings ({{key}} is the found key)")
    if "{segment}" not in listing["url"] and not params:
        raise PlanError(f"{site_id}: discovered segments need {{segment}} in listing.url or params")
    label = seg.get("label", "text")
    if label is not None and label != "text" and not re.fullmatch(r"attr:[A-Za-z_][A-Za-z0-9_.-]{0,63}", str(label)):
        raise PlanError(f"{site_id}: segments.label is text, attr:<name> or null")
    if type(seg.get("min_segments", 1)) is not int or seg.get("min_segments", 1) < 1:
        raise PlanError(f"{site_id}: segments.min_segments must be a positive integer")


def _validate_second_step(site_id, adapter):
    """ext:two_step_detail: a link on the detail page is the PDF source (no request) or a second
    detail page (one more governed request). Same host unless listed in allow_hosts."""
    step = adapter.get("second_step")
    if step is None:
        return
    if not isinstance(step, dict) or step.get("use") not in ("pdf_url", "detail_page"):
        raise PlanError(f"{site_id}: adapter.second_step.use must be pdf_url or detail_page")
    template = step.get("url_template")
    if template is not None:
        # ext:detail_aux_request: the second address is built from the item's own listing fields.
        if "selector" in step or not isinstance(template, str) or not re.match(r"https?://[^/{}]+/", template):
            raise PlanError(f"{site_id}: adapter.second_step.url_template is an absolute URL (instead of selector)")
        rest = re.sub(r"\{item:[A-Za-z_][A-Za-z0-9_]{0,63}\}", "", template)
        if "{" in rest or "}" in rest or rest == template:
            raise PlanError(f"{site_id}: adapter.second_step.url_template uses {{item:<field>}} values only")
    elif not isinstance(step.get("selector"), str) or not step["selector"].strip():
        raise PlanError(f"{site_id}: adapter.second_step.selector is required")
    if not _LITERAL.match(str(step.get("attr", "href"))):
        raise PlanError(f"{site_id}: adapter.second_step.attr must be an attribute name")
    hosts = step.get("allow_hosts", [])
    if not isinstance(hosts, list) or not all(isinstance(h, str) and re.fullmatch(r"[a-z0-9.-]+", h) for h in hosts):
        raise PlanError(f"{site_id}: adapter.second_step.allow_hosts must be a list of host names")
    if adapter.get("type", "list_only") == "list_only":
        raise PlanError(f"{site_id}: second_step needs a detail page (adapter type selectors or crawler)")


def _validate_first_tokens(site_id, adapter):
    """ext:two_step_keep_first: first_* argument tokens only with a second detail page."""
    bindings = []
    for stage in ("detail", "paper"):
        bindings += list(adapter.get(stage + "_args") or []) + list((adapter.get(stage + "_kwargs") or {}).values())
    list_bindings = list(adapter.get("list_args") or []) + list((adapter.get("list_kwargs") or {}).values())
    if "segment" in list_bindings:
        raise PlanError(f"{site_id}: the segment argument is for detail/paper methods")
    uses_first = any(isinstance(b, str) and b.startswith("first_") for b in bindings)
    if uses_first and (adapter.get("second_step") or {}).get("use") != "detail_page":
        raise PlanError(f"{site_id}: first_* arguments need adapter.second_step.use detail_page")
    # ext:pdf_text: the extraction and its argument come together, on a crawler detail method.
    uses_text = "pdf_text" in bindings
    if uses_text != (adapter.get("pdf_text") is not None):
        raise PlanError(f"{site_id}: adapter.pdf_text and the pdf_text argument go together")
    if uses_text:
        from .pdftext import PdfTextConfigError, validate
        if adapter.get("type") != "crawler" or not adapter.get("detail_method"):
            raise PlanError(f"{site_id}: pdf_text needs a crawler detail_method")
        try:
            validate(adapter["pdf_text"])
        except PdfTextConfigError as exc:
            raise PlanError(f"{site_id}: {exc}") from None


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
        _validate_listing_extensions(site_id, raw, listing)
        if listing.get("page_mode") == "offset" and not isinstance(listing.get("page_size"), int):
            raise PlanError(f"{site_id}: offset paging requires listing.page_size")
        raw["listing"] = listing
    if raw["strategy"] == "date_range_split":
        seg = raw.get("segments") or {}
        if seg.get("mode", "range") == "explicit":
            if not seg.get("items"):
                raise PlanError(f"{site_id}: explicit segments require items")
            _validate_segment_items(site_id, raw["listing"], seg["items"])
        elif seg.get("mode") == "discover":
            _validate_discover(site_id, raw["listing"], seg)
        elif (seg.get("filter") or {}).get("type") != "query_prefix_range" and (
                not seg.get("from") or not (seg.get("filter") or {}).get("type", "range")):
            raise PlanError(f"{site_id}: range segments require 'from' and 'filter'")
        flt = seg.get("filter") or {}
        if flt.get("type") in ("query_range", "query_prefix_range"):
            if seg.get("mode", "range") != "range":
                raise PlanError(f"{site_id}: query_range requires range segment mode")
            # Only a field and a parameter name, never executable templates or URLs.
            for key in ("param", "field"):
                value = flt.get(key)
                if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", value):
                    raise PlanError(f"{site_id}: query_range requires a literal {key}")
            if flt["param"] == raw["listing"].get("page_param", "page"):
                raise PlanError(f"{site_id}: query_range parameter must differ from page_param")
            if flt.get("to_inclusive", True) is not True:
                raise PlanError(f"{site_id}: query_range currently requires inclusive date bounds")
            if raw.get("overall_total_check") is not True:
                raise PlanError(f"{site_id}: query_range requires overall_total_check=true")
            from urllib.parse import parse_qs, urlsplit
            existing = parse_qs(urlsplit(raw["listing"]["url"]).query, keep_blank_values=True)
            if any(existing.get(flt["param"], [])) or (raw["listing"].get("params") or {}).get(flt["param"]):
                raise PlanError(f"{site_id}: query_range cannot replace an existing scope query")
            if flt["type"] == "query_prefix_range":
                # Half-open string ranges over digit-prefixed ids (lexicographic comparison).
                if type(seg.get("hidden_cap")) is not int or seg["hidden_cap"] < 1:
                    raise PlanError(f"{site_id}: query_prefix_range requires a positive integer hidden_cap")
                if type(seg.get("max_depth")) is not int or not 1 <= seg["max_depth"] <= 18:
                    raise PlanError(f"{site_id}: query_prefix_range requires max_depth within [1, 18]")
                if not raw["listing"].get("total"):
                    raise PlanError(f"{site_id}: query_prefix_range requires listing.total (the reported range total)")
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
    _validate_headers(site_id, raw)
    _validate_second_step(site_id, raw.get("adapter") or {})
    _validate_first_tokens(site_id, raw.get("adapter") or {})
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

"""Single governed request path for enumeration.

Every listing, detail, sitemap, robots and file request goes through
``Fetcher.get``. It serializes requests per host with the same file-locked
``RequestGate`` the structure reconnaissance uses (minimum gap measured from
the end of the previous response), enforces request/time budgets, retries
transient failures a bounded number of times and classifies access refusals
as a stop condition. Refusals are never retried with other clients.

Redirects are never followed by the transport: every hop is a separate gated,
budgeted request preceded by the destination's robots and hold checks, and a
Location that points at a login/authentication/challenge path is not followed.
Hard refusals are recorded as persistent host holds (shared by every site id
on that host) and 429 cooldowns are written into the host's RequestGate.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from delivery.health.access_challenge import access_challenge
from delivery.health.diagnostics import host_only
from delivery.health.request_gate import NetworkDeferred, RequestGate
from delivery.health.source_observer import retry_after

from .urls import host_of

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
_LOGIN_PATH = re.compile(r"(^|/)(login|logon|signin|sign-in|sso|cas/login|auth|oauth2?|saml|captcha|challenge)"
                         r"(/|$|\.|\?)", re.I)
REFUSAL_STATUSES = (401, 403, 407, 429, 451)
SLOW_REQUEST_S = 5.0  # gate wait + response time at or above this is kept as a slow sample
SLOW_SAMPLES = 10
ROBOTS_RETRY_S = 300  # an unreachable robots.txt is not asked again sooner (per origin, per Fetcher)
# A Crawl-delay/Request-rate gap above this is not waited out request by request (holding the
# host lock for hours): the site stops (robots_crawl_delay_too_long) for a person to decide.
MAX_CRAWL_DELAY_S = 300
_PASSWORD = re.compile(rb"<input[^>]+type\s*=\s*[\"']?password", re.I)
_LOGIN_WORDS = re.compile(r"\b(log ?in|sign ?in|logon)\b|로그인|connexion|anmelden|inloggen|accedi|iniciar sesi", re.I)


_OVERLAY = re.compile(r"modal|popup|pop-up|dialog|offcanvas|overlay|dropdown|lightbox", re.I)
_PASSWORD_TYPE = re.compile(r"^\s*password\s*$", re.I)


def _overlay(tag):
    """A modal/dialog/popup container: page chrome, not what the page is for."""
    if tag.name == "dialog" or str(tag.get("role") or "").lower() in ("dialog", "alertdialog"):
        return True
    classes = tag.get("class") or []
    names = " ".join(classes if isinstance(classes, list) else [str(classes)]) + " " + str(tag.get("id") or "")
    return bool(_OVERLAY.search(names))


def login_page(body):
    """A page whose main purpose is a login form (not a sidebar/header/modal login widget).

    True only when a password form exists and either the page title/main heading names a
    login, or nothing but the form carries links. When every password field sits inside a
    modal/dialog/popup and the page has its own <h1> outside overlays and chrome, the
    overlay's headings do not count (a site-wide login modal on a content page); the title
    and the remaining headings still do.
    """
    if not _PASSWORD.search(body[:262144]):
        return False
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(body[:262144], "html.parser")
    for tag in soup.find_all(_overlay):
        tag.decompose()
    in_content = soup.find("input", attrs={"type": _PASSWORD_TYPE}) is not None
    discount = False
    if not in_content:
        # Overlay headings are discounted only when the page has its own main heading outside
        # the overlays and the page chrome (a content page carrying a site-wide login modal).
        # A dedicated login page built as a "popup" box has none: its headings still count.
        rest = BeautifulSoup(body[:262144], "html.parser")
        for tag in rest.find_all(_overlay) + rest.find_all(["nav", "header", "footer"]):
            tag.decompose()
        discount = any(h.get_text(strip=True) for h in rest.find_all("h1"))
    heads_soup = soup if discount else BeautifulSoup(body[:262144], "html.parser")
    heads = [heads_soup.title] + heads_soup.find_all(["h1", "h2"], limit=3)
    if any(h is not None and _LOGIN_WORDS.search(h.get_text(" ", strip=True)) for h in heads):
        return True
    for form in soup.find_all("form"):
        form.decompose()
    for tag in soup.find_all(["nav", "header", "footer"]):
        tag.decompose()
    return not soup.find_all("a", href=True)


_SECONDS = re.compile(r"(\d+(?:\.\d*)?|\.\d+)\s*(s?)", re.I)
_RATE = re.compile(r"(\d+)\s*/\s*(\d+(?:\.\d*)?|\.\d+)\s*([smh]?)", re.I)
_UNIT_S = {"": 1, "s": 1, "m": 60, "h": 3600}


def crawl_delay(text, user_agent):
    """Crawl-delay (seconds, fractional allowed, optional 's' suffix): the larger of the groups
    matching this agent (whole product token, as the rule groups) and '*'.
    urllib.robotparser ignores non-integer values, so this reads the file itself. A value that
    cannot be read in a group that applies to us raises ValueError: the caller then reads the
    file as unparseable (the site stops) instead of as "no delay"."""
    agents, in_rules, best, star = [], False, None, None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        field, value = (x.strip() for x in line.split(":", 1))
        field = field.lower()
        if field == "user-agent":
            if in_rules:
                agents, in_rules = [], False
            agents.append(value.lower())
        elif field == "crawl-delay":
            in_rules = True
            applies = [a for a in agents if a == "*" or _agent_applies(a, user_agent)]
            if not applies or not value:
                continue
            match = _SECONDS.fullmatch(value)
            if not match:
                raise ValueError(f"unreadable Crawl-delay: {value!r}")
            delay = float(match.group(1))
            for agent in applies:
                if agent == "*":
                    star = delay if star is None else star
                else:
                    best = delay
        else:
            in_rules = True
    # The larger of a matching group's and the '*' group's delay: a group naming a word of a
    # browser User-Agent must not shorten the spacing the site asks of every crawler.
    return max(0.0, best or 0.0, star or 0.0)


_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_HEX = frozenset(b"0123456789abcdefABCDEF")


def robots_octets(value):
    """RFC 9309 2.2.2 comparison form: characters outside printable ASCII become UTF-8
    %XX, %XX of an unreserved character is decoded, other escapes are upper-cased."""
    data = value.encode("utf-8")
    out, i = [], 0
    while i < len(data):
        b = data[i]
        if b == 0x25 and i + 2 < len(data) and data[i + 1] in _HEX and data[i + 2] in _HEX:
            code = int(data[i + 1:i + 3], 16)
            out.append(chr(code) if code in _UNRESERVED else "%%%02X" % code)
            i += 3
            continue
        out.append("%%%02X" % b if b <= 0x20 or b >= 0x7F else chr(b))
        i += 1
    return "".join(out)


def _agent_applies(agent, user_agent):
    """Group user-agent token vs our product token: the first product of our User-Agent
    ("Mozilla" for the default), case-insensitive, as urllib.robotparser selected groups.
    Other words of a browser User-Agent (Chrome, Safari...) do not select a group: such a
    group could otherwise replace a stricter '*' group."""
    product = re.split(r"[/\s]", user_agent.strip().lower(), 1)[0]
    # The whole group value must be that bare token: "Mozilla/5.0 (compatible; Yeti/1.1)"
    # names another crawler's full User-Agent, never us.
    return bool(product) and agent.strip() == product


class RobotsRules:
    """Parsed robots.txt (RFC 9309): groups of user-agent lines followed by rules."""

    def __init__(self, groups):
        self.groups = groups  # [{"agents": [...], "rules": [(allow, length, pieces, anchored)], "rate": ...}]

    def _groups_for(self, user_agent):
        specific = [g for g in self.groups if any(_agent_applies(a, user_agent) for a in g["agents"])]
        return specific or [g for g in self.groups if "*" in g["agents"]]

    def allows(self, url, user_agent):
        parts = urlsplit(url)
        target = robots_octets((parts.path or "/") + (f"?{parts.query}" if parts.query else ""))
        best = None  # (length, allow): the longest match wins, Allow on equal length
        for group in self._groups_for(user_agent):
            for allow, length, pieces, anchored in group["rules"]:
                if _robots_match(pieces, anchored, target) and (best is None or (length, allow) > best):
                    best = (length, allow)
        return True if best is None else best[1]

    def request_rate(self, user_agent):
        groups = self._groups_for(user_agent)
        if any(g["rate"] == "unreadable" for g in groups):
            # Never read as "no rate": the caller reads the file as unparseable (the site stops).
            raise ValueError("unreadable Request-rate")
        rates = [g["rate"] for g in groups if g["rate"]]
        return max(rates) if rates else 0.0


def _robots_pattern(value):
    """(length, pieces, anchored): the literal pieces between '*' wildcards (a run of '*' is one
    wildcard) and whether a trailing '$' anchors the end. The length is the rule as written."""
    pattern = robots_octets(value)
    if not pattern.startswith(("/", "*")):
        pattern = "/" + pattern  # a path rule without its leading slash is still a path rule
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    return len(pattern), re.sub(r"\*+", "*", body).split("*"), anchored


def _robots_match(pieces, anchored, target):
    """Prefix match of a '*'-wildcard rule (no regex, no backtracking): the first piece at the
    start, each further piece at its leftmost place after the previous one, the last at the end
    when anchored. Leftmost placement is exact for '*'-only patterns; O(len(target) * len(rule))."""
    if not target.startswith(pieces[0]):
        return False
    if len(pieces) == 1:
        return not anchored or target == pieces[0]
    pos = len(pieces[0])
    for piece in pieces[1:-1]:
        pos = target.find(piece, pos)
        if pos < 0:
            return False
        pos += len(piece)
    last = pieces[-1]
    if anchored:
        return len(target) - len(last) >= pos and target.endswith(last)
    return target.find(last, pos) >= 0


def parse_robots(text):
    """Parse robots.txt text into RobotsRules. Unknown lines (Sitemap, ...) are ignored; rules
    before any user-agent line belong to no group; an empty Disallow/Allow carries no rule."""
    groups, current, agent_run = [], None, False
    for raw in text.lstrip("﻿").splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        field, value = (x.strip() for x in line.split(":", 1))
        field = field.lower()
        if field == "user-agent":
            if not agent_run:
                current = {"agents": [], "rules": [], "rate": None}
                groups.append(current)
            current["agents"].append(value.lower())
            agent_run = True
        elif field in ("allow", "disallow"):
            agent_run = False
            if current is not None and value:
                current["rules"].append((field == "allow", *_robots_pattern(value)))
        elif field in ("crawl-delay", "request-rate"):
            agent_run = False
            if field == "request-rate" and current is not None and value and current["rate"] != "unreadable":
                # "n/t" with an optional s/m/h unit on t: one request per t/n seconds.
                match = _RATE.fullmatch(value)
                if not match or not int(match.group(1)):
                    current["rate"] = "unreadable"
                else:
                    rate = float(match.group(2)) * _UNIT_S[match.group(3).lower()] / int(match.group(1))
                    current["rate"] = max(rate, current["rate"] or 0.0)
    return RobotsRules(groups)


def forbidden_address(value):
    """True for any address a crawl must never reach: private, loopback, link-local
    (incl. 169.254.169.254 metadata), unique-local, unspecified, multicast, reserved,
    shared (100.64/10), documentation/benchmark ranges; IPv4-mapped IPv6 is unwrapped."""
    ip = ipaddress.ip_address(str(value).split("%", 1)[0])
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified)


def system_resolve(host):
    """All addresses the system resolver returns for ``host`` (TCP)."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return list(dict.fromkeys(info[4][0] for info in infos))


def checked_addresses(url, host, resolver):
    """Addresses of ``host`` after the destination check. IP literals are checked as is;
    names are resolved and refused if *any* address is forbidden. No resolver: literals only."""
    try:
        addresses = [str(ipaddress.ip_address(host.strip("[]")))]
    except ValueError:
        if resolver is None:
            return []
        try:
            addresses = list(resolver(host) or ())
        except (OSError, UnicodeError):
            addresses = []
        if not addresses:
            raise TransientFailure("dns_unresolved", url)
    for address in addresses:
        if forbidden_address(address):
            raise PrivateDestination(url, host, address)
    return addresses


def location_of(resp):
    """Location header as requests reads it: http.client decodes header bytes as latin-1,
    so a raw UTF-8 Location is re-encoded and decoded as UTF-8 (unchanged when not UTF-8)."""
    location = resp.header("location") or ""
    try:
        return location.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return location


def governed_url(url):
    """``url`` with an internationalised host written as IDNA (punycode), so the request gate
    can key it. A host the gate cannot govern (e.g. '_' in a label) is a PermanentFailure:
    such a URL is never requested."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise PermanentFailure("invalid_url", url) from None
    if host and not host.isascii():
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise PermanentFailure("invalid_host", url) from None
        userinfo = parts.netloc.rpartition("@")[0]
        netloc = (userinfo + "@" if userinfo else "") + ascii_host + (f":{port}" if port is not None else "")
        url = urlunsplit(parts._replace(netloc=netloc))
    if host_only(url) is None:
        raise PermanentFailure("ungovernable_host", url)
    return url


class FetchError(Exception):
    def __init__(self, reason, url=None, status=None):
        super().__init__(reason)
        self.reason = reason
        self.url = url
        self.status = status


class PermanentFailure(FetchError):
    """404/410/other 4xx: retrying will not help."""


class TransientFailure(FetchError):
    """Transport error, 5xx or 429 that persisted through the retries."""


class EnumerationStop(BaseException):
    """Stops a site run. BaseException so parser code's ``except Exception`` cannot swallow it."""

    def __init__(self, reason, url=None, status=None):
        super().__init__(reason)
        self.reason = reason
        self.url = url
        self.status = status


class AccessBlocked(EnumerationStop):
    """Authentication, refusal, challenge or robots denial. Never bypassed."""


class RobotsDisallowed(AccessBlocked):
    """robots.txt disallows this one URL. Not recorded on the Fetcher: a detail or file URL is
    ledgered as an item failure; a listing/sitemap URL propagates and stops the site."""


class RateLimited(EnumerationStop):
    """Repeated 429 responses: stop the site and respect a cooldown."""

    def __init__(self, reason, url=None, status=429, retry_at=None):
        super().__init__(reason, url, status)
        self.retry_at = retry_at


class PolicyRefusal(ValueError):
    """A plan tried to relax the access policy (fixed execution ceiling)."""


class PrivateDestination(EnumerationStop):
    """The destination resolves to a non-public address: never requested, site stopped
    (not a host hold: it says nothing about the source's consent)."""

    def __init__(self, url, host, address):
        super().__init__(f"private_destination_blocked:{host}:{address}", url)
        self.host, self.address = host, address


class RobotsUnavailable(EnumerationStop):
    """robots.txt answered 5xx/429 or could not be reached: permission is never assumed.
    Stops the site run (resumable, not a hold); cached for ROBOTS_RETRY_S per origin."""


class LoginReview(EnumerationStop):
    """A listing that is only a login page: stop and wait for review (not a permanent hold)."""


class BudgetExhausted(EnumerationStop):
    """Request or wall-clock budget reached; the run stops without completion."""


class TransportError(Exception):
    """Raised by a transport for connection-level failures."""


class BodyTooLarge(TransportError):
    """The body exceeds the transport's byte limit: retrying would download it again."""


_BOMS = ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))
_CHARSET = re.compile(r"charset\s*=\s*[\"']?\s*([\w.:-]+)", re.I)
_META_CHARSET = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([\w.:-]+)", re.I)
_META_SCAN = 64 * 1024  # bytes of <head> searched for a <meta> charset


class Response:
    def __init__(self, status, headers=None, body=b"", url="", encoding=None):
        self.status = int(status)
        self.headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        self.body = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.url = url
        self._encoding = encoding
        self.login_suspect = False

    def header(self, name, default=None):
        return self.headers.get(name.lower(), default)

    @property
    def content_type(self):
        return self.header("content-type", "") or ""

    def text(self, encoding=None):
        """Decoded body. A byte order mark wins (and is stripped), then the given/plan
        encoding, the Content-Type charset (quoted or not), a <meta> charset in <head>, UTF-8."""
        for bom, name in _BOMS:
            if self.body.startswith(bom):
                return self.body[len(bom):].decode(name, errors="replace")
        enc = encoding or self._encoding
        if not enc:
            m = _CHARSET.search(self.content_type)
            if m:
                enc = m.group(1)
        if not enc:
            head = self.body[:_META_SCAN]
            end = head.lower().find(b"</head")
            m = _META_CHARSET.search(head if end < 0 else head[:end])
            if m:
                enc = m.group(1).decode("ascii", "ignore")
        try:
            return self.body.decode(enc or "utf-8", errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text())


def _pin_connections(session, resolver):
    """DNS rebinding guard: every new connection resolves and checks its host once and
    connects to that checked address (``_dns_host``); Host header, SNI and certificate
    verification keep the original name."""
    from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

    def pinned(base):
        scheme = "https" if issubclass(base, HTTPSConnectionPool) else "http"

        class Connection(base.ConnectionCls):
            def _new_conn(self):
                # urllib3's ``host`` property reads ``_dns_host`` (SNI, certificate check, Host
                # header), so the checked address may only stand in while the socket is made.
                name = self._dns_host
                self._dns_host = checked_addresses(f"{scheme}://{name}", name, resolver)[0]
                try:
                    return super()._new_conn()
                finally:
                    self._dns_host = name

        class Pool(base):
            ConnectionCls = Connection
        return Pool

    classes = {"http": pinned(HTTPConnectionPool), "https": pinned(HTTPSConnectionPool)}
    for adapter in session.adapters.values():
        adapter.poolmanager.pool_classes_by_scheme = classes


class RequestsTransport:
    """Production transport. Bounded body, redirects followed by requests."""

    def __init__(self, *, verify=True, timeout=(10, 60), max_bytes=DEFAULT_MAX_BYTES, resolver=system_resolve):
        import requests
        self._requests = requests
        self.resolve = resolver
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.verify = verify
        self.timeout = timeout
        self.max_bytes = max_bytes
        _pin_connections(self.session, resolver)

    def request(self, method, url, *, headers=None, data=None):
        try:
            # Redirects are handled hop by hop by Fetcher (gate, budget, robots, holds).
            with self.session.request(method, url, headers=headers or {}, data=data, stream=True,
                                      timeout=self.timeout, allow_redirects=False) as r:
                declared = str(r.headers.get("Content-Length") or "").strip()
                if declared.isdigit() and int(declared) > self.max_bytes:
                    raise BodyTooLarge("body_too_large")  # refused before reading the body
                chunks, size = [], 0
                for chunk in r.iter_content(64 * 1024):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise BodyTooLarge("body_too_large")
                    chunks.append(chunk)
                return Response(r.status_code, dict(r.headers), b"".join(chunks), r.url)
        except self._requests.RequestException as exc:
            raise TransportError(type(exc).__name__) from exc


class Fetcher:
    def __init__(self, site_id, transport, gate_dir, *, interval=1.5, max_requests=None,
                 max_runtime_s=None, retries=3, backoff_s=2.0, max_retry_wait_s=120,
                 respect_robots=True, user_agent=DEFAULT_USER_AGENT, encoding=None,
                 extra_headers=None, rate_limit_stop=3, holds=None, max_redirects=5,
                 clock=time.monotonic, sleep=time.sleep, resolver=None):
        if not 0 <= float(interval) <= 10:
            raise ValueError("request_interval_s must be within [0, 10]")
        self.site_id = site_id
        self.transport = transport
        self.gate_dir = Path(gate_dir)
        self.interval = float(interval)
        self.max_requests = max_requests
        self.max_runtime_s = max_runtime_s
        self.retries = max(1, int(retries))
        self.backoff_s = backoff_s
        self.max_retry_wait_s = max_retry_wait_s
        self.respect_robots = respect_robots
        self.user_agent = user_agent
        self.encoding = encoding
        self.extra_headers = dict(extra_headers or {})
        self.clock = clock
        self.sleep = sleep
        self.started = clock()
        self.requests_made = 0
        self.blocked = None
        self.rate_limit_stop = int(rate_limit_stop)
        self._consecutive_429 = 0
        self.holds = holds
        self.max_redirects = int(max_redirects)
        # Destination check before every request/hop; the production transport also
        # pins the connection to the checked address.
        self.resolver = resolver or getattr(transport, "resolve", None)
        self._gates = {}
        self._host_delay = {}
        self._robots = {}
        self._robots_failed = {}  # origin -> (retry_after monotonic, reason)
        self._timing = {}  # host -> aggregated request timing (diagnostics)
        self._slow = []    # slowest requests (at most SLOW_SAMPLES)

    # -- gate ----------------------------------------------------------
    def _gate(self, host):
        delay = max(self.interval, self._host_delay.get(host, 0))
        gate = self._gates.get(host)
        if gate is None or gate.delay != min(delay, 10):
            root = self.gate_dir / hashlib.sha256(host.encode()).hexdigest()[:20]
            gate = self._gates[host] = RequestGate(root, delay=min(delay, 10))
        return gate

    def _check_budget(self):
        if self.max_requests is not None and self.requests_made >= self.max_requests:
            raise BudgetExhausted("request_budget")
        if self.max_runtime_s is not None and self.clock() - self.started >= self.max_runtime_s:
            raise BudgetExhausted("time_budget")

    def _check_holds(self, url):
        if self.blocked is not None:
            raise AccessBlocked(self.blocked.reason, url, self.blocked.status)
        host = host_of(url)
        held = None
        if self.holds is not None and host:
            held = self.holds.host_hold(host)
            if not held and "xn--" in host:
                try:  # a hold may name the host in its Unicode form
                    held = self.holds.host_hold(host.encode("ascii").decode("idna"))
                except UnicodeError:
                    pass
        if held:
            self.blocked = AccessBlocked(f"host_access_hold:{held['reason']}", url)
            raise self.blocked

    def _once(self, url, *, method="GET", data=None, content_type=None):
        """One gated request. Returns Response or raises TransportError."""
        self._check_budget()
        host = host_of(url)
        if not host or urlsplit(url).scheme not in ("http", "https"):
            raise PermanentFailure("invalid_url", url)
        checked_addresses(url, host, self.resolver)
        headers = {"User-Agent": self.user_agent,
                   "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8"}
        headers.update(self.extra_headers)
        if content_type:
            headers["Content-Type"] = content_type
        delay = self._host_delay.get(host, 0)
        gate = self._gate(host)
        began = sent = self.clock()
        outcome = None
        try:
            with gate.enter(url):
                if delay > gate.delay:
                    # The gate waits at most 10 s after the previous response on this host.
                    # A longer Crawl-delay is completed here, inside the gate (host lock held),
                    # measured from that same previous response end.
                    remaining = gate._read().get("last_end", 0) + delay - time.time()
                    if self.max_runtime_s is not None and \
                            self.clock() + max(remaining, 0) - self.started >= self.max_runtime_s:
                        raise BudgetExhausted("time_budget")  # never sleep past the budget
                    if remaining > 0:
                        self.sleep(remaining)
                self.requests_made += 1
                sent = self.clock()
                try:
                    # Only a POST passes a body (transports written for GET keep working).
                    resp = self.transport.request(method, url, headers=headers,
                                                  **({"data": data} if method != "GET" else {}))
                except TransportError as exc:
                    outcome = f"transport:{exc}"
                    raise
                outcome = resp.status
                return resp
        except NetworkDeferred as exc:
            # Another site id (or run) on this host is cooling down after a 429.
            raise RateLimited("host_cooldown", url, 429, retry_at=exc.retry_at) from None
        finally:
            if outcome is not None:
                self._record_timing(host, url, sent - began, self.clock() - sent, outcome)

    # -- diagnostics -----------------------------------------------------
    def _record_timing(self, host, url, wait, response, outcome):
        t = self._timing.setdefault(host, {"requests": 0, "gate_wait_s": 0.0, "gate_wait_max_s": 0.0,
                                           "response_s": 0.0, "response_max_s": 0.0})
        t["requests"] += 1
        t["gate_wait_s"] += wait
        t["gate_wait_max_s"] = max(t["gate_wait_max_s"], wait)
        t["response_s"] += response
        t["response_max_s"] = max(t["response_max_s"], response)
        if wait + response >= SLOW_REQUEST_S:
            self._slow.append({"host": host, "url": url[:300], "gate_wait_s": round(wait, 3),
                               "response_s": round(response, 3), "outcome": outcome,
                               "crawl_delay_s": self._host_delay.get(host)})
            self._slow.sort(key=lambda s: -(s["gate_wait_s"] + s["response_s"]))
            del self._slow[SLOW_SAMPLES:]

    def timing_summary(self, *, reset=False):
        """Per-host request timing since the last reset: count, gate wait (spacing incl.
        Crawl-delay), response time, the Crawl-delay applied, and the slowest requests."""
        hosts = {}
        for host, t in self._timing.items():
            n = t["requests"] or 1
            hosts[host] = {"requests": t["requests"],
                           "gate_wait_avg_s": round(t["gate_wait_s"] / n, 3),
                           "gate_wait_max_s": round(t["gate_wait_max_s"], 3),
                           "response_avg_s": round(t["response_s"] / n, 3),
                           "response_max_s": round(t["response_max_s"], 3),
                           "crawl_delay_s": self._host_delay.get(host),
                           "effective_gap_s": max(self.interval, self._host_delay.get(host, 0))}
        summary = {"hosts": hosts, "slow_samples": list(self._slow), "slow_threshold_s": SLOW_REQUEST_S}
        if reset:
            self._timing, self._slow = {}, []
        return summary

    def _defer(self, url, retry_at):
        """Persist a 429 cooldown in the host gate, visible to every Fetcher on the host."""
        gate = self._gate(host_of(url))
        try:
            with gate.enter(url):
                gate.defer(url, retry_at)
        except NetworkDeferred:
            pass

    # -- robots ----------------------------------------------------------
    def _robots_allows(self, url):
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            failed = self._robots_failed.get(origin)
            if failed and self.clock() < failed[0]:
                raise RobotsUnavailable(failed[1], url)
            try:
                self._robots[origin] = self._load_robots(origin)
            except (TransientFailure, PermanentFailure) as exc:
                # PermanentFailure here is body_too_large: no readable file, no permission.
                reason = f"robots_unavailable:{exc.reason}"
                self._robots_failed[origin] = (self.clock() + ROBOTS_RETRY_S, reason)
                raise RobotsUnavailable(reason, url, exc.status) from None
            self._robots_failed.pop(origin, None)
        rule = self._robots[origin]
        if rule == "too_slow":
            raise self._block("robots_crawl_delay_too_long", url, host_wide=False)
        if rule == "unparseable":
            # Never read a file we could not parse as permission.
            raise self._block("robots_unparseable", url, host_wide=False)
        return True if rule == "allow_all" else rule.allows(url, self.user_agent)

    def _load_robots(self, origin):
        """404/410 and other 4xx: no rules. 401/403/407/451, a challenge/login page or a
        redirect to a login path: hold (never read as permission). 5xx/429/transport errors
        after the retries: TransientFailure, turned into a RobotsUnavailable stop by the caller."""
        url = origin + "/robots.txt"
        for _hop in range(self.max_redirects + 1):
            resp = self._with_retries(url, classify=False)
            if 300 <= resp.status < 400 and resp.header("location"):
                nxt = urljoin(url, location_of(resp))
                if _LOGIN_PATH.search(urlsplit(nxt).path or ""):
                    raise self._block("robots_login_redirect", url, resp.status)
                try:
                    url = governed_url(nxt)
                except PermanentFailure as exc:
                    # Never read as "no rules": the caller stops the site (robots_unavailable).
                    raise TransientFailure(f"redirect_{exc.reason}", nxt) from None
                continue
            break
        else:
            raise self._block("robots_redirect_loop", url)
        if resp.status in (401, 403, 407, 451):
            raise self._block(f"robots_{resp.status}", url, resp.status)
        if not 200 <= resp.status < 300:
            return "allow_all"
        if self._challenge(resp) or login_page(resp.body):
            raise self._block("robots_challenge", url, resp.status)
        text = resp.text()
        try:
            rules = parse_robots(text)
            wanted = max(crawl_delay(text, self.user_agent), rules.request_rate(self.user_agent))
        except Exception:  # noqa: BLE001 - any parser failure means "no permission inferred"
            return "unparseable"
        if wanted > MAX_CRAWL_DELAY_S:
            return "too_slow"
        if wanted > self.interval:
            # Keyed to the host whose pages are requested, even if robots.txt was redirected.
            self._host_delay[host_of(origin)] = wanted
        return rules

    # -- public ----------------------------------------------------------
    def _with_retries(self, url, *, classify=True, accept_statuses=(), method="GET", data=None, content_type=None):
        last = None
        for attempt in range(self.retries):
            wait = self.backoff_s * (3 ** attempt)
            try:
                resp = self._once(url, method=method, data=data, content_type=content_type)
            except BodyTooLarge:
                raise PermanentFailure("body_too_large", url) from None
            except TransportError as exc:
                last = TransientFailure(f"transport:{exc}", url)
            except TransientFailure as exc:
                if exc.reason != "dns_unresolved":
                    raise
                last = exc  # resolver hiccup: retried with the same backoff, nothing was requested
            else:
                self._consecutive_429 = self._consecutive_429 + 1 if resp.status == 429 else 0
                if resp.status == 429 or resp.status >= 500:
                    last = TransientFailure("rate_limited" if resp.status == 429 else f"http_{resp.status}",
                                            url, resp.status)
                    if resp.status == 429:
                        until = retry_after(resp.header("retry-after"), now=time.time())
                        wait = max(wait, until - time.time())
                        if self._consecutive_429 >= self.rate_limit_stop or wait > self.max_retry_wait_s:
                            retry_at = max(until, time.time() + self.max_retry_wait_s)
                            self._defer(url, retry_at)
                            raise RateLimited("rate_limited_cooldown", url, 429, retry_at=retry_at)
                    elif resp.status == 503 and resp.header("retry-after"):
                        # Maintenance with a stated return time: wait for it, or stop with a
                        # host-wide cooldown when it is longer than we may wait.
                        until = retry_after(resp.header("retry-after"), now=time.time())
                        wait = max(wait, until - time.time())
                        if wait > self.max_retry_wait_s:
                            self._defer(url, until)
                            raise RateLimited("unavailable_retry_after", url, 503, retry_at=until)
                else:
                    return self._classify(resp, url, accept_statuses) if classify else resp
            if attempt < self.retries - 1:
                self.sleep(min(wait, self.max_retry_wait_s))
        raise last

    @staticmethod
    def _challenge(resp):
        if not ("html" in resp.content_type.lower()
                or resp.body[:200].lstrip().lower().startswith((b"<!doctype", b"<html"))):
            return None
        challenge = access_challenge(resp.body)
        lower = resp.body[:131072].lower()
        if not challenge and (b"<title>just a moment" in lower or (b"cf-chl-" in lower and b"challenge-platform" in lower)):
            challenge = "browser_security_challenge"
        return challenge

    def _classify(self, resp, url, accept_statuses):
        status = resp.status
        # Refusals are classified before any plan-accepted status: a plan can
        # never turn 401/403/407/451 into an ordinary end page.
        if status in (401, 407):
            raise self._block("authentication_required", url, status)
        if status in (403, 451):
            raise self._block("access_refused", url, status)
        if 300 <= status < 400:
            if resp.header("location"):
                return resp  # followed hop by hop by get()
            raise PermanentFailure("http_3xx_without_location", url, status)
        if status in accept_statuses:
            return resp
        if status in (404, 410):
            raise PermanentFailure(f"http_{status}", url, status)
        if status >= 400:
            raise PermanentFailure("http_4xx", url, status)
        final = resp.url or url
        if final != url and _LOGIN_PATH.search(urlsplit(final).path or ""):
            raise self._block("login_redirect", url, status)
        challenge = self._challenge(resp)
        if challenge:
            raise self._block(f"challenge:{challenge}", url, status)
        if "html" in resp.content_type.lower() or resp.body[:200].lstrip().lower().startswith((b"<!doctype", b"<html")):
            # Not a stop by itself: listings with items ignore it, the strategy
            # decides for empty listings and the runner ledgers detail pages.
            resp.login_suspect = login_page(resp.body)
        return resp

    def _block(self, reason, url, status=None, *, host_wide=True):
        self.blocked = AccessBlocked(reason, url, status)
        if host_wide and self.holds is not None and host_of(url):
            self.holds.hold_host(host_of(url), reason, url, self.site_id)
        return self.blocked

    def get(self, url, *, accept_statuses=()):
        """Fetch ``url`` through the gate, following redirects hop by hop.
        Returns a 2xx (or accepted) Response.

        Raises PermanentFailure, TransientFailure, AccessBlocked, RateLimited, RobotsUnavailable or
        BudgetExhausted.
        """
        accept_statuses = tuple(s for s in accept_statuses if s not in REFUSAL_STATUSES)
        current = url
        for _hop in range(self.max_redirects + 1):
            normalised = governed_url(current)
            if normalised != current:
                self._check_holds(current)  # a hold may name the host in its Unicode form
            current = normalised
            self._check_holds(current)
            if not self._robots_allows(current):
                # A path-level rule: never a host hold, and never requested.
                raise RobotsDisallowed("robots_disallowed", current)
            resp = self._with_retries(current, accept_statuses=accept_statuses)
            if 300 <= resp.status < 400:
                nxt = urljoin(current, location_of(resp))
                if _LOGIN_PATH.search(urlsplit(nxt).path or ""):
                    raise self._block("login_redirect", current, resp.status)
                current = nxt
                continue
            resp.url = resp.url or current
            resp._encoding = resp._encoding or self.encoding
            return resp
        raise PermanentFailure("too_many_redirects", url)

    def post(self, url, body, *, body_type="form", accept_statuses=()):
        """POST a listing query (plans may use it only for idempotent search APIs) through the
        same gate, budget, hold, robots and destination checks as ``get``. A redirect is never
        followed (a POST cannot be replayed safely at another address)."""
        from urllib.parse import urlencode
        accept_statuses = tuple(s for s in accept_statuses if s not in REFUSAL_STATUSES)
        if body_type == "json":
            data, ctype = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8"), "application/json"
        elif body_type == "form":
            data, ctype = urlencode(body, doseq=True).encode("ascii"), "application/x-www-form-urlencoded"
        else:
            raise PermanentFailure(f"unknown_body_type:{body_type}", url)
        current = governed_url(url)
        if current != url:
            self._check_holds(url)
        self._check_holds(current)
        if not self._robots_allows(current):
            raise RobotsDisallowed("robots_disallowed", current)
        resp = self._with_retries(current, accept_statuses=accept_statuses, method="POST", data=data,
                                  content_type=ctype)
        if 300 <= resp.status < 400:
            raise PermanentFailure("post_redirect_not_followed", current, resp.status)
        resp.url = resp.url or current
        resp._encoding = resp._encoding or self.encoding
        return resp

"""URL assembly that never double-encodes pre-encoded filter keys.

Plans and legacy configs mix two spellings of the same query key:
``type[170]`` and ``type%5B170%5D``. Passing the second form to requests as a
params key yields ``type%255B170%255D``, a different (usually ignored) filter.
Every query here is decoded exactly once and encoded exactly once.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit, urlunsplit

_PCT = re.compile(r"%[0-9A-Fa-f]{2}")


def _decode_once(value):
    value = "" if value is None else str(value)
    # Encoded query text uses '+' for spaces (application/x-www-form-urlencoded).
    return unquote_plus(value) if _PCT.search(value) else value


def decode_preencoded(params):
    """Return params with already-percent-encoded keys/values decoded once.

    Only strings containing a ``%XX`` escape are touched, so a literal value
    such as ``100%`` survives unchanged.
    """
    if params is None:
        return {}
    items = params.items() if isinstance(params, dict) else params
    return {_decode_once(k): v if isinstance(v, (list, tuple)) else _decode_once(v) for k, v in items}


def build_url(base, params=None, *, drop=()):
    """Merge ``params`` into ``base``'s query (params win), encoding once.

    ``drop`` removes keys from the base query (e.g. a page parameter that
    must be absent for the unparameterized first-page check).
    """
    parts = urlsplit(base)
    merged = {}
    order = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key in drop:
            continue
        if key not in merged:
            order.append(key)
        merged[key] = value
    for key, value in decode_preencoded(params).items():
        if key in drop:
            continue
        if key not in merged:
            order.append(key)
        merged[key] = value
    pairs = []
    for key in order:
        value = merged[key]
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            pairs.extend((key, str(v)) for v in value)
        else:
            pairs.append((key, str(value)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), ""))


def normalize_key_url(url):
    """Identity form of a detail URL: fragment dropped, nothing else changed."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, parts.query, ""))


def host_of(url):
    return (urlsplit(url).hostname or "").lower()


# List-state parameters boards embed in detail links (page number, offsets...).
LIST_STATE_PARAMS = {"page", "p", "pageindex", "pageno", "pagenum", "page_no", "page_num", "pagenumber",
                     "currentpage", "cpage", "npage", "start", "offset"}


def url_identity(url, ignore=()):
    """Comparable form of a detail URL: fragment dropped, host lower-cased, query
    parameters sorted, list-state parameters (and ``ignore``) removed."""
    parts = urlsplit(url or "")
    drop = {k.lower() for k in ignore} | LIST_STATE_PARAMS
    pairs = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in drop)
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, urlencode(pairs), ""))

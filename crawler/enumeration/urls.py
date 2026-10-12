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
    """Merge ``params`` into ``base``'s query, encoding once.

    The base query is kept as ordered multi-value pairs (``f=a&f=b``,
    ``fq=..&fq=..``, blank values). A key given in ``params`` replaces *all*
    of its base values at the position of its first occurrence (a list value
    yields several pairs); other keys are never touched. ``drop`` removes keys.
    """
    parts = urlsplit(base)
    overrides = {k: v for k, v in decode_preencoded(params).items() if k not in drop}

    def values(value):
        if value is None:
            return []
        return [str(v) for v in value] if isinstance(value, (list, tuple)) else [str(value)]

    pairs, emitted = [], set()
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key in drop:
            continue
        if key in overrides:
            if key not in emitted:
                pairs.extend((key, v) for v in values(overrides[key]))
                emitted.add(key)
            continue
        pairs.append((key, value))
    for key, value in overrides.items():
        if key not in emitted:
            pairs.extend((key, v) for v in values(value))
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

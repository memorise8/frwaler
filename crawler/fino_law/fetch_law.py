from __future__ import annotations

import os
import time

import httpx

_OC = os.environ.get("LAW_API_OC", "fino-law-data")
_SEARCH = "http://www.law.go.kr/DRF/lawSearch.do"
_SERVICE = "http://www.law.go.kr/DRF/lawService.do"


def _get(client: httpx.Client, url: str, params: dict, delay: float) -> dict:
    last: Exception | None = None
    for attempt in range(3):
        if delay > 0:
            time.sleep(delay)
        try:
            r = client.get(url, params=params)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
            time.sleep(1 + attempt)
    raise RuntimeError(f"law.go.kr fetch failed: {url}") from last


def search_law(client: httpx.Client, name: str, delay: float = 0.3) -> dict:
    return _get(client, _SEARCH,
                {"OC": _OC, "target": "law", "type": "JSON", "query": name, "display": "10"}, delay)


def fetch_law_service(client: httpx.Client, mst: str, delay: float = 0.3) -> dict:
    return _get(client, _SERVICE,
                {"OC": _OC, "target": "law", "type": "JSON", "MST": mst}, delay)

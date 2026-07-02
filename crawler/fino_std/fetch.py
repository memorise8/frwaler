from __future__ import annotations

import time

import httpx

BASE = "https://db.kasb.or.kr"
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"


def is_unavailable(data: dict | None) -> bool:
    """서버가 미지원 기준서에 주는 오류 페이로드({"message": ...}) 여부."""
    if data is None:
        return True
    return "message" in data and "titles" not in data and "clauses" not in data


def _get_json(client: httpx.Client, path: str, delay: float) -> dict | None:
    for attempt in range(3):
        if delay > 0:
            time.sleep(delay)
        try:
            resp = client.get(f"{BASE}{path}", headers={"User-Agent": _UA}, timeout=30)
            if resp.status_code == 500:  # 미지원 기준서 — 재시도 무의미
                return None
            if resp.status_code == 200:
                return resp.json()
        except (httpx.HTTPError, ValueError):
            pass
        if attempt < 2:
            time.sleep(1 + attempt)
    return None


def fetch_titles(client: httpx.Client, std_num: int, delay: float = 0.4) -> dict | None:
    return _get_json(client, f"/api/title/{std_num}", delay)


def fetch_content(client: httpx.Client, std_num: int, document_id: str, delay: float = 0.4) -> dict | None:
    return _get_json(client, f"/api/content/{std_num}/{document_id}", delay)

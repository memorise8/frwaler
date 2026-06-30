from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from .models import Target
from .pagination import paged_url


PageMethod = Literal["GET", "POST"]


@dataclass(frozen=True, slots=True)
class PageRequest:
    url: str
    external_id: str
    method: PageMethod = "GET"
    data: dict[str, str] | None = None


def direct_page_request(url: str, external_id: str) -> PageRequest:
    return PageRequest(url=url, external_id=external_id)


def kasb_detail_request(seq: str, ctg_cd: str, site_cd: str = "002000000000000") -> PageRequest:
    return PageRequest(
        url=f"https://www.kasb.or.kr/front/board/View{ctg_cd}.do",
        external_id=f"{ctg_cd}-{seq}",
        method="POST",
        data={"seq": seq, "ctgCd": ctg_cd, "siteCd": site_cd},
    )


def page_request_for_target(target: Target, page: int) -> PageRequest:
    if _is_kasb_url(target.url):
        return PageRequest(
            url=target.url,
            external_id=f"{target.url}#page={page}",
            method="POST",
            data={
                "siteCd": "002000000000000",
                "replySummary": "Y",
                "page": str(page),
            },
        )
    return PageRequest(
        url=paged_url(target.url, page),
        external_id=f"{target.url}#page={page}",
    )


def _is_kasb_url(url: str) -> bool:
    return urlparse(url).netloc.endswith("kasb.or.kr")

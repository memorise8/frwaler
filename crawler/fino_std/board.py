"""회계기준 공표 게시판 — 기준서별 현재 판본 대장.

조문 API(`db.kasb.or.kr/api/`)만으로는 회계기준 전체를 덮지 못한다. 보험업
회계처리준칙(제91장)·재무제표 영문양식(제93장)·제1118호는 API가 500이나 빈
응답을 주고, 폐지된 구기준도 없다. 그런데 공표 게시판에는 전부 있다.

게시판이 더 중요한 이유는 따로 있다. **첨부 파일명에 판본이 박혀 있다.**

    시행중_K-IFRS_제1101호_..._(2024_개정_..._수정목록_26-1_...).pdf
    보유분                        (2023_개정_..._수정목록_24-1_...)

파일명만 대조하면 무엇이 낡았는지 바로 나온다. 본문을 내려받아 비교할 필요가
없으므로 최신화 감지를 값싸게 돌릴 수 있다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

import httpx

BASE: Final = "https://www.kasb.or.kr"
_UA: Final = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
)
DOWNLOAD_PATH: Final = "/commonFile/fileDownload.do"


@dataclass(frozen=True, slots=True)
class BoardSource:
    code: str
    title: str
    path: str


# 회계기준 공표 게시판. 어느 것도 페이지를 나누지 않아 한 번 요청으로 전부 온다.
BOARD_SOURCES: Final[tuple[BoardSource, ...]] = (
    BoardSource("kifrs", "한국채택국제회계기준(K-IFRS)", "/front/board/ingAccountingList.do"),
    BoardSource("gaap", "일반기업회계기준", "/front/board/List3003.do"),
    BoardSource("special", "특수분야회계기준", "/front/board/List3004.do"),
    BoardSource("sme", "중소기업회계기준", "/front/board/List3005.do"),
    BoardSource("nonprofit", "비영리조직회계기준", "/front/board/List3006.do"),
    BoardSource("former", "종전기업회계기준", "/front/board/List3008.do"),
)


@dataclass(frozen=True, slots=True)
class PublishedFile:
    board: str          # BoardSource.code
    entry_title: str    # 게시판 행 제목 — "제5001호 결합재무제표"
    file_name: str      # 첨부 파일명 — 판본이 여기 들어 있다
    file_no: str        # fileDownload 1번 인자
    file_seq: str       # fileDownload 2번 인자

    @property
    def ext(self) -> str:
        return self.file_name.rsplit(".", 1)[-1].lower() if "." in self.file_name else ""


_ROW: Final = re.compile(r"<tr\b.*?</tr>", re.S | re.I)
# 제목 칸은 링크가 있는 판(fn_Detail)과 글자만 있는 판(중소기업)이 섞여 있다.
_TITLE_CELL: Final = re.compile(r'<td[^>]*class="[^"]*\bleft\b[^"]*"[^>]*>(.*?)</td>', re.S | re.I)
# 첨부는 `<li class="down_*">` 하나가 파일 하나다. li 단위로 끊어야 이웃 항목의
# 파일명을 잘못 물지 않는다. 그리고 게시판에 따라 파일번호가 음수다
# (K-IFRS 목록은 전부 `fileDownload('-49990963','2')` 꼴).
_ATTACH_ITEM: Final = re.compile(r"<li[^>]*class=\"down_[^\"]*\".*?</li>", re.S | re.I)
_ATTACH: Final = re.compile(
    r"fileDownload\('(-?\d+)','(-?\d+)'\).*?<span>\s*(.*?)\s*</span>", re.S
)
_TAG: Final = re.compile(r"<[^>]+>")


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", _TAG.sub(" ", html)).strip()


def parse_board(html: str, board: str) -> list[PublishedFile]:
    """게시판 목록 HTML → 첨부 파일 목록.

    한 행에 hwp·pdf 가 함께 달리므로 파일마다 한 건씩 만든다.
    """
    out: list[PublishedFile] = []
    for row in _ROW.findall(html):
        cell = _TITLE_CELL.search(row)
        if cell is None:
            continue
        title = _text(cell.group(1))
        for item in _ATTACH_ITEM.findall(row):
            found = _ATTACH.search(item)
            if found is None:
                continue
            file_name = _text(found.group(3))
            if file_name:
                out.append(
                    PublishedFile(
                        board=board,
                        entry_title=title,
                        file_name=file_name,
                        file_no=found.group(1),
                        file_seq=found.group(2),
                    )
                )
    return out


def fetch_board(client: httpx.Client, source: BoardSource) -> list[PublishedFile]:
    resp = client.get(f"{BASE}{source.path}", headers={"User-Agent": _UA}, timeout=40)
    _ = resp.raise_for_status()
    return parse_board(resp.text, source.code)


def download_file(client: httpx.Client, item: PublishedFile) -> bytes:
    """첨부 실물을 받는다. 게시판이 POST 폼으로만 내려주므로 GET 으로는 안 된다."""
    resp = client.post(
        f"{BASE}{DOWNLOAD_PATH}",
        data={"fileNo": item.file_no, "fileSeq": item.file_seq},
        headers={"User-Agent": _UA, "Referer": f"{BASE}/front/board/"},
        timeout=120,
    )
    _ = resp.raise_for_status()
    return resp.content

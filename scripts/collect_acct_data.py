"""acct_data.xlsx 대상 수집기 — 우선순위 18~23을 fino_acct 표준 스키마로 적재.

기존 파이프라인과 동일한 산출물을 만든다.
  - 문서: acct_documents (source_priority, external_id, title, detail_url,
          published_date, body_text ...)
  - 첨부: acct_attachments (local_path = data/fino_acct_docs/{NN}/{doc_id}_{파일명})

대상 보드(= crawler.fino_acct.sources 의 Target 18~23):
  18 KASB 행사·교육자료      List2003.do   399건
  19 KASB 기고자료           List2005.do   120건
  20 KASB 교육자료           eduAccstdList  47건 (스마트강의=YouTube 는 대상 제외)
  21 KASB 회계기준적용의견서 opinionList    23건
  22 KASB 정착지원TF         List016008     27건
  23 KICPA IFRS 실무사례     list.brd      347건

--reuse-dir 로 앞선 원본 덤프(manifest.ndjson)를 지정하면 이미 받은 첨부는
재다운로드하지 않고 복사한다.

사용법:
    python -m scripts.collect_acct_data                      # 전체
    python -m scripts.collect_acct_data --priorities 20,21   # 일부
    python -m scripts.collect_acct_data --limit-items 3      # 스모크
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler.fino_acct.db import connect_db, init_schema, upsert_attachment, upsert_document
from crawler.fino_acct.fetch import download_attachment, new_session, safe_filename
from crawler.fino_acct.models import AttachmentLink, Target
from crawler.fino_acct.parsers import clean_text, kasb_attachment, page_body
from crawler.fino_acct.sources import TARGETS

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path("data/fino_acct.db")
DOWNLOAD_ROOT = Path("data/fino_acct_docs")
REUSE_DIR = Path("data/acct_data_20260805")
DELAY = 0.35

KASB_BASE = "https://www.kasb.or.kr/front/board"
SITE_CD = "002000000000000"
FN_DETAIL_RE = re.compile(r"fn_Detail\('([^']+)'(?:\s*,\s*'([^']+)')?\)")
FILE_DOWNLOAD_RE = re.compile(r"fileDownload\('([^']+)'\s*,\s*'([^']+)'\)")
DATE_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})")
REG_DATE_RE = re.compile(r"등록일\s*[:\s]*(20\d{2}-\d{2}-\d{2})")
# fn_Detail 인자 2개일 때 게시판 코드로 인정할 값(나머지 인자가 seq).
KNOWN_CTG = {"2003", "2004", "2005", "016008", "016009"}

KICPA_BOARD = "accstd02"
KICPA_LIST = "https://www.kicpa.or.kr/board/list.brd"
KICPA_READ = "https://www.kicpa.or.kr/board/read.brd"
KICPA_BLTN_RE = re.compile(r"readBulletin\('([^']+)'\s*,\s*'([^']+)'\)")
KICPA_ENTRY = (
    "https://www.kicpa.or.kr/portal/default/kicpa/gnb/kr_pc/menu08/menu03/menu13.page"
)

# priority -> (목록 URL, 상세 페이지 파일명, 단일인자 보드의 external_id 접두어)
BOARDS: dict[int, tuple[str, str, str | None]] = {
    18: (f"{KASB_BASE}/List2003.do", "View{ctg}.do", None),
    19: (f"{KASB_BASE}/List2005.do", "View{ctg}.do", None),
    20: (f"{KASB_BASE}/eduAccstdList.do", "eduAccstdView.do", "edu"),
    21: (f"{KASB_BASE}/opinionList.do", "opinionView.do", "opinion"),
    22: (f"{KASB_BASE}/List016008.do", "View{ctg}.do", None),
}


# --------------------------------------------------------------------------- 재사용 풀


def load_reuse_pool(reuse_dir: Path) -> dict[str, Path]:
    manifest = reuse_dir / "manifest.ndjson"
    if not manifest.exists():
        return {}
    pool: dict[str, Path] = {}
    with manifest.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("status") != "downloaded" or not row.get("path"):
                continue
            path = reuse_dir / row["path"]
            if path.exists():
                pool[row["key"]] = path
    return pool


def place_attachment(
    session,
    *,
    link: AttachmentLink,
    reuse_key: str,
    reuse_pool: dict[str, Path],
    doc_id: int,
    priority: int,
    stats: dict,
) -> tuple[Path, int, str] | None:
    """첨부를 표준 위치에 놓고 (경로, 크기, content_type) 반환."""
    target_dir = REPO_ROOT / DOWNLOAD_ROOT / f"{priority:02d}"
    target_dir.mkdir(parents=True, exist_ok=True)
    source = reuse_pool.get(reuse_key)
    if source is not None:
        destination = target_dir / safe_filename(f"{doc_id}_{link.filename}")
        if not destination.exists():
            _ = shutil.copy2(source, destination)
        stats["reused"] += 1
        return destination, destination.stat().st_size, "application/octet-stream"

    result = download_attachment(session, link, target_dir, str(doc_id), DELAY)
    if result.local_path is None:
        stats["failed"] += 1
        return None
    stats["downloaded"] += 1
    return result.local_path, result.size_bytes, result.content_type


def register(
    conn,
    session,
    *,
    target: Target,
    external_id: str,
    title: str,
    detail_url: str,
    published_date: str,
    body_text: str,
    attachments: list[tuple[AttachmentLink, str]],
    reuse_pool: dict[str, Path],
    stats: dict,
) -> None:
    doc_id = upsert_document(
        conn,
        source_priority=target.priority,
        agency=target.agency,
        target_name=target.target_name,
        source_url=target.url,
        source_type=target.source_type,
        source_subtype=target.source_subtype,
        index_name=target.index_name,
        external_id=external_id,
        title=title,
        detail_url=detail_url,
        published_date=published_date,
        body_text=body_text,
    )
    stats["documents"] += 1
    for link, reuse_key in attachments:
        placed = place_attachment(
            session,
            link=link,
            reuse_key=reuse_key,
            reuse_pool=reuse_pool,
            doc_id=doc_id,
            priority=target.priority,
            stats=stats,
        )
        if placed is None:
            continue
        path, size, content_type = placed
        upsert_attachment(
            conn,
            document_id=doc_id,
            url=link.url,
            filename=link.filename,
            local_path=str(path.relative_to(REPO_ROOT)),
            content_type=content_type,
            size_bytes=size,
            status="downloaded",
        )
        stats["attachments"] += 1


# --------------------------------------------------------------------------- KASB


def parse_fn_detail(onclick: str) -> tuple[str, str] | None:
    """fn_Detail 인자 → (ctg, seq). 보드마다 인자 순서가 달라 코드 기준으로 판별."""
    match = FN_DETAIL_RE.search(onclick)
    if match is None:
        return None
    first, second = match.group(1), match.group(2)
    if second is None:
        return "", first
    if first in KNOWN_CTG:
        return first, second
    if second in KNOWN_CTG:
        return second, first
    return second, first  # 기존 파서와 동일한 (seq, ctg) 가정


def kasb_rows(soup: BeautifulSoup, base_url: str) -> list[dict]:
    rows: list[dict] = []
    for row in soup.select("tbody tr"):
        item = None
        title = ""
        attachments: list[AttachmentLink] = []
        for anchor in row.find_all("a"):
            onclick = str(anchor.get("onclick", ""))
            if FILE_DOWNLOAD_RE.search(onclick):
                link = kasb_attachment(base_url, onclick, anchor.get_text(" ", strip=True))
                if link is not None:
                    attachments.append(link)
                continue
            parsed = parse_fn_detail(onclick)
            if parsed is not None and item is None:
                item = parsed
                title = clean_text(anchor.get_text(" ", strip=True))
        if item is None:
            continue
        row_text = clean_text(row.get_text(" ", strip=True))
        date_match = DATE_RE.search(row_text)
        rows.append(
            {
                "ctg": item[0],
                "seq": item[1],
                "title": title,
                "row_date": date_match.group(1) if date_match else "",
                "attachments": attachments,
            }
        )
    return rows


def collect_kasb_board(
    conn, session, target: Target, reuse_pool: dict[str, Path], stats: dict, limit: int | None
) -> None:
    list_url, view_pattern, prefix = BOARDS[target.priority]
    seen: set[tuple[str, str]] = set()
    page = 1
    while True:
        response = session.get(list_url, params={"page": page}, timeout=60)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        rows = [r for r in kasb_rows(soup, list_url) if (r["ctg"], r["seq"]) not in seen]
        if not rows:
            break
        for row in rows:
            seen.add((row["ctg"], row["seq"]))
            if limit is not None and stats["documents"] >= limit:
                return
            ctg, seq = row["ctg"], row["seq"]
            view = view_pattern.format(ctg=ctg) if "{ctg}" in view_pattern else view_pattern
            detail_url = f"{KASB_BASE}/{view}?siteCd={SITE_CD}&seq={seq}"
            external_id = f"{ctg}-{seq}" if ctg else f"{prefix}-{seq}"

            body_text = ""
            published = row["row_date"]
            title = row["title"]
            try:
                detail = session.get(detail_url, timeout=30)
                if detail.status_code == 200:
                    detail_soup = BeautifulSoup(detail.content, "html.parser")
                    body_text = page_body(detail_soup)
                    heading = detail_soup.find("h3")
                    if heading is not None and clean_text(heading.get_text(" ", strip=True)):
                        title = clean_text(heading.get_text(" ", strip=True))
                    reg = REG_DATE_RE.search(body_text) or DATE_RE.search(body_text)
                    if reg:
                        published = reg.group(1)
                else:
                    stats["page_errors"] += 1
            except Exception as exc:  # noqa: BLE001 - 문서 단위 실패는 기록만
                print(f"    [ERR] {detail_url} :: {exc}", flush=True)
                stats["page_errors"] += 1

            attachments = [(link, f"kasb:{link.post_file_no}:{link.post_file_seq}") for link in row["attachments"]]
            register(
                conn,
                session,
                target=target,
                external_id=external_id,
                title=title or external_id,
                detail_url=detail_url,
                published_date=published,
                body_text=body_text,
                attachments=attachments,
                reuse_pool=reuse_pool,
                stats=stats,
            )
            print(
                f"    [{target.priority}] {external_id} 첨부 {len(attachments)} | {title[:52]}",
                flush=True,
            )
            time.sleep(DELAY)
        page += 1


# --------------------------------------------------------------------------- KICPA


def collect_kicpa(
    conn, session, target: Target, reuse_pool: dict[str, Path], stats: dict, limit: int | None
) -> None:
    _ = session.get(KICPA_ENTRY, timeout=30)  # jsessionid 확보
    seen: set[str] = set()
    page = 1
    while True:
        response = session.get(
            KICPA_LIST, params={"boardId": KICPA_BOARD, "page": page}, timeout=30
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        items = []
        for anchor in soup.find_all("a"):
            match = KICPA_BLTN_RE.search(str(anchor.get("onclick", "")))
            if match is not None and match.group(2) not in seen:
                items.append(
                    {
                        "bltn_no": match.group(2),
                        "title": clean_text(anchor.get_text(" ", strip=True)),
                    }
                )
        if not items:
            break
        for item in items:
            seen.add(item["bltn_no"])
            if limit is not None and stats["documents"] >= limit:
                return
            detail_url = f"{KICPA_READ}?boardId={KICPA_BOARD}&bltnNo={item['bltn_no']}"
            body_text = ""
            published = ""
            attachments: list[tuple[AttachmentLink, str]] = []
            try:
                detail = session.get(detail_url, timeout=30)
                if detail.status_code == 200:
                    detail_soup = BeautifulSoup(detail.content, "html.parser")
                    body_text = page_body(detail_soup)
                    reg = re.search(r"등록일\s*(20\d{2})\.(\d{2})\.(\d{2})", body_text)
                    if reg:
                        published = f"{reg.group(1)}-{reg.group(2)}-{reg.group(3)}"
                    for anchor in detail_soup.find_all("a", href=True):
                        href = str(anchor.get("href", ""))
                        if "fileMngr" not in href:
                            continue
                        seq_match = re.search(r"fileSeq=(\d+)", href)
                        seq = seq_match.group(1) if seq_match else "1"
                        link = AttachmentLink(
                            url=f"https://www.kicpa.or.kr{href}" if href.startswith("/") else href,
                            filename=clean_text(anchor.get_text(" ", strip=True))
                            or f"{item['bltn_no']}.bin",
                            method="GET",
                        )
                        attachments.append((link, f"kicpa:{item['bltn_no']}:{seq}"))
                else:
                    stats["page_errors"] += 1
            except Exception as exc:  # noqa: BLE001
                print(f"    [ERR] {detail_url} :: {exc}", flush=True)
                stats["page_errors"] += 1

            register(
                conn,
                session,
                target=target,
                external_id=f"kicpa-{item['bltn_no']}",
                title=item["title"],
                detail_url=detail_url,
                published_date=published,
                body_text=body_text,
                attachments=attachments,
                reuse_pool=reuse_pool,
                stats=stats,
            )
            print(
                f"    [23] kicpa-{item['bltn_no']} 첨부 {len(attachments)} | {item['title'][:52]}",
                flush=True,
            )
            time.sleep(DELAY)
        page += 1


# --------------------------------------------------------------------------- main


def main() -> int:
    global DOWNLOAD_ROOT

    parser = argparse.ArgumentParser(prog="collect_acct_data")
    _ = parser.add_argument("--db-path", type=Path, default=DB_PATH)
    _ = parser.add_argument("--download-root", type=Path, default=DOWNLOAD_ROOT)
    _ = parser.add_argument("--reuse-dir", type=Path, default=REUSE_DIR)
    _ = parser.add_argument("--priorities", default="18,19,20,21,22,23")
    _ = parser.add_argument("--limit-items", type=int, default=None, help="보드당 문서 상한(스모크)")
    args = parser.parse_args()

    DOWNLOAD_ROOT = args.download_root

    wanted = {int(x) for x in args.priorities.split(",") if x.strip()}
    reuse_pool = load_reuse_pool(REPO_ROOT / args.reuse_dir)
    print(f"[start] 재사용 풀 {len(reuse_pool)}건 | DB {args.db_path}", flush=True)

    session = new_session()
    started = time.time()
    totals = {
        "documents": 0,
        "attachments": 0,
        "reused": 0,
        "downloaded": 0,
        "failed": 0,
        "page_errors": 0,
    }

    with connect_db(REPO_ROOT / args.db_path) as conn:
        init_schema(conn)
        for target in TARGETS:
            if target.priority not in wanted or target.priority < 18:
                continue
            stats = dict.fromkeys(totals, 0)
            print(f"\n=== [{target.priority}] {target.agency} {target.target_name} ===", flush=True)
            if target.priority == 23:
                collect_kicpa(conn, session, target, reuse_pool, stats, args.limit_items)
            else:
                collect_kasb_board(conn, session, target, reuse_pool, stats, args.limit_items)
            print(
                f"  → 문서 {stats['documents']} | 첨부 {stats['attachments']} "
                f"(재사용 {stats['reused']} / 신규 {stats['downloaded']} / 실패 {stats['failed']}) "
                f"| 페이지오류 {stats['page_errors']}",
                flush=True,
            )
            for key in totals:
                totals[key] += stats[key]

    print(
        f"\n[done] {(time.time() - started) / 60:.1f}분 | 문서 {totals['documents']} "
        f"| 첨부 {totals['attachments']} (재사용 {totals['reused']} / 신규 {totals['downloaded']}) "
        f"| 실패 {totals['failed']} | 페이지오류 {totals['page_errors']}",
        flush=True,
    )
    return 0 if totals["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

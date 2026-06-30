from pathlib import Path
import sqlite3
import subprocess
import sys

from bs4 import BeautifulSoup

from crawler.fino_acct import collect as collect_mod
from crawler.fino_acct.db import connect_db, init_schema, upsert_attachment, upsert_document
from crawler.fino_acct.fetch import safe_filename
from crawler.fino_acct.models import FetchResult
from crawler.fino_acct.pagination import paged_url
from crawler.fino_acct.parsers import extract_fss_details, extract_links, extract_links_for_target
from crawler.fino_acct.sources import TARGETS
from crawler.fino_acct.target_pages import kasb_detail_request, page_request_for_target, PageRequest


def _dump_database(conn: sqlite3.Connection) -> str:
    return "\n".join(conn.iterdump())


def _quoted_value_count(dump: str, value: str) -> int:
    return dump.count(f"'{value}'")


def test_targets_include_accounting_seed_urls() -> None:
    assert len(TARGETS) == 17
    assert TARGETS[0].agency == "한국회계기준원(KASB)"
    assert TARGETS[0].source_subtype == "K_IFRS_QNA|GAAP_QNA|IFRS_IC_KR|FAST_QNA|TF_SUPPORT"


def test_extract_links_when_fsc_list_has_detail_and_download() -> None:
    html = (
        '<a href="/no010101/87017?curPage=1">회계법인 품질관리 감리 결과</a>'
        '<a href="/comm/getFile?srvcId=BBSTY1&upperNo=87017&fileTy=ATTACH&fileNo=1">'
        "260601 보도자료.hwp"
        "</a>"
    )
    soup = BeautifulSoup(html, "html.parser")

    links = extract_links("https://www.fsc.go.kr/no010101", soup)

    assert links.details == ("https://www.fsc.go.kr/no010101/87017?curPage=1",)
    assert links.attachments[0].url == (
        "https://www.fsc.go.kr/comm/getFile?srvcId=BBSTY1&upperNo=87017&fileTy=ATTACH&fileNo=1"
    )
    assert links.attachments[0].filename == "260601 보도자료.hwp"


def test_upsert_document_when_duplicate_external_id_updates_title(tmp_path: Path) -> None:
    db_path = tmp_path / "acct.db"
    with connect_db(db_path) as conn:
        init_schema(conn)
        doc_id = upsert_document(
            conn,
            source_priority=1,
            agency="기관",
            target_name="대상",
            source_url="https://example.test/list",
            source_type="qna",
            source_subtype="K_IFRS_QNA",
            index_name="idx",
            external_id="doc-1",
            title="first",
            detail_url="https://example.test/doc-1",
            published_date="2026-01-01",
            body_text="body",
        )
        same_doc_id = upsert_document(
            conn,
            source_priority=1,
            agency="기관",
            target_name="대상",
            source_url="https://example.test/list",
            source_type="qna",
            source_subtype="K_IFRS_QNA",
            index_name="idx",
            external_id="doc-1",
            title="second",
            detail_url="https://example.test/doc-1",
            published_date="2026-01-01",
            body_text="body",
        )
        upsert_attachment(
            conn,
            document_id=doc_id,
            url="https://example.test/a.pdf",
            filename="a.pdf",
            local_path="docs/a.pdf",
            content_type="application/pdf",
            size_bytes=12,
        )

        dump = _dump_database(conn)

    assert same_doc_id == doc_id
    assert _quoted_value_count(dump, "second") == 1
    assert _quoted_value_count(dump, "a.pdf") == 1


def test_cli_help_when_invoked_as_module() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "crawler.fino_acct.collect", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "--db-path" in result.stdout
    assert "--download-dir" in result.stdout


def test_safe_filename_when_korean_title_is_long_limits_utf8_bytes() -> None:
    long_name = "회계법인 품질관리 감리 결과 개선권고사항을 공개합니다" * 12 + ".pdf"

    filename = safe_filename(long_name)

    assert len(filename.encode("utf-8")) <= 240
    assert filename.endswith(".pdf")


def test_paged_url_when_known_sites_use_site_specific_page_parameters() -> None:
    assert paged_url("https://www.fss.or.kr/fss/bbs/B0000132/list.do?menuNo=200442", 3).endswith(
        "menuNo=200442&pageIndex=3"
    )
    assert paged_url("https://www.fsc.go.kr/no010101", 3).endswith("curPage=3")
    assert paged_url("https://www.fsc.go.kr/no010101?curPage=&srchKey=dept", 3).endswith(
        "curPage=3&srchKey=dept"
    )


def test_extract_links_for_target_when_fsc_press_list_filters_accounting_rows() -> None:
    html = (
        '<tr><td><a href="/no010101/1">보안위협 대응</a></td>'
        '<td><a href="/comm/getFile?upperNo=1&fileNo=1">security.pdf</a></td></tr>'
        '<tr><td><a href="/no010101/2">회계법인 품질관리 감리 결과</a></td>'
        '<td><a href="/comm/getFile?upperNo=2&fileNo=1">audit.pdf</a></td></tr>'
    )
    soup = BeautifulSoup(html, "html.parser")

    links = extract_links_for_target(TARGETS[7], "https://www.fsc.go.kr/no010101", soup)

    assert links.details == ("https://www.fsc.go.kr/no010101/2",)
    assert links.attachments[0].filename == "audit.pdf"


def test_page_request_for_target_when_kasb_uses_post_page_field() -> None:
    request = page_request_for_target(TARGETS[0], 3)

    assert request.url == "https://www.kasb.or.kr/front/board/allReplySummaryList.do"
    assert request.method == "POST"
    assert request.data is not None
    assert request.data["page"] == "3"
    assert request.external_id == "https://www.kasb.or.kr/front/board/allReplySummaryList.do#page=3"


def test_store_attachments_when_kasb_post_links_share_endpoint_keeps_all(tmp_path: Path) -> None:
    db_path = tmp_path / "acct.db"
    html = (
        """<a href="javascript:void(0);" onclick="javascript:fileDownload('-1','1'); return false;">a.pdf</a>"""
        """<a href="javascript:void(0);" onclick="javascript:fileDownload('-2','1'); return false;">b.pdf</a>"""
    )
    links = extract_links("https://www.kasb.or.kr/front/board/allReplySummaryList.do", BeautifulSoup(html, "html.parser"))

    with connect_db(db_path) as conn:
        init_schema(conn)
        document_id = upsert_document(
            conn,
            source_priority=1,
            agency="한국회계기준원(KASB)",
            target_name="질의회신 요약 전체",
            source_url="https://www.kasb.or.kr/front/board/allReplySummaryList.do",
            source_type="qna",
            source_subtype="K_IFRS_QNA|GAAP_QNA|IFRS_IC_KR|FAST_QNA|TF_SUPPORT",
            index_name="idx",
            external_id="kasb-page-1",
            title="질의회신 요약 전체",
            detail_url="https://www.kasb.or.kr/front/board/allReplySummaryList.do",
            published_date="",
            body_text="body",
        )
        for link in links.attachments:
            upsert_attachment(
                conn,
                document_id=document_id,
                url=link.url,
                filename=link.filename,
                local_path="",
                content_type="",
                size_bytes=0,
                status="pending",
            )
        dump = _dump_database(conn)

    assert _quoted_value_count(dump, "a.pdf") == 1
    assert _quoted_value_count(dump, "b.pdf") == 1
    assert _quoted_value_count(dump, "https://www.kasb.or.kr/commonFile/fileDownload.do?fileNo=-1&fileSeq=1") == 1
    assert _quoted_value_count(dump, "https://www.kasb.or.kr/commonFile/fileDownload.do?fileNo=-2&fileSeq=1") == 1


def test_extract_links_for_target_when_kasb_list_ignores_non_row_downloads() -> None:
    html = (
        """<a href="javascript:void(0);" onclick="javascript:fileDownload('-10','1'); return false;">정비목록.hwp</a>"""
        """<table><tbody><tr><td class="left">"""
        """<a href="javascript:void(0);" onclick="javascript:fn_Detail('40637','016003');">질의 제목</a>"""
        """</td><td><a href="javascript:void(0);" """
        """onclick="javascript:fileDownload('-20','1'); return false;">질의.pdf</a></td></tr></tbody></table>"""
    )

    links = extract_links_for_target(
        TARGETS[0],
        "https://www.kasb.or.kr/front/board/allReplySummaryList.do",
        BeautifulSoup(html, "html.parser"),
    )

    assert [link.filename for link in links.attachments] == ["질의.pdf"]


def test_extract_fss_details_picks_view_do_nttid_links() -> None:
    html = Path("tests/fixtures/fino_acct/fss_list.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    details = extract_fss_details("https://www.fss.or.kr/fss/bbs/B0000132/list.do?menuNo=200442", soup)
    # 절대 URL, nttId 포함, 중복 제거
    assert details
    assert all("view.do?nttId=" in u for u in details)
    assert all(u.startswith("https://www.fss.or.kr") for u in details)
    assert len(details) == len(set(details))


def test_extract_kasb_list_returns_seq_ctgcd_items() -> None:
    html = Path("tests/fixtures/fino_acct/kasb_list.html").read_text(encoding="utf-8")
    links = extract_links_for_target(
        TARGETS[0], "https://www.kasb.or.kr/front/board/allReplySummaryList.do",
        BeautifulSoup(html, "html.parser"),
    )
    assert ("40533", "016009") in links.kasb_items
    assert len(links.kasb_items) == len(set(links.kasb_items))


def test_kasb_detail_request_builds_post_view_url() -> None:
    req = kasb_detail_request("40533", "016009")
    assert req.url == "https://www.kasb.or.kr/front/board/View016009.do"
    assert req.method == "POST"
    assert req.data == {"seq": "40533", "ctgCd": "016009", "siteCd": "002000000000000"}
    assert req.external_id == "016009-40533"


def test_extract_links_for_target_routes_fss_board_to_view_details() -> None:
    html = Path("tests/fixtures/fino_acct/fss_list.html").read_text(encoding="utf-8")
    links = extract_links_for_target(
        TARGETS[1], "https://www.fss.or.kr/fss/bbs/B0000132/list.do?menuNo=200442",
        BeautifulSoup(html, "html.parser"),
    )
    assert links.details
    assert all("view.do?nttId=" in u for u in links.details)


def test_collect_list_stores_details_not_list_page(tmp_path, monkeypatch) -> None:
    list_html = (
        '<a href="/fss/bbs/B0000132/view.do?nttId=111&menuNo=200442">질의응답 A</a>'
        '<a href="/fss/bbs/B0000132/view.do?nttId=222&menuNo=200442">질의응답 B</a>'
    )
    detail_html = "<h2>제목</h2><div>질의: ... 회신: 내용</div>"

    def fake_fetch(session, request: PageRequest, delay):
        body = list_html if "list.do" in request.url else detail_html
        return FetchResult(url=request.url, status_code=200, content_type="text/html", content=body.encode())

    monkeypatch.setattr(collect_mod, "fetch_page_request", fake_fetch)

    db_path = tmp_path / "acct.db"
    with connect_db(db_path) as conn:
        init_schema(conn)
        docs, _ = collect_mod.collect_target(
            conn=conn, session=None, target=TARGETS[1],  # FSS B0000132
            download_dir=tmp_path / "dl", max_pages=1, delay_seconds=0, download=False,
        )
        rows = conn.execute("SELECT external_id, detail_url FROM acct_documents ORDER BY external_id").fetchall()

    eids = [r["external_id"] for r in rows]
    assert "111" in eids and "222" in eids           # 상세가 문서로 저장됨
    assert all("list.do" not in r["detail_url"] for r in rows)  # 목록 페이지는 저장 안 됨
    assert all("view.do?nttId=" in r["detail_url"] for r in rows)

"""공표 게시판 목록 파서 — 실제 페이지 대신 행 마크업으로 검증한다.

게시판 HTML 전체(50KB+)를 저장소에 넣지 않는다. 파서가 다루는 어려움은 행
구조뿐이라 최소 마크업으로 재현할 수 있다.
"""

from crawler.fino_std.board import BOARD_SOURCES, parse_board


def _row(title_cell: str, attachments: str) -> str:
    return f'<tr><td class="left">{title_cell}</td><td class="">{attachments}</td></tr>'


def _attachment(file_no: str, file_seq: str, name: str) -> str:
    return (
        f'<li class="down_pdf"><a href="javascript:void(0)" title="다운로드 하려면 클릭하세요  {name}"'
        f" onclick=\"javascript:fileDownload('{file_no}','{file_seq}'); return false;\">"
        f"<span>{name}</span></a></li>"
    )


def test_row_with_link_title_and_two_attachments() -> None:
    # Given: 한 행에 hwp·pdf 가 함께 달린 보통의 게시판 행.
    html = _row(
        "<a href=\"javascript:void(0);\" onclick=\"javascript:fn_Detail('3004','368');\">"
        "<span>제5001호 결합재무제표</span></a>",
        _attachment("4240", "1", "결합재무제표(수정목록_23-1_반영).hwp")
        + _attachment("4240", "2", "결합재무제표(수정목록_23-1_반영).pdf"),
    )

    # When: 파싱하면
    items = parse_board(html, "special")

    # Then: 파일마다 한 건씩, 행 제목을 공유한다.
    assert [i.file_name for i in items] == [
        "결합재무제표(수정목록_23-1_반영).hwp",
        "결합재무제표(수정목록_23-1_반영).pdf",
    ]
    assert {i.entry_title for i in items} == {"제5001호 결합재무제표"}
    assert [i.ext for i in items] == ["hwp", "pdf"]
    assert items[1].file_no == "4240" and items[1].file_seq == "2"
    assert {i.board for i in items} == {"special"}


def test_row_with_plain_text_title() -> None:
    # Given: 중소기업회계기준 게시판은 제목 칸에 링크 없이 글자만 있다.
    html = _row(
        "중소기업회계기준 해설 - 2013.3.29. 발표",
        _attachment("301", "1", "중소기업회계기준_해설.hwp"),
    )

    # When: 파싱하면
    items = parse_board(html, "sme")

    # Then: 링크가 없어도 제목을 읽는다.
    assert len(items) == 1
    assert items[0].entry_title == "중소기업회계기준 해설 - 2013.3.29. 발표"


def test_row_without_attachment_is_skipped() -> None:
    # Given: 첨부가 없는 행.
    html = _row("<span>공지사항</span>", "")

    # Then: 판본 대장에 남길 것이 없다.
    assert parse_board(html, "gaap") == []


def test_version_string_survives_in_file_name() -> None:
    """판본 비교가 파일명에 걸리므로 괄호 안 문자열이 온전해야 한다."""
    name = "시행중_K-IFRS_제1101호_한국채택국제회계기준의_최초채택(2024_개정_2023_타기준서_개정_수정목록_26-1_2020_구성양식_변경_반영).pdf"
    items = parse_board(_row("<span>제1101호</span>", _attachment("1", "2", name)), "kifrs")

    assert items[0].file_name == name
    assert "수정목록_26-1" in items[0].file_name


def test_board_sources_are_unique() -> None:
    codes = [s.code for s in BOARD_SOURCES]
    paths = [s.path for s in BOARD_SOURCES]
    assert len(set(codes)) == len(codes)
    assert len(set(paths)) == len(paths)


def test_negative_file_number_is_accepted() -> None:
    """K-IFRS 목록은 파일번호가 음수다. 부호를 빠뜨리면 그 게시판이 통째로 비어버린다."""
    name = "시행중_K-IFRS_재무보고를_위한_개념체계(수정목록_24-1).pdf"
    html = _row("<span>개념체계</span>", _attachment("-49990963", "1", name))

    items = parse_board(html, "kifrs")

    assert len(items) == 1
    assert items[0].file_no == "-49990963"
    assert items[0].file_name == name


def test_attachments_do_not_borrow_neighbour_file_names() -> None:
    """li 를 넘어 짝을 지으면 앞 항목이 뒤 항목의 파일명을 가져간다."""
    html = _row(
        "<span>제1101호</span>",
        _attachment("-1", "2", "가.hwp") + _attachment("-2", "1", "나.pdf"),
    )

    items = parse_board(html, "kifrs")

    assert [(i.file_no, i.file_name) for i in items] == [("-1", "가.hwp"), ("-2", "나.pdf")]

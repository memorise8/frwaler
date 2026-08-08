"""공표 PDF 파서 — pdftotext -layout 출력(줄 목록) 단위로 검증한다.

실제 PDF는 저장소에 넣지 않는다. 파서가 다루는 어려움은 전부 텍스트 레이아웃
문제이므로 줄 목록으로 재현할 수 있다.
"""

from crawler.fino_std.pdf_parse import PARA_NUM, parse_glossary, parse_lines


def _nums(records) -> list[str]:
    return [r.para_num for r in records if r.para_num]


def test_para_num_covers_every_namespace() -> None:
    for raw, expected in [
        ("1   목적을 정한다.", "1"),
        ("28A  개정 문단이다.", "28A"),
        ("한3.1  외부감사법에서 정하는 회사에 적용한다.", "한3.1"),
        ("B65   적용지침이다.", "B65"),
        ("C1    경과규정이다.", "C1"),
        ("BC407  결론도출근거이다.", "BC407"),
        ("IE93  적용사례이다.", "IE93"),
    ]:
        m = PARA_NUM.match(raw)
        assert m is not None and m.group(1) == expected, raw


def test_paragraph_split_by_number() -> None:
    lines = [
        "1   이 기준서는 표시와 공시에 관한 요구사항을 정한다.",
        "",
        "2   기업은 재무제표를 작성한다.",
    ]
    records = parse_lines(lines)
    assert _nums(records) == ["1", "2"]
    assert records[0].body_text == "이 기준서는 표시와 공시에 관한 요구사항을 정한다."


def test_paragraph_continues_across_page_break() -> None:
    """문단이 페이지를 넘으면 페이지번호를 건너뛰고 이어붙여야 한다."""
    lines = [
        "119   문단 120에 따라 반박할 수 있지 않는 한, 공개적인 의사소통에",
        "      사용하는 수익과 비용의 중간합계",
        "",
        "                          - 178 -",
        "",
        "      는 기업 전체의 재무성과에 대한 경영진의 견해를 전달한다고 가정한다.",
    ]
    records = parse_lines(lines)
    assert _nums(records) == ["119"]
    assert records[0].body_text.endswith("가정한다.")
    assert "- 178 -" not in records[0].body_text


def test_heading_is_not_absorbed_into_previous_paragraph() -> None:
    """종결된 문단 뒤의 짧은 제목 줄은 본문에 흡수되지 않고 절 제목이 된다."""
    lines = [
        "10   기업은 재무제표를 표시한다.",
        "",
        "경영진이 정의한 성과측정치",
        "",
        "11   중간합계를 공시한다.",
    ]
    records = parse_lines(lines)
    assert _nums(records) == ["10", "11"]
    assert "성과측정치" not in records[0].body_text
    assert records[1].section_path == "경영진이 정의한 성과측정치"


def test_table_and_footnote_are_not_absorbed() -> None:
    lines = [
        "17   다음과 같은 형식을 사용할 수 있다.",
        "",
        "                                           (단위: 원)",
        "             미래현금흐름의      위험조정      보험계약마진",
        " 기초잔액               –             –             –",
        "",
        "61) 2020년 6월 IASB는 IFRS 17을 개정하였다.",
        "",
        "18   기업은 공시한다.",
    ]
    records = parse_lines(lines)
    assert _nums(records) == ["17", "18"]
    assert records[0].body_text == "다음과 같은 형식을 사용할 수 있다."
    assert "IASB" not in records[0].body_text


def test_sub_items_stay_with_their_paragraph() -> None:
    lines = [
        "118  중간합계는 다음과 같다.",
        "",
        "     ⑴ 매출총손익",
        "     ⑵ 영업손익",
    ]
    records = parse_lines(lines)
    assert _nums(records) == ["118"]
    assert "매출총손익" in records[0].body_text and "영업손익" in records[0].body_text


def test_glossary_two_column_layout() -> None:
    """부록 A는 문단번호가 없는 2단 표라 컬럼 위치로 항목을 가른다."""
    lines = [
        "보장기간          보험계약서비스가 제공되는 기간. 이 기간은 보",
        "              험계약의 경계 내에 있는 모든 보험료를 포함함",
        "금융위험          하나 이상의 특정변수의 미래 변동으로 인한 위험",
    ]
    entries = parse_glossary(lines, "부록 A 용어의 정의")
    assert [t for t, _ in entries] == ["보장기간", "금융위험"]
    assert entries[0][1].endswith("포함함")


def test_glossary_records_carry_term_in_section_path() -> None:
    lines = [
        "부록 A 용어의 정의",
        "",
        "이 부록은 이 기준서의 일부를 구성한다.",
        "",
        "보장기간          보험계약서비스가 제공되는 기간",
        "",
        "부록 B 적용지침",
        "",
        "B1   이 부록은 적용지침이다.",
    ]
    records = parse_lines(lines)
    glossary = [r for r in records if not r.para_num]
    assert len(glossary) == 1
    assert glossary[0].section_path == "부록 A 용어의 정의 > 보장기간"
    assert glossary[0].body_text == "보험계약서비스가 제공되는 기간"
    assert _nums(records) == ["B1"]


def test_appendix_intro_line_is_not_a_glossary_entry() -> None:
    lines = [
        "부록 A 용어의 정의",
        "",
        "이 부록은 이 기준서의 일부를 구성한다.",
    ]
    assert parse_lines(lines) == []


def test_seq_is_sequential_and_source_url_propagates() -> None:
    lines = ["1   목적이다.", "", "2   범위이다."]
    records = parse_lines(lines, source_url="https://www.kasb.or.kr/x.pdf")
    assert [r.seq for r in records] == [0, 1]
    assert all(r.source_url == "https://www.kasb.or.kr/x.pdf" for r in records)
    assert all(r.body_html == "" for r in records)


def test_glossary_blank_separated_layout_keeps_short_terms() -> None:
    """제1118호 판은 항목이 빈 줄로 갈리고 용어가 두 줄로 넘어간다.

    좌측 컬럼에 글자가 있다고 무조건 끊으면 용어가 잘리고, 반대로 짧은 용어를
    '이어지는 정의'로 보면 `분류`·`주석` 같은 두 글자 용어가 앞 항목에 먹힌다.
    """
    lines = [
        "경영진이 정의한 성과측   다음 요건을 모두 충족하는 수익과 비용의 중간",
        "정치             합계",
        "",
        "",
        "당기순손익          손익계산서에 포함되어 수익에서 비용을 차감한",
        "               합계",
        "",
        "",
        "분류             공유되는 특성에 따라 자산, 부채, 자본을 구분하는 것",
    ]
    entries = parse_glossary(lines, "부록 A. 용어의 정의")

    assert [t for t, _ in entries] == [
        "경영진이 정의한 성과측 정치",
        "당기순손익",
        "분류",
    ]
    assert entries[1][1] == "손익계산서에 포함되어 수익에서 비용을 차감한 합계"


def test_glossary_drops_page_numbers() -> None:
    # Given: 정의 도중에 쪽번호가 끼어든 지면.
    lines = [
        "소유주            자본으로 분류되는 청구권의 보유자",
        "",
        "                    - 186 -",
        "",
        "영업손익          영업 범주로 분류되는 모든 수익과 비용의 합",
        "              계",
    ]

    # When: 파싱하면
    entries = parse_glossary(lines, "부록 A. 용어의 정의")

    # Then: 쪽번호가 정의 본문에 섞이지 않는다.
    assert entries[0] == ("소유주", "자본으로 분류되는 청구권의 보유자")
    assert "186" not in entries[0][1]
    assert [t for t, _ in entries] == ["소유주", "영업손익"]

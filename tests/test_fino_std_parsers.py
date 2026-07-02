import json
from pathlib import Path

from crawler.fino_std.parsers import html_to_text, parse_content, pick_big_sections

_FX = Path("tests/fixtures/fino_std")


def test_html_to_text_strips_tags_and_entities() -> None:
    raw = '<div class="para-inner-para">이 기준서는 &#039;일반목적&#039;   재무제표<br/>를 다룬다.</div>'
    assert html_to_text(raw) == "이 기준서는 '일반목적' 재무제표 를 다룬다."


def test_pick_big_sections_from_fixture() -> None:
    titles = json.load(open(_FX / "title_1001.json"))
    bigs = pick_big_sections(titles)
    assert len(bigs) == 10                      # 본문 8 + 적용사례 + 결론도출근거
    assert bigs[0].title == "목적" and bigs[0].document_id == "c214c7"
    assert any(b.title.startswith("결론도출근거") for b in bigs)


def test_parse_content_extracts_paragraphs_with_numbers() -> None:
    content = json.load(open(_FX / "content_1001_1f0730.json"))
    paras = parse_content(content, std_num=1001, start_seq=0)
    nums = [p.para_num for p in paras]
    assert nums == ["10", "한10.1", "10A", "11", "12", "13", "14"]
    assert all(p.body_text and "<" not in p.body_text for p in paras)
    assert paras[0].source_url == "https://db.kasb.or.kr/s/1001/10"
    assert [p.seq for p in paras] == list(range(7))


def test_parse_content_builds_section_path() -> None:
    content = json.load(open(_FX / "content_1001_1f0730.json"))
    paras = parse_content(content, std_num=1001, start_seq=0)
    # title clause 스택으로 경로 구성 — '전체 재무제표' 섹션이 경로에 있어야 한다
    assert "전체 재무제표" in paras[0].section_path

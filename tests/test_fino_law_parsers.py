import json
from pathlib import Path

from crawler.fino_law.parsers_law import parse_search_for_current, parse_law_service


FIX = Path("tests/fixtures")


def test_parse_search_picks_current_exact_match() -> None:
    data = json.load(open(FIX / "law_search_법인세법.json"))
    hit = parse_search_for_current(data, "법인세법")
    assert hit is not None
    assert hit["법령명한글"] == "법인세법"
    assert hit["현행연혁코드"] == "현행"
    assert hit["법령일련번호"]  # MST 존재


def test_parse_law_service_extracts_articles_with_citation_url() -> None:
    data = json.load(open(FIX / "law_service_법인세법.json"))
    doc = parse_law_service(data, name="법인세법", category="법률", external_id="001563")
    assert doc.source_kind == "law"
    assert doc.source_url == "https://www.law.go.kr/법령/법인세법"
    assert len(doc.articles) > 100
    first = doc.articles[0]
    assert first.article_no.startswith("제")
    assert first.source_url.startswith("https://www.law.go.kr/법령/법인세법")
    assert first.body_text  # 본문 비어있지 않음

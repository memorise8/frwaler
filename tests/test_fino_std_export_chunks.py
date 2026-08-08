"""크롤러 DB → 청크 export.

실물 DB 대신 메모리 DB 에 최소 문서를 세워 검증한다. 파서가 다루는 어려움은
번호 판정과 파일명 충돌 둘뿐이라 몇 행이면 재현된다.
"""

from pathlib import Path



from crawler.fino_std.db import connect_db, init_schema, replace_paragraphs, upsert_document
from crawler.fino_std.export_chunks import (
    document_dir_name,
    export_document,
    existing_dir_names,
    fill_from_corpus,
    is_subheading,
    is_unnumbered,
)
from crawler.fino_std.models import ParagraphRecord


def _para(seq: int, num: str, section: str, text: str) -> ParagraphRecord:
    return ParagraphRecord(
        para_num=num, section_path=section, body_html=f"<p>{text}</p>",
        body_text=text, seq=seq, source_url="",
    )


def _db(tmp_path: Path, *, std_type: str, std_num: int, title: str,
        paras: list[ParagraphRecord]):
    conn = connect_db(tmp_path / "t.db")
    init_schema(conn)
    doc_id = upsert_document(
        conn, std_num=std_num, std_type=std_type, title=title, source_url="http://x"
    )
    _ = replace_paragraphs(conn, doc_id, paras)
    return conn


def test_api_sentinel_numbers_are_not_paragraph_numbers() -> None:
    """`웩1`·`왝0` 은 KASB API 가 번호 없는 조각에 붙이는 표시다."""
    assert is_unnumbered("웩1") and is_unnumbered("왝0") and is_unnumbered("")
    # 뜻이 있는 한글 접두는 진짜 번호다.
    for real in ("한2.1", "실10", "결1", "소3", "사515.1"):
        assert not is_unnumbered(real), real
    for real in ("1", "23A", "BC4", "IE12", "AG99BA"):
        assert not is_unnumbered(real), real


def test_subheading_split_uses_length_and_sentence_end() -> None:
    assert is_subheading("유효이자율법")
    assert is_subheading("부여한 지분상품의 공정가치 결정")
    # 문장으로 끝나면 본문이다 — 길이가 짧아도.
    assert not is_subheading("이 부록은 이 기준서의 일부를 구성한다.")
    assert not is_subheading("가" * 200)


def test_subheading_becomes_heading_of_following_unnumbered_body(tmp_path: Path) -> None:
    # Given: 소제목 뒤에 번호 없는 본문이 온다.
    conn = _db(tmp_path, std_type="kifrs", std_num=1012, title="법인세", paras=[
        _para(0, "웩5", "적용사례", "계산 및 표시의 예"),
        _para(1, "웩6", "적용사례", "원가 10,000원인 비품을 정액법으로 감가상각한다."),
    ])
    out = tmp_path / "out"

    # When: 내보내면
    stats = export_document(conn, std_type="kifrs", std_num=1012, out_root=out)

    # Then: 소제목은 파일이 되지 않고, 본문의 헤딩으로 붙는다.
    assert (stats[0].subheadings, stats[0].unnumbered_body, stats[0].paragraphs) == (1, 1, 0)
    body = (out / "KIFRS_chunks/제1012호_법인세/무번호_00001.md").read_text(encoding="utf-8")
    assert "## 계산 및 표시의 예" in body
    assert "웩" not in body


def test_section_change_drops_a_dangling_subheading(tmp_path: Path) -> None:
    """소제목은 자기 절 안에서만 유효하다. 절이 바뀌면 남의 헤딩이 되면 안 된다."""
    conn = _db(tmp_path, std_type="kifrs", std_num=1109, title="금융상품", paras=[
        _para(0, "웩2", "제5.4절 상각후원가 > 금융자산", "유효이자율법"),
        _para(1, "웩3", "부록 A. 용어의 정의", "이 부록은 이 기준서의 일부를 구성한다."),
    ])
    out = tmp_path / "out"

    _ = export_document(conn, std_type="kifrs", std_num=1109, out_root=out)

    body = (out / "KIFRS_chunks/제1109호_금융상품/무번호_00001.md").read_text(encoding="utf-8")
    assert "유효이자율법" not in body
    assert "## 부록 A. 용어의 정의" in body


def test_numbered_paragraph_keeps_citation_shape(tmp_path: Path) -> None:
    conn = _db(tmp_path, std_type="gaap", std_num=19, title="주식기준보상", paras=[
        _para(0, "19.1", "적용범위", "이 장은 주식기준보상거래에 적용한다."),
    ])
    out = tmp_path / "out"

    _ = export_document(conn, std_type="gaap", std_num=19, out_root=out)

    text = (out / "GAAP_chunks/제19장_주식기준보상/문단_19.1.md").read_text(encoding="utf-8")
    assert text.count("## 문단 19.1") == 1
    assert "<!-- chunk_type: standard -->" in text


def test_repeated_paragraph_number_never_overwrites(tmp_path: Path) -> None:
    """제60장은 개정 회차마다 문단 1로 되돌아간다. 덮어쓰면 조용히 사라진다."""
    conn = _db(tmp_path, std_type="gaap", std_num=60, title="시행일 및 경과규정", paras=[
        _para(0, "1", "시행일 및 경과규정(2009.12.30.) > 시행일", "2011년부터 시행한다."),
        _para(1, "1", "시행일 및 경과규정(2011.10.5.) > 시행일", "2011년 개정분이다."),
    ])
    out = tmp_path / "out"

    _ = export_document(conn, std_type="gaap", std_num=60, out_root=out)

    bodies = [p.read_text(encoding="utf-8") for p in out.rglob("문단_1.md")]
    assert len(bodies) == 2
    assert {"2011년부터 시행한다." in b for b in bodies} == {True, False}


def test_dissent_blocks_split_by_the_subheading_that_names_the_member(tmp_path: Path) -> None:
    """소수의견은 위원마다 DO1로 되돌아가고, 가르는 것은 위원 이름 소제목뿐이다."""
    conn = _db(tmp_path, std_type="kifrs", std_num=1109, title="금융상품", paras=[
        _para(0, "DO1", "IFRS 9에 대한 소수의견", "Leisenring 위원은 복잡성을 지적하였다."),
        _para(1, "DO2", "IFRS 9에 대한 소수의견", "이 위원은 공정가치를 선호한다."),
        _para(2, "웩9", "IFRS 9에 대한 소수의견", "McConnell 위원의 소수의견"),
        _para(3, "DO1", "IFRS 9에 대한 소수의견", "McConnell 위원은 공정가치가 목적적합하다고 본다."),
    ])
    out = tmp_path / "out"

    _ = export_document(conn, std_type="kifrs", std_num=1109, out_root=out)

    root = out / "KIFRS_chunks"
    first = (root / "제1109호_금융상품/문단_DO1.md").read_text(encoding="utf-8")
    second = (
        root / "제1109호_금융상품_McConnell_위원의_소수의견/문단_DO1.md"
    ).read_text(encoding="utf-8")
    assert "Leisenring" in first
    assert "McConnell 위원은" in second


def test_an_open_block_keeps_taking_its_later_paragraphs(tmp_path: Path) -> None:
    """묶음이 반쯤 본문서에 흩어지면 안 된다 — 겹치지 않는 번호가 나와도 마찬가지다."""
    conn = _db(tmp_path, std_type="kifrs", std_num=1109, title="금융상품", paras=[
        _para(0, "DO1", "소수의견", "첫 위원의 의견이다."),
        _para(1, "웩9", "소수의견", "McConnell 위원의 소수의견"),
        _para(2, "DO1", "소수의견", "둘째 위원의 의견이다."),
        _para(3, "DO2", "소수의견", "둘째 위원의 이어지는 의견이다."),
    ])
    out = tmp_path / "out"

    _ = export_document(conn, std_type="kifrs", std_num=1109, out_root=out)

    block = out / "KIFRS_chunks/제1109호_금융상품_McConnell_위원의_소수의견"
    assert sorted(p.name for p in block.glob("*.md")) == ["문단_DO1.md", "문단_DO2.md"]
    assert not (out / "KIFRS_chunks/제1109호_금융상품/문단_DO2.md").exists()


def test_split_by_section_separates_amendment_rounds(tmp_path: Path) -> None:
    conn = _db(tmp_path, std_type="gaap", std_num=60, title="시행일 및 경과규정", paras=[
        _para(0, "1", "시행일 및 경과규정(2009.12.30.) > 시행일", "2011년부터 시행한다."),
        _para(1, "1", "시행일 및 경과규정(2011.10.5.) > 시행일", "2011년 개정분이다."),
        _para(2, "소1", "소수의견", "최관 위원은 반대하였다."),
    ])
    out = tmp_path / "out"

    stats = export_document(
        conn, std_type="gaap", std_num=60, out_root=out, split_by_section=True
    )

    assert len(stats) == 3
    root = out / "GAAP_chunks"
    assert (root / "제60장_시행일_및_경과규정(2009.12.30.)/문단_1.md").exists()
    assert (root / "제60장_시행일_및_경과규정(2011.10.5.)/문단_1.md").exists()
    assert (root / "제60장_시행일_및_경과규정_소수의견/문단_소1.md").exists()


def test_document_dir_name_does_not_repeat_the_title() -> None:
    assert document_dir_name(
        title="시행일 및 경과규정", std_num=60, std_type="gaap",
        section="시행일 및 경과규정(2009.12.30.)",
    ) == "제60장_시행일_및_경과규정(2009.12.30.)"
    # 절 이름이 문서 제목과 무관하면 둘 다 남긴다 — 절만 남기면 어느 기준서의
    # 소수의견인지 사라진다.
    assert document_dir_name(
        title="시행일 및 경과규정", std_num=60, std_type="gaap", section="소수의견",
    ) == "제60장_시행일_및_경과규정_소수의견"
    assert document_dir_name(
        title="금융상품", std_num=1109, std_type="kifrs",
    ) == "제1109호_금융상품"


def test_existing_corpus_name_is_reused_so_chunk_ids_stay_stable(tmp_path: Path) -> None:
    """청크 식별자가 `{디렉토리}::{파일}` 이라 이름이 바뀌면 골든셋 라벨이 어긋난다."""
    conn = _db(tmp_path, std_type="kifrs", std_num=1109, title="금융상품", paras=[
        _para(0, "5.1.1", "인식", "최초 인식 시점에 측정한다."),
    ])
    out = tmp_path / "out"

    _ = export_document(
        conn, std_type="kifrs", std_num=1109, out_root=out,
        existing_name="9.시행중_K-IFRS_제1109호_금융상품",
    )

    assert (out / "KIFRS_chunks/9.시행중_K-IFRS_제1109호_금융상품/문단_5.1.1.md").exists()


def test_existing_dir_names_prefers_the_directory_that_has_content(tmp_path: Path) -> None:
    """코퍼스에는 `oversize` 만 든 빈 껍데기가 섞여 있다."""
    root = tmp_path / "corpus" / "GAAP_chunks"
    (root / "2.제2장_재무제표의_작성과_표기1").mkdir(parents=True)
    _ = (root / "2.제2장_재무제표의_작성과_표기1" / "문단_2.1.md").write_text("x", encoding="utf-8")
    (root / "2.제2장_재무제표의_작성과_표시1" / "oversize").mkdir(parents=True)

    names = existing_dir_names(tmp_path / "corpus")

    assert names[("GAAP", 2)] == "2.제2장_재무제표의_작성과_표기1"


def test_block_label_is_capped(tmp_path: Path) -> None:
    """소수의견 절 제목은 위원 이름을 다 늘어놓아 200자가 넘기도 한다."""
    long_title = "2013년 11월에 발표된 IFRS 9 '금융상품'에 대한 여러 위원의 소수의견 " * 4
    conn = _db(tmp_path, std_type="kifrs", std_num=1109, title="금융상품", paras=[
        _para(0, "DO1", "소수의견", "첫 위원의 의견이다."),
        _para(1, "웩9", "소수의견", long_title.strip()[:110]),
        _para(2, "DO1", "소수의견", "둘째 위원의 의견이다."),
    ])
    out = tmp_path / "out"

    stats = export_document(conn, std_type="kifrs", std_num=1109, out_root=out)

    block = next(s.doc_id for s in stats if s.doc_id != "제1109호_금융상품")
    assert len(block) <= len("제1109호_금융상품_") + 40


def _corpus(root: Path, doc: str, files: dict[str, str]) -> Path:
    d = root / "KIFRS_chunks" / doc
    d.mkdir(parents=True)
    for name, text in files.items():
        _ = (d / name).write_text(text, encoding="utf-8")
    return d


def test_fill_carries_a_paragraph_the_api_never_had(tmp_path: Path) -> None:
    """조문 API 는 2023 개정을 담고 있지 않다. 빠뜨리면 시행 중인 요구사항이 사라진다."""
    conn = _db(tmp_path, std_type="kifrs", std_num=1116, title="리스", paras=[
        _para(0, "102", "판매후리스", "판매자-리스이용자는 사용권자산을 측정한다."),
    ])
    corpus = _corpus(tmp_path / "corpus", "16.시행중_K-IFRS_제1116호_리스", {
        "문단_102A.md": "<!-- source: 제1116호(2023_개정).md / 문단 102A -->\n"
                        "\n## 문단 102A\n리스개시일 후에 판매자-리스이용자는 문단 36~46을 적용한다.\n",
    })
    target = tmp_path / "out"

    added = fill_from_corpus(
        conn, std_type="kifrs", std_num=1116, corpus_dir=corpus, target_dir=target
    )

    assert added == ["102A"]
    text = (target / "문단_102A.md").read_text(encoding="utf-8")
    assert "## 문단 102A" in text
    assert "제1116호(2023_개정).md" in text          # 출처가 크롤러가 아님을 남긴다
    assert "fill_reason" in text


def test_fill_skips_a_fragment_whose_text_the_crawler_already_has(tmp_path: Path) -> None:
    """PDF 추출이 한 문단을 쪼개면 뒤 조각이 없는 번호를 달고 나타난다(`IE12A`)."""
    conn = _db(tmp_path, std_type="kifrs", std_num=1036, title="자산손상", paras=[
        _para(0, "IE12", "사례", "A는 독립된 현금창출단위일 가능성이 높다. A의 제품의 활성시장이 있기 때문이다."),
    ])
    corpus = _corpus(tmp_path / "corpus", "35.시행중_K-IFRS_제1036호_자산손상", {
        "문단_IE12A.md": "\n## 문단 IE12A\n는 독립된 현금창출단위일 가능성이 높다. A의 제품의 활성시장이 있기 때문이다.\n",
    })
    target = tmp_path / "out"

    added = fill_from_corpus(
        conn, std_type="kifrs", std_num=1036, corpus_dir=corpus, target_dir=target
    )

    assert added == []
    assert not (target / "문단_IE12A.md").exists()


def test_fill_never_overwrites_a_crawler_chunk(tmp_path: Path) -> None:
    conn = _db(tmp_path, std_type="kifrs", std_num=1007, title="현금흐름표", paras=[
        _para(0, "44F", "공시", "크롤러 본문이다."),
    ])
    corpus = _corpus(tmp_path / "corpus", "18.시행중_K-IFRS_제1007호_현금흐름표", {
        "문단_44F.md": "\n## 문단 44F\n코퍼스 본문이다.\n",
    })
    target = tmp_path / "out"
    target.mkdir()
    _ = (target / "문단_44F.md").write_text("\n## 문단 44F\n크롤러 본문이다.\n", encoding="utf-8")

    added = fill_from_corpus(
        conn, std_type="kifrs", std_num=1007, corpus_dir=corpus, target_dir=target
    )

    assert added == []
    assert "크롤러 본문" in (target / "문단_44F.md").read_text(encoding="utf-8")

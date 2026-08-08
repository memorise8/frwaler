"""판본 대조 — 파일명만으로 최신 여부를 가른다."""

from crawler.fino_std.board import PublishedFile
from crawler.fino_std.board_diff import compare, doc_key, version_sig


def _pub(name: str, board: str = "kifrs") -> PublishedFile:
    return PublishedFile(board=board, entry_title="", file_name=name, file_no="1", file_seq="2")


def test_doc_key_reads_standard_number() -> None:
    assert doc_key("시행중_K-IFRS_제1101호_최초채택(2024_개정).pdf") == "제1101호"
    assert doc_key("2.시행중_K-IFRS_제1101호_최초채택(2023_개정)_25.11.7.md") == "제1101호"
    assert doc_key("제29장_중간재무제표(2020_개정).pdf") == "제29장"
    assert doc_key("보험업회계처리준칙(수정목록_19-1_반영).pdf") == "보험업회계처리준칙"


def test_version_sig_ignores_prefix_and_housekeeping_suffix() -> None:
    """보유분에는 MD_SET 접두번호와 정리 날짜가 붙는다. 판본은 괄호 안에만 있다."""
    published = "제4장_연결재무제표(2018년_개정_반영_수정목록_20-1_반영).pdf"
    held = "4.제4장_연결재무제표(2018년_개정_반영_수정목록_20-1_반영)_25.11.7.md"
    assert version_sig(published) == version_sig(held)


def test_version_sig_separates_real_revisions() -> None:
    a = "시행중_K-IFRS_제1101호_최초채택(2024_개정_수정목록_26-1_반영).pdf"
    b = "2.시행중_K-IFRS_제1101호_최초채택(2023_개정_수정목록_24-1_반영).md"
    assert version_sig(a) != version_sig(b)


def test_compare_classifies_three_verdicts() -> None:
    published = [
        _pub("제4장_연결재무제표(2018년_개정_반영_수정목록_20-1_반영).pdf"),
        _pub("시행중_K-IFRS_제1101호_최초채택(2024_개정_수정목록_26-1_반영).pdf"),
        _pub("보험업회계처리준칙(수정목록_19-1_반영).pdf"),
    ]
    holdings = [
        "4.제4장_연결재무제표(2018년_개정_반영_수정목록_20-1_반영)_25.11.7.md",
        "2.시행중_K-IFRS_제1101호_최초채택(2023_개정_수정목록_24-1_반영).md",
    ]

    rows = compare(published, holdings)

    assert {r.key: r.verdict for r in rows} == {
        "제4장": "같음",
        "제1101호": "낡음",
        "보험업회계처리준칙": "없음",
    }


def test_compare_skips_other_extensions() -> None:
    """게시판 한 행에 hwp·pdf 가 함께 달려 있어 확장자를 고르지 않으면 두 번 센다."""
    published = [
        _pub("제4장_연결재무제표(2018년_개정).hwp"),
        _pub("제4장_연결재무제표(2018년_개정).pdf"),
    ]
    assert len(compare(published, [], ext="pdf")) == 1

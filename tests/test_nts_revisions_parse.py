from crawler.nts_revisions.parse import case_year, parse_case_cell, row_to_revision


def test_parse_case_cell_single_and_multi() -> None:
    assert parse_case_cell("재산세과-271(2010.05.04.)") == [("재산세과-271", "2010.05.04.")]
    assert parse_case_cell("법인46012-1784(2000.08.19.)|법인46012-379(2000.02.10.)") == [
        ("법인46012-1784", "2000.08.19."), ("법인46012-379", "2000.02.10.")]


def test_parse_case_cell_edge() -> None:
    assert parse_case_cell(None) == []
    assert parse_case_cell("") == []
    # 괄호 없는 조문(법령)도 번호만 뽑고 날짜는 빈값
    assert parse_case_cell("소득세법 시행령 제155조의3") == [("소득세법 시행령 제155조의3", "")]


def test_case_year() -> None:
    assert case_year("2010.05.04.") == "2010"
    assert case_year("") == ""


def test_row_to_revision() -> None:
    row = ("996", "상증", "사망보험금을 협의분할...", "사전-2014-법령해석재산-20405(2015.07.13.)",
           "재산세과-271(2010.05.04.)", "국세법령해석심의위원회...", "2026.06.26.")
    rev = row_to_revision(row, "nts_new.xlsx")
    assert rev is not None
    assert rev.seq == 996 and rev.tax_category == "상증"
    assert rev.keep_cases == [("사전-2014-법령해석재산-20405", "2015.07.13.")]
    assert rev.delete_cases == [("재산세과-271", "2010.05.04.")]
    assert rev.registered_at == "2026.06.26." and rev.source_file == "nts_new.xlsx"


def test_row_to_revision_skips_empty() -> None:
    assert row_to_revision((None, None, None, None, None, None, None), "x") is None

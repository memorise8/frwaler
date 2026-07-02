from crawler.fino_std.sources import STD_SEEDS, para_citation_url, std_source_url


def test_seed_counts_by_type() -> None:
    by_type: dict[str, int] = {}
    for t in STD_SEEDS:
        by_type[t.std_type] = by_type.get(t.std_type, 0) + 1
    assert by_type == {"kifrs": 43, "kifrs_interp": 19, "kifrs_etc": 3, "gaap": 37}
    assert len(STD_SEEDS) == 102


def test_seed_membership_and_exclusions() -> None:
    nums = {t.std_num for t in STD_SEEDS}
    assert {1000, 1001, 1116, 1117, 1118, 2010, 2123, 99, 1, 33, 60, 91, 93} <= nums
    # 구기준(KASB판 제외)과 주석 처리된 92는 시드에 없어야 한다
    assert not ({1011, 1017, 1018, 1104, 92} & nums)


def test_seed_titles() -> None:
    m = {t.std_num: t.title for t in STD_SEEDS}
    assert m[1001] == "재무제표 표시"
    assert m[99] == "재무회계개념체계"
    assert m[13] == "리스"
    assert m[2123] == "법인세 처리의 불확실성"


def test_citation_urls() -> None:
    assert std_source_url(1001) == "https://db.kasb.or.kr/s/1001"
    assert para_citation_url(1001, "한10.1") == "https://db.kasb.or.kr/s/1001/한10.1"
    assert para_citation_url(1001, "") == "https://db.kasb.or.kr/s/1001"

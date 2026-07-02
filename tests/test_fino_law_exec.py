from crawler.fino_law.sources_exec import EXEC_TARGETS
from crawler.fino_law.pdf_exec import extract_bodies


def test_exec_targets_15_with_ids() -> None:
    assert len(EXEC_TARGETS) == 15
    법인 = next(t for t in EXEC_TARGETS if t.name == "법인세 집행기준")
    assert 법인.ntst_bsc_id == "100000000000001563"
    assert 법인.ntst_plcn_bk_id == "511100000000000003"
    assert any(t.name == "국세기본법 집행기준" for t in EXEC_TARGETS)


_SAMPLE = """법인세 집행기준
                    < 목  차 >
집행기준 2-0-1              【내국법인과 외국법인의 구분】 ··············· 1
집행기준 2-0-2              【비영리법인의 범위】 ··············· 2

- 1 -
법인세 집행기준
집행기준    2-0-1   내국법인과 외국법인의 구분

내국법인과 외국법인의 구분은 본점 또는 주사무소의 소재지를 기준으로 구분한다.
그 관리장소를 기준으로 구분한다.
- 2 -
법인세 집행기준
집행기준    2-0-2   비영리법인의 범위

① 비영리내국법인은 내국법인 중 다음에 해당하는 법인을 말한다.
국세청
"""


def test_extract_bodies_splits_and_strips_noise() -> None:
    bodies = extract_bodies(_SAMPLE, page_headers=("법인세 집행기준", "국세청"))
    assert set(bodies) == {"2-0-1", "2-0-2"}                     # 목차 아닌 본문만
    assert "본점 또는 주사무소" in bodies["2-0-1"]
    assert "법인세 집행기준" not in bodies["2-0-1"]              # 페이지 머리말 제거
    assert "- 1 -" not in bodies["2-0-1"] and "- 2 -" not in bodies["2-0-1"]
    assert bodies["2-0-2"].startswith("① 비영리내국법인")
    assert "국세청" not in bodies["2-0-2"]


import json
from pathlib import Path
from crawler.fino_law.parsers_exec import parse_exec, exec_citation_url


def test_parse_exec_matches_bodies_by_number() -> None:
    data = json.load(open(Path("tests/fixtures/exec_법인세_list.json")))
    bodies = {"2-0-1": "내국법인과 외국법인의 구분은 본점 또는...", "2-0-2": "① 비영리내국법인은..."}
    doc = parse_exec(data, name="법인세 집행기준", ntst_bsc_id="100000000000001563", bodies=bodies)
    assert doc.source_kind == "exec_standard"
    assert doc.source_url == "https://taxlaw.nts.go.kr/st/USESTE001M.do?ntstBscId=100000000000001563"
    first = next(a for a in doc.articles if a.article_no == "2-0-1")
    assert "내국법인과 외국법인의 구분" in first.article_title
    assert "본점 또는" in first.body_text
    assert first.source_url.endswith("ntstBscId=100000000000001563#2-0-1")


def test_exec_citation_url() -> None:
    assert exec_citation_url("100000000000001563") == \
        "https://taxlaw.nts.go.kr/st/USESTE001M.do?ntstBscId=100000000000001563"

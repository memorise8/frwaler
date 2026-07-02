from crawler.fino_law.sources_exec import EXEC_TARGETS


def test_exec_targets_15_with_ids() -> None:
    assert len(EXEC_TARGETS) == 15
    법인 = next(t for t in EXEC_TARGETS if t.name == "법인세 집행기준")
    assert 법인.ntst_bsc_id == "100000000000001563"
    assert 법인.ntst_plcn_bk_id == "511100000000000003"
    assert any(t.name == "국세기본법 집행기준" for t in EXEC_TARGETS)

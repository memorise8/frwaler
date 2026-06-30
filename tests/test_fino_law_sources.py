from crawler.fino_law.sources import LAW_TARGETS


def test_law_targets_cover_core_national_and_local_taxes() -> None:
    names = {t.name for t in LAW_TARGETS}
    assert "법인세법" in names
    assert "소득세법" in names
    assert "부가가치세법" in names
    assert "지방세법" in names
    # 각 법령은 법률/시행령/시행규칙 3종이 있어야 함
    categories = {(t.name, t.category) for t in LAW_TARGETS}
    assert ("법인세법", "법률") in categories
    assert ("법인세법", "시행령") in categories
    assert ("법인세법", "시행규칙") in categories


def test_law_targets_are_unique() -> None:
    keys = [(t.name, t.category) for t in LAW_TARGETS]
    assert len(keys) == len(set(keys))

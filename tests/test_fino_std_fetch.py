from crawler.fino_std.fetch import is_unavailable


def test_is_unavailable_on_error_payload() -> None:
    assert is_unavailable(None)
    assert is_unavailable({"message": "Something went wrong!"})


def test_is_unavailable_false_on_real_payloads() -> None:
    assert not is_unavailable({"titles": [{"title": "목적"}]})
    assert not is_unavailable({"clauses": [], "status": 200})
    # titles가 비어 있어도 오류 페이로드는 아니다(빈 기준서는 호출부에서 skip)
    assert not is_unavailable({"titles": [], "titlesObj": {}})

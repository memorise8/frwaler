import httpx

from crawler.fino_std.fetch import _get_json, is_unavailable


def test_is_unavailable_on_error_payload() -> None:
    assert is_unavailable(None)
    assert is_unavailable({"message": "Something went wrong!"})


def test_is_unavailable_false_on_real_payloads() -> None:
    assert not is_unavailable({"titles": [{"title": "목적"}]})
    assert not is_unavailable({"clauses": [], "status": 200})
    # titles가 비어 있어도 오류 페이로드는 아니다(빈 기준서는 호출부에서 skip)
    assert not is_unavailable({"titles": [], "titlesObj": {}})


def test_get_json_returns_none_on_malformed_json() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"<html>not json</html>")
    )
    with httpx.Client(transport=transport) as client:
        assert _get_json(client, "/api/title/1", delay=0) is None

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


def paged_url(url: str, page: int) -> str:
    parsed = urlparse(url)
    pairs = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if "fsc.go.kr" in parsed.netloc:
        pairs["curPage"] = str(page)
    elif "pageIndex" in pairs or page > 1:
        pairs["pageIndex"] = str(page)
    return urlunparse(parsed._replace(query=urlencode(pairs)))

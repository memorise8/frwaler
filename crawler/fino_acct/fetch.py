from pathlib import Path
import re
import time

import requests

from .models import AttachmentLink, DownloadResult, FetchResult
from .target_pages import PageRequest


SAFE_NAME_RE = re.compile(r"[^0-9A-Za-z가-힣._()\\[\\] -]+")


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        }
    )
    return session


def fetch_page(session: requests.Session, url: str, delay_seconds: float) -> FetchResult:
    last_error: requests.RequestException | None = None
    for attempt in range(3):
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        try:
            response = session.get(url, timeout=20)
            return FetchResult(
                url=response.url,
                status_code=response.status_code,
                content_type=response.headers.get("content-type", ""),
                content=response.content,
            )
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(1 + attempt)
    if last_error is not None:
        raise last_error
    raise requests.RequestException(f"failed to fetch {url}")


def fetch_page_request(session: requests.Session, request: PageRequest, delay_seconds: float) -> FetchResult:
    match request.method:
        case "GET":
            return fetch_page(session, request.url, delay_seconds)
        case "POST":
            return fetch_post_page(session, request, delay_seconds)


def fetch_post_page(session: requests.Session, request: PageRequest, delay_seconds: float) -> FetchResult:
    last_error: requests.RequestException | None = None
    for attempt in range(3):
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        try:
            response = session.post(request.url, data=request.data, timeout=20)
            return FetchResult(
                url=response.url,
                status_code=response.status_code,
                content_type=response.headers.get("content-type", ""),
                content=response.content,
            )
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(1 + attempt)
    if last_error is not None:
        raise last_error
    raise requests.RequestException(f"failed to fetch {request.url}")


def safe_filename(filename: str) -> str:
    cleaned = SAFE_NAME_RE.sub("_", filename).strip(" ._")
    if not cleaned:
        return "download.bin"
    stem, suffix = _split_suffix(cleaned)
    suffix_bytes = len(suffix.encode("utf-8"))
    budget = max(16, 240 - suffix_bytes)
    return f"{_truncate_utf8(stem, budget)}{suffix}"


def _split_suffix(filename: str) -> tuple[str, str]:
    path = Path(filename)
    suffix = path.suffix
    if suffix:
        return filename[: -len(suffix)], suffix
    return filename, ""


def _truncate_utf8(value: str, max_bytes: int) -> str:
    output: list[str] = []
    size = 0
    for char in value:
        char_size = len(char.encode("utf-8"))
        if size + char_size > max_bytes:
            break
        output.append(char)
        size += char_size
    truncated = "".join(output).strip(" ._")
    if truncated:
        return truncated
    return "download"


def download_attachment(
    session: requests.Session,
    link: AttachmentLink,
    download_dir: Path,
    prefix: str,
    delay_seconds: float,
) -> DownloadResult:
    download_dir.mkdir(parents=True, exist_ok=True)
    filename = safe_filename(f"{prefix}_{link.filename}")
    destination = download_dir / filename
    if delay_seconds > 0:
        time.sleep(delay_seconds)
    try:
        response = _download_response(session, link)
    except requests.RequestException:
        return DownloadResult(local_path=None, content_type="", size_bytes=0, status="failed")
    if response.status_code >= 400:
        return DownloadResult(local_path=None, content_type=response.headers.get("content-type", ""), size_bytes=0, status="failed")
    _ = destination.write_bytes(response.content)
    return DownloadResult(
        local_path=destination,
        content_type=response.headers.get("content-type", ""),
        size_bytes=len(response.content),
        status="downloaded",
    )


def _download_response(session: requests.Session, link: AttachmentLink) -> requests.Response:
    match link.method:
        case "POST":
            return session.post(
                link.url,
                data={"fileNo": link.post_file_no, "fileSeq": link.post_file_seq},
                timeout=30,
            )
        case "GET":
            return session.get(link.url, timeout=30)
        case unreachable:
            raise requests.RequestException(f"unsupported method {unreachable}")

# -*- coding: utf-8 -*-
"""CIRNAC/RCAANC reports and publications crawler.

Target: https://www.rcaanc-cirnac.gc.ca/eng/1575131323136/1575131374706

The site exposes this collection as a static WET/GCWeb HTML resource list.
There is no JSON list API behind the page; each internal /eng/<id>/<id>
link is the real detail endpoint and detail pages carry standard dcterms
metadata plus the report body.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

# Absolute import: spec_from_file_location has no package context.
_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from crawler.base_crawler import BaseCrawler  # noqa: E402


SITE_ID = "rcaanc-cirnac-gc-ca-eng"
BASE_URL = "https://www.rcaanc-cirnac.gc.ca"
START_URL = f"{BASE_URL}/eng/1575131323136/1575131374706"


def _make_soup(raw):
    """Parse HTML with html5lib -> lxml -> html.parser fallbacks."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup

            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _normalize_space(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _curl_get(url: str, *, site_id: str = SITE_ID, retries: int = 3) -> str | None:
    """Fetch with curl and exponential backoff; decode mixed encodings safely."""
    waits = [1, 3, 9]
    cmd = [
        "curl",
        "--tls-max",
        "1.3",
        "-skL",
        "--connect-timeout",
        "15",
        "--max-time",
        "45",
        "-H",
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
        "-H",
        "Accept-Language: en-CA,en;q=0.9,fr-CA;q=0.7,fr;q=0.6",
        "-H",
        f"User-Agent: {BaseCrawler.USER_AGENT}",
        url,
    ]
    last_msg = "unknown error"
    for attempt in range(retries):
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=55, check=False)
            raw = res.stdout or b""
            if res.returncode == 0 and raw.strip():
                return raw.decode("utf-8", errors="replace")
            stderr = (res.stderr or b"").decode("utf-8", errors="replace").strip()
            last_msg = stderr or f"curl exit {res.returncode}, {len(raw)} bytes"
        except Exception as exc:
            last_msg = str(exc)

        if attempt < retries - 1:
            wait = waits[attempt]
            print(f"[{site_id}] fetch attempt {attempt + 1}/{retries} failed for {url}: {last_msg}; retrying in {wait}s")
            time.sleep(wait)

    print(f"[{site_id}] fetch failed after {retries} attempts for {url}: {last_msg}")
    return None


def _meta_map(soup) -> dict:
    metas = {}
    if not soup:
        return metas
    for meta in soup.find_all("meta"):
        key = meta.get("name") or meta.get("property")
        if key and meta.get("content") is not None:
            metas[key.lower()] = _normalize_space(meta.get("content"))
    return metas


def _iso_date(value: str | None) -> str | None:
    if not value:
        return None
    text = _normalize_space(value)
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def _date_from_numeric_id(value: str | None) -> str | None:
    if not value or not re.fullmatch(r"\d{10,14}", value):
        return None
    try:
        timestamp = int(value)
        if len(value) >= 13:
            timestamp = timestamp / 1000.0
        return datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return None


def _native_ids(url: str) -> tuple[str | None, str | None]:
    match = re.search(r"/eng/(\d{10,14})(?:/(\d{10,14}))?", url)
    if match:
        return match.group(1), match.group(2)
    path = urlparse(url).path.rstrip("/")
    slug = path.split("/")[-1] if path else None
    return slug or None, None


def _filename_from_pdf_url(pdf_url: str | None) -> str | None:
    if not pdf_url:
        return None
    parsed = urlparse(pdf_url)
    query_url = parse_qs(parsed.query).get("url")
    if query_url:
        return _filename_from_pdf_url(query_url[0])
    tail = unquote(parsed.path.rstrip("/").split("/")[-1])
    if tail and "." in tail and len(tail) <= 220:
        return tail
    return None


def _keywords_from_subject(subject: str | None) -> str | None:
    if not subject:
        return None
    parts = [_normalize_space(p) for p in re.split(r"[;,]", subject) if _normalize_space(p)]
    return ", ".join(parts) if parts else None


def _publisher_from_creator(creator: str | None) -> str | None:
    if not creator:
        return None
    parts = [_normalize_space(p) for p in creator.split(";") if _normalize_space(p)]
    return "; ".join(parts) if parts else _normalize_space(creator)


def _department_from_taxonomy(taxonomy: str | None) -> str | None:
    if not taxonomy:
        return None
    parts = [_normalize_space(p) for p in taxonomy.split(">") if _normalize_space(p)]
    if len(parts) > 1:
        return " > ".join(parts[1:])
    return parts[0] if parts else None


def _extract_pdf(soup, detail_url: str) -> tuple[str | None, str | None]:
    """Return the official report PDF link when the detail page exposes one."""
    if not soup:
        return None, None
    candidates = []
    detail_host = urlparse(detail_url).netloc
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        text = _normalize_space(anchor.get_text(" ", strip=True)).lower()
        full = urljoin(detail_url, href)
        parsed = urlparse(full)
        href_l = full.lower()
        is_pdf = ".pdf" in href_l or "forcepdfdownload" in href_l
        if not is_pdf:
            continue

        same_host = parsed.netloc == detail_host
        official_asset = same_host or "rcaanc-cirnac.gc.ca" in parsed.netloc
        label_score = 0
        if "pdf version" in text or "version pdf" in text:
            label_score += 5
        if "pdf" in text:
            label_score += 2
        if "forcepdfdownload" in href_l or "/dam/" in href_l:
            label_score += 3
        if official_asset:
            label_score += 5

        # Avoid citing incidental external PDF references embedded in reports.
        if not official_asset and label_score < 7:
            continue
        candidates.append((label_score, full))

    if not candidates:
        return None, None
    candidates.sort(key=lambda item: item[0], reverse=True)
    pdf_url = candidates[0][1]
    return pdf_url, _filename_from_pdf_url(pdf_url)


def _body_abstract(soup) -> str:
    if not soup:
        return ""
    main = soup.find("main") or soup
    for selector in ("script", "style", "noscript", "nav", "form"):
        for tag in main.find_all(selector):
            try:
                tag.decompose()
            except Exception:
                pass
    for heading in main.find_all(["h2", "h3", "h4"]):
        if _normalize_space(heading.get_text(" ", strip=True)).lower() == "table of contents":
            container = heading.find_parent(["section", "div"]) or heading
            try:
                container.decompose()
            except Exception:
                pass

    chunks = []
    for tag_name in ("p", "li"):
        for tag in main.find_all(tag_name):
            text = _normalize_space(tag.get_text(" ", strip=True))
            if len(text) < 45:
                continue
            lowered = text.lower()
            if lowered.startswith("pdf version") or lowered.startswith("date modified"):
                continue
            if "skip to main content" in lowered:
                continue
            chunks.append(text)
            if len(" ".join(chunks)) >= 1400:
                break
        if len(" ".join(chunks)) >= 400:
            break
    return " ".join(chunks)[:1800].strip()


def _parse_detail(detail_url: str, html: str, list_item: dict) -> dict | None:
    soup = _make_soup(html)
    if not soup:
        return None

    metas = _meta_map(soup)
    h1 = soup.find("h1")
    title = _normalize_space(h1.get_text(" ", strip=True)) if h1 else ""
    title = title or metas.get("dcterms.title") or list_item.get("title") or ""

    meta_desc = metas.get("dcterms.description") or metas.get("description") or ""
    body_desc = _body_abstract(soup)
    if len(meta_desc) >= 120:
        abstract = meta_desc
    elif meta_desc and body_desc:
        abstract = f"{meta_desc} {body_desc}"
    else:
        abstract = body_desc or meta_desc
    abstract = _normalize_space(abstract)

    node_id, alt_id = _native_ids(detail_url)
    published_raw = metas.get("dcterms.issued")
    modified_raw = metas.get("dcterms.modified")
    published_date = _iso_date(published_raw) or _date_from_numeric_id(node_id)
    listed_date = list_item.get("listed_date") or _date_from_numeric_id(node_id) or published_date
    listed_raw = list_item.get("posted_date_raw") or node_id or listed_date
    pdf_url, original_filename = _extract_pdf(soup, detail_url)

    publisher = _publisher_from_creator(metas.get("dcterms.creator"))
    topic_taxonomy = metas.get("gcterms.topictaxonomy")
    category = list_item.get("category") or metas.get("dcterms.type") or None
    department = _department_from_taxonomy(topic_taxonomy)
    keywords = _keywords_from_subject(metas.get("dcterms.subject"))

    metadata = {
        "posted_date": listed_raw,
        "listed_date": listed_date,
        "published_date_raw": published_raw,
        "modified_date": _iso_date(modified_raw) or modified_raw,
        "originalFilename": original_filename,
        "journal_raw": None,
        "series": None,
        "volume": None,
        "issue": None,
        "node_id": node_id,
        "alternate_node_id": alt_id,
        "language": metas.get("dcterms.language"),
        "dcterms": metas,
        "topic_taxonomy": topic_taxonomy,
        "list_record": list_item,
    }

    external_id = node_id or list_item.get("external_id") or detail_url.rstrip("/").split("/")[-1]
    return {
        "id": f"{SITE_ID}:{external_id}",
        "site_id": SITE_ID,
        "external_id": external_id,
        "post_number": node_id if node_id and node_id.isdigit() else external_id,
        "title": title,
        "abstract": abstract,
        "published_date": published_date,
        "listed_date": listed_date,
        "posted_date": listed_date,
        "authors": None,
        "publisher": publisher,
        "department": department,
        "journal": None,
        "url": detail_url,
        "pdf_url": pdf_url,
        "keywords": keywords,
        "category": category,
        "doi": None,
        "original_filename": original_filename,
        "metadata": json.dumps(metadata, ensure_ascii=False),
    }


def _parse_list_page(html: str, page_url: str) -> tuple[list[dict], str | None]:
    soup = _make_soup(html)
    if not soup:
        return [], None
    main = soup.find("main") or soup
    items = []
    current_category = None

    for node in main.find_all(["h2", "h3", "h4", "li"]):
        if node.name in ("h2", "h3", "h4"):
            text = _normalize_space(node.get_text(" ", strip=True))
            if text and text.lower() not in {"on this page"}:
                current_category = text
            continue

        anchor = node.find("a", href=True)
        if not anchor:
            continue
        href = anchor.get("href", "")
        if href.startswith("#"):
            continue
        full_url = urljoin(page_url, href)
        parsed = urlparse(full_url)
        if parsed.fragment:
            continue
        if parsed.netloc != urlparse(BASE_URL).netloc:
            continue
        if not re.search(r"/eng/\d{10,14}/\d{10,14}", parsed.path):
            continue

        title = _normalize_space(anchor.get_text(" ", strip=True))
        if not title:
            continue
        node_id, alt_id = _native_ids(full_url)
        items.append(
            {
                "title": title,
                "url": full_url,
                "category": current_category,
                "external_id": node_id,
                "node_id": node_id,
                "alternate_node_id": alt_id,
                "listed_date": _date_from_numeric_id(node_id),
                "posted_date_raw": node_id,
            }
        )

    next_url = None
    next_link = main.find("a", rel=lambda value: value and "next" in value)
    if not next_link:
        for anchor in main.find_all("a", href=True):
            text = _normalize_space(anchor.get_text(" ", strip=True)).lower()
            if text in {"next", "next page", "page suivante"}:
                next_link = anchor
                break
    if next_link and next_link.get("href"):
        next_url = urljoin(page_url, next_link.get("href"))

    return items, next_url


class RcaancCirnacGcCaEngCrawler(BaseCrawler):
    site_id = "rcaanc-cirnac-gc-ca-eng"
    site_name = "Custom: rcaanc-cirnac-gc-ca-eng"
    base_url = "https://www.rcaanc-cirnac.gc.ca"

    def crawl(self, limit=None):
        saved = 0
        page = 1
        page_url = START_URL
        seen_urls = set()
        start_time = time.time()
        max_seconds = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
        stop_threshold = max_seconds - 30
        limit_or_inf = str(limit) if limit is not None else "inf"
        max_pages = 200

        if limit is not None and limit <= 0:
            return 0

        while True:
            if limit is not None and saved >= limit:
                break
            elapsed = time.time() - start_time
            if elapsed >= stop_threshold:
                print(f"[{self.site_id}] approaching 25-minute budget after {elapsed / 60:.1f} minutes; stopping cleanly")
                break
            if page > max_pages:
                print(f"[{self.site_id}] safety cap of {max_pages} pages reached; stopping")
                break
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            list_html = _curl_get(page_url, site_id=self.site_id)
            if not list_html:
                print(f"[{self.site_id}] page {page}: failed to fetch list page; stopping")
                break

            items, next_url = _parse_list_page(list_html, page_url)
            if not items:
                print(f"[{self.site_id}] page {page}: 0 records; stopping")
                break

            page_new_urls = 0
            for idx, item in enumerate(items, start=1):
                if limit is not None and saved >= limit:
                    break
                elapsed = time.time() - start_time
                if elapsed >= stop_threshold:
                    print(f"[{self.site_id}] approaching 25-minute budget after {elapsed / 60:.1f} minutes; stopping cleanly")
                    return saved

                detail_url = item.get("url")
                if not detail_url or detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                page_new_urls += 1

                try:
                    time.sleep(self._delay)
                    detail_html = _curl_get(detail_url, site_id=self.site_id)
                    if not detail_html:
                        print(f"[{self.site_id}] item {idx} failed: empty detail response")
                        continue
                    paper = _parse_detail(detail_url, detail_html, item)
                    if not paper:
                        print(f"[{self.site_id}] item {idx} failed: could not parse detail page")
                        continue
                    if not paper.get("title"):
                        print(f"[{self.site_id}] item {idx} skipped: empty title")
                        continue
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 50:
                        print(f"[{self.site_id}] item {idx} skipped: abstract too short ({len(abstract)} chars)")
                        continue
                    self._save_paper(paper)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {idx} failed: {exc}")
                    continue

            if page_new_urls == 0:
                print(f"[{self.site_id}] page {page}: no new URLs; stopping")
                break
            if not next_url:
                break
            if next_url in seen_urls:
                print(f"[{self.site_id}] page {page}: next page already seen; stopping")
                break
            page += 1
            page_url = next_url

        return saved

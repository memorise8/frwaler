# -*- coding: utf-8 -*-
"""Crawler for miteco.gob.es press releases (ultimas-noticias + historico)."""

from __future__ import annotations

import json
import re
import subprocess
import time
import unicodedata
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


_CURL_META_MARKER = "__MITECO_GOB_ES_CURL_META__:"
_BACKOFF = (1, 3, 9)
_CURL_TIMEOUT = 45
_MIN_ABSTRACT_CHARS = 100
_MIN_ABSTRACT_SKIP = 50

_MONTHS_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}

# The CDN always serves the same cached response regardless of ?offset=N params.
# Both list pages expose a fixed 10-item "latest" batch; pagination is broken
# at the CDN layer (content-page-ref header is identical for all offset values).
# The crawler processes each source once, detects duplicates on the second
# attempt and stops naturally.
_LIST_SOURCES = [
    "https://www.miteco.gob.es/es/prensa/ultimas-noticias.html",
    "https://www.miteco.gob.es/es/prensa/historico.html",
]


class MitecoGobEsEsCrawler(BaseCrawler):
    site_id = "miteco-gob-es-es"
    site_name = "Custom: miteco-gob-es-es"
    base_url = "https://www.miteco.gob.es"

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        total_pages = 0

        limit_str = str(limit) if limit is not None else "inf"

        for list_url in _LIST_SOURCES:
            if limit is not None and saved >= limit:
                break

            # Walk pages (CDN serves same content past page 0, so we stop
            # as soon as a page returns 0 new URLs — usually after 2 attempts).
            page = 0
            safety_cap = 200

            while page < safety_cap:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > 25 * 60:
                    print(f"[{self.site_id}] 25-minute budget reached, stopping cleanly")
                    return saved
                if page == safety_cap - 1:
                    print(f"[{self.site_id}] safety cap {safety_cap} pages reached on {list_url}")

                page_url = list_url if page == 0 else f"{list_url}?offset={page}"
                raw = self._curl_get(page_url, context=f"list page {page}")
                if not raw:
                    print(f"[{self.site_id}] list fetch failed at page {page}; stopping source")
                    break

                records = self._parse_list(raw, list_url)
                new_records = [r for r in records if r["url"] not in seen_urls]

                if not new_records:
                    print(f"[{self.site_id}] page {page}: no new records, stopping source")
                    break

                total_pages += 1
                if total_pages % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

                for record in new_records:
                    if limit is not None and saved >= limit:
                        break
                    seen_urls.add(record["url"])

                    try:
                        time.sleep(self._delay)
                        detail_raw = self._curl_get(
                            record["url"],
                            context=f"detail {record['url']}",
                            referer=list_url,
                        )
                        if not detail_raw:
                            raise RuntimeError("detail fetch failed after retries")

                        parsed = self._parse_detail(detail_raw, record)
                        abstract = parsed.get("abstract") or ""
                        if len(abstract) < _MIN_ABSTRACT_SKIP:
                            print(
                                f"[{self.site_id}] skip: abstract too short "
                                f"({len(abstract)} chars) for {record['url']}"
                            )
                            continue

                        self._save_paper(parsed)
                        saved += 1
                        print(
                            f"[{self.site_id}] saved {saved}/{limit_str}: "
                            f"{parsed['title'][:80]}"
                        )

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[{self.site_id}] item failed ({record.get('url','')}): {exc}")
                        continue

                page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url, context="request", referer=None, accept=None):
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(_CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-H", f"Accept: {accept or 'text/html,application/xhtml+xml,*/*;q=0.8'}",
            "-H", "Accept-Language: es-ES,es;q=0.9,en;q=0.7",
            "-w", "\n" + _CURL_META_MARKER + "%{http_code}\t%{url_effective}",
        ]
        if referer:
            cmd += ["-H", f"Referer: {referer}"]
        cmd.append(url)

        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=_CURL_TIMEOUT + 10)
                stdout = (result.stdout or b"").decode("utf-8", errors="replace")
                body, http_code, _ = _split_curl_output(stdout, url)
                if result.returncode != 0:
                    raise RuntimeError(f"curl exit {result.returncode}")
                if http_code and int(http_code) >= 400:
                    raise RuntimeError(f"HTTP {http_code}")
                if not body.strip():
                    raise RuntimeError("empty response")
                return body
            except Exception as exc:
                last_error = str(exc)
                print(f"[{self.site_id}] {context} attempt {attempt}/3 failed: {last_error}")
                if attempt < 3:
                    wait = _BACKOFF[attempt - 1]
                    print(f"[{self.site_id}] retrying in {wait}s…")
                    time.sleep(wait)

        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return None

    # ------------------------------------------------------------------
    # List page parsing
    # ------------------------------------------------------------------

    def _parse_list(self, raw, list_url):
        soup = _make_soup(raw, site_id=self.site_id)
        if soup is None:
            return []

        records = []
        seen_local: set[str] = set()

        for li in soup.select("ul.search-results li"):
            a = li.select_one("a[href][title]")
            if not a:
                continue
            href = a.get("href", "").strip()
            if not href:
                continue
            url = urljoin(self.base_url, href)
            if not _is_press_url(url):
                continue
            if url in seen_local:
                continue
            seen_local.add(url)

            title = _one_line(a.get("title") or a.get_text(" ", strip=True))
            if not title:
                continue

            date_div = li.select_one(".search-results__info div")
            listed_date = _parse_date_ddmmyyyy(
                _one_line(date_div.get_text(" ", strip=True)) if date_div else ""
            )

            records.append({
                "url": url,
                "title": title,
                "listed_date": listed_date,
                "list_url": list_url,
            })

        return records

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, raw, record):
        soup = _make_soup(raw, site_id=self.site_id)
        if soup is None:
            soup = BeautifulSoup("", "html.parser")

        url = record.get("url") or ""

        # --- Canonical URL ---
        canon = _meta_content(soup, "og:url") or url
        url = urljoin(self.base_url, canon) or url

        # --- JSON-LD ---
        jld = _extract_jsonld(raw, "@type", "NewsArticle")

        # --- Title ---
        h1 = soup.select_one("h1")
        title = (
            _one_line(h1.get_text(" ", strip=True)) if h1 else ""
        ) or _one_line(jld.get("headline") or "") or record.get("title") or ""
        title = re.sub(r"\s*\|\s*.+$", "", title).strip()

        # --- Dates ---
        published_date = _iso_date_from_jsonld(jld.get("datePublished") or "")
        listed_date = record.get("listed_date") or ""

        # Date from the press-release box (DD/MM/YYYY format)
        box = soup.select_one(".press-release-form__box-content strong")
        if box:
            box_date = _parse_date_ddmmyyyy(_one_line(box.get_text()))
            if box_date:
                if not published_date:
                    published_date = box_date
                if not listed_date:
                    listed_date = box_date

        # --- Category / section ---
        subtitle_node = soup.select_one(".press-release-form__subtitle h3")
        category = _one_line(subtitle_node.get_text()) if subtitle_node else ""
        if not category:
            category = (jld.get("articleSection") or "")
            if isinstance(category, list):
                category = "; ".join(category)

        # --- Keywords ---
        kw_raw = jld.get("keywords") or []
        if isinstance(kw_raw, str):
            kw_raw = [kw_raw]
        keywords = ",".join(dict.fromkeys(k.strip() for k in kw_raw if k.strip()))

        # --- Abstract / body text ---
        abstract = _extract_body_text(soup)
        if not abstract:
            abstract = _one_line(jld.get("description") or "")
        if not abstract:
            abstract = _meta_content(soup, "description", "og:description", "twitter:description")

        # --- PDF URL ---
        pdf_url = ""
        original_filename = None
        for a in soup.select("a[href]"):
            href = urljoin(self.base_url, a.get("href", "").strip())
            if re.search(r"\.pdf(?:[?#]|$)", href, re.I):
                # Prefer native miteco PDFs over external (BOE, etc.)
                if "miteco.gob.es" in href or "mapa-gob.es" in href:
                    pdf_url = href
                    break
                if not pdf_url:
                    pdf_url = href
        if pdf_url:
            original_filename = pdf_url.rstrip("/").split("/")[-1].split("?")[0] or None
            if original_filename:
                try:
                    from urllib.parse import unquote
                    original_filename = unquote(original_filename)
                except Exception:
                    pass

        # --- Authors ---
        authors_raw = jld.get("author") or []
        if isinstance(authors_raw, dict):
            authors_raw = [authors_raw]
        authors = ";".join(
            a.get("name", "").strip()
            for a in authors_raw
            if isinstance(a, dict) and a.get("name", "").strip()
        )

        # --- Publisher ---
        pub_raw = jld.get("publisher") or {}
        publisher = ""
        if isinstance(pub_raw, dict):
            publisher = pub_raw.get("name", "").strip()
        if not publisher:
            publisher = "Ministerio para la Transición Ecológica y el Reto Demográfico"

        # --- External ID and post_number ---
        path = urlparse(url).path.strip("/")
        # path example: es/prensa/ultimas-noticias/2026/junio/slug
        parts = path.split("/")
        slug = parts[-1].replace(".html", "") if parts else ""
        external_id = path.replace(".html", "")
        # post_number: use the slug (alphanumeric-dash); no numeric ID on this site
        post_number = slug or None

        # --- DOI ---
        doi_match = re.search(r"https?://(?:dx\.)?doi\.org/(10\.[^\s\"'<>]+)", raw or "")
        doi = doi_match.group(1).rstrip(".,)") if doi_match else None

        # --- Image ---
        image_url = _meta_content(soup, "og:image", "twitter:image") or ""
        if image_url and image_url.startswith("/"):
            image_url = self.base_url + image_url

        metadata = {
            "posted_date": listed_date,
            "category": category,
            "image_url": image_url,
            "list_url": record.get("list_url", ""),
            "jsonld_type": jld.get("@type", ""),
        }
        if original_filename:
            metadata["originalFilename"] = original_filename

        return {
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "authors": authors,
            "publisher": publisher,
            "url": url,
            "pdf_url": pdf_url or None,
            "keywords": keywords or None,
            "category": category or None,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }


# ------------------------------------------------------------------
# Helpers (module-level so they don't pollute the class namespace)
# ------------------------------------------------------------------

def _split_curl_output(raw, fallback_url):
    marker_pos = raw.rfind("\n" + _CURL_META_MARKER)
    if marker_pos == -1:
        return raw, "", fallback_url
    body = raw[:marker_pos]
    meta = raw[marker_pos + 1 + len(_CURL_META_MARKER):].strip()
    if "\t" not in meta:
        return body, "", fallback_url
    http_code, effective_url = meta.split("\t", 1)
    return body, http_code.strip(), effective_url.strip() or fallback_url


def _make_soup(raw, site_id="miteco-gob-es-es"):
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    text = raw or ""
    last_exc = None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(text, parser)
        except Exception as exc:
            last_exc = exc
            print(f"[{site_id}] BeautifulSoup({parser}) failed: {exc}")
    print(f"[{site_id}] all parsers failed: {last_exc}")
    return None


def _meta_content(soup, *keys):
    for key in keys:
        for tag in ({"name": key}, {"property": key}):
            node = soup.find("meta", attrs=tag)
            if node and node.get("content"):
                return node["content"].strip()
    return ""


def _extract_jsonld(raw, key, value):
    if not raw:
        return {}
    for m in re.finditer(r"<script[^>]*type=\"application/ld\+json\"[^>]*>(.*?)</script>", raw, re.DOTALL):
        try:
            d = json.loads(m.group(1).strip())
            if not isinstance(d, dict):
                continue
            if d.get(key) == value:
                return d
        except Exception:
            pass
    return {}


def _extract_body_text(soup):
    container = (
        soup.select_one(".text .cmp-text")
        or soup.select_one(".press-release-form")
        or soup.select_one("main")
        or soup
    )
    # Make a copy to strip noise
    try:
        working = BeautifulSoup(str(container), "html.parser")
    except Exception:
        working = container

    for bad in working.select(
        "script, style, noscript, iframe, header, footer, nav, "
        ".cookie, .modal, .breadcrumb, .pagination, .share"
    ):
        bad.decompose()

    parts = []
    seen: set[str] = set()
    for node in working.find_all(["p", "li", "h2", "h3"], recursive=True):
        text = _one_line(node.get_text(" ", strip=True))
        if len(text) < 20:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        parts.append(text)

    return "\n\n".join(parts)


def _one_line(value):
    text = unescape(str(value or ""))
    text = text.replace("\xa0", " ").replace("​", "")
    return re.sub(r"\s+", " ", text).strip()


def _parse_date_ddmmyyyy(text):
    m = re.search(r"\b(\d{2})/(\d{2})/(\d{4})\b", text or "")
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}"
    # Also accept YYYY-MM-DD
    m2 = re.search(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b", text or "")
    if m2:
        return m2.group(0)
    return ""


def _iso_date_from_jsonld(value):
    if not value:
        return ""
    m = re.match(r"((?:19|20)\d{2}-\d{2}-\d{2})", value)
    if m:
        return m.group(1)
    return ""


def _is_press_url(url):
    parsed = urlparse(url or "")
    return (
        parsed.netloc in {"www.miteco.gob.es", "miteco.gob.es"}
        and "/prensa/" in parsed.path
        and parsed.path.endswith(".html")
        and parsed.path not in {
            "/es/prensa/ultimas-noticias.html",
            "/es/prensa/historico.html",
            "/es/prensa.html",
        }
    )

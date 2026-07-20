# -*- coding: utf-8 -*-
"""Crawler for FutureNeuro publications.

Starting URL:
    https://futureneurocentre.ie/our-research/publications/

The list page is server-rendered WordPress HTML for page 1. Further pages are
served by the theme AJAX endpoint:

    POST /wp-admin/admin-ajax.php
    action=load_more_posts
    postType=publications

Publication cards link to external journal pages and, when available, an
internal FutureNeuro lay-summary detail page. The internal detail page is the
preferred metadata URL because it contains the real abstract-like summary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from html import unescape
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse


_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402


SITE_ID = "futureneurocentre-ie-our-research"
BASE_URL = "https://futureneurocentre.ie"
START_URL = "https://futureneurocentre.ie/our-research/publications/"
AJAX_URL = "https://futureneurocentre.ie/wp-admin/admin-ajax.php"

BACKOFFS = (1, 3, 9)
SAFETY_CAP_PAGES = 200
MAX_CRAWL_SECONDS = 25 * 60
MIN_ABSTRACT_CHARS = 100

_BS_PARSER_CACHE = [None]
_DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s\"'<>]+)", re.IGNORECASE)

_PUBLISHER_BY_DOMAIN = {
    "advanced.onlinelibrary.wiley.com": "Wiley",
    "bpspubs.onlinelibrary.wiley.com": "Wiley",
    "onlinelibrary.wiley.com": "Wiley",
    "link.springer.com": "Springer Nature",
    "www.nature.com": "Springer Nature",
    "nature.com": "Springer Nature",
    "pubs.acs.org": "American Chemical Society",
    "www.sciencedirect.com": "Elsevier BV",
    "sciencedirect.com": "Elsevier BV",
    "www.ibroneuroreports.org": "Elsevier BV",
    "ibroneuroreports.org": "Elsevier BV",
    "www.tandfonline.com": "Taylor & Francis",
    "tandfonline.com": "Taylor & Francis",
    "pmc.ncbi.nlm.nih.gov": "National Library of Medicine",
}


def _make_soup(raw):
    """Build a BeautifulSoup object with the required tolerant parser chain."""
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        print(f"[{SITE_ID}] BeautifulSoup unavailable: {exc}")
        return None

    parsers = ["html5lib", "lxml", "html.parser"]
    cached = _BS_PARSER_CACHE[0]
    if cached:
        parsers = [cached] + [p for p in parsers if p != cached]

    for parser in parsers:
        try:
            soup = BeautifulSoup(raw or "", parser)
            _BS_PARSER_CACHE[0] = parser
            return soup
        except Exception as exc:
            print(f"[{SITE_ID}] BeautifulSoup {parser} failed: {exc}")
    return None


def _clean_text(value):
    if value is None:
        return ""
    text = unescape(str(value)).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _first_text(node, selector=None):
    if node is None:
        return ""
    target = node.select_one(selector) if selector else node
    return _clean_text(target.get_text(" ", strip=True)) if target else ""


def _absolute_url(url):
    if not url:
        return None
    return urljoin(BASE_URL, url.strip())


def _slug_from_url(url):
    if not url:
        return None
    path = urlparse(url).path.rstrip("/")
    if not path:
        return None
    slug = unquote(path.rsplit("/", 1)[-1]).strip()
    return slug or None


def _extract_doi(value):
    if not value:
        return None
    decoded = unquote(str(value))
    match = _DOI_RE.search(decoded)
    if not match:
        return None
    doi = match.group(1)
    doi = re.split(r"[?#]", doi, maxsplit=1)[0]
    return doi.rstrip(".,;)/")


def _publisher_from_url(url):
    host = (urlparse(url or "").netloc or "").lower()
    if host in _PUBLISHER_BY_DOMAIN:
        return _PUBLISHER_BY_DOMAIN[host]
    if host.startswith("www.") and host[4:] in _PUBLISHER_BY_DOMAIN:
        return _PUBLISHER_BY_DOMAIN[host[4:]]
    return host or None


def _filename_from_url(url):
    if not url:
        return None
    tail = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])
    if "." in tail and len(tail) <= 200:
        return tail
    return None


def _tag_taxonomy_from_href(href):
    parsed = urlparse(href or "")
    query = parse_qs(parsed.query)
    mapping = {
        "themetic-area": "thematic_area",
        "thematic-area": "thematic_area",
        "disease-area": "disease_area",
        "research-focus": "research_focus",
        "collaborator": "collaborator",
        "publications-tag": "publications_tag",
    }
    for raw_key, values in query.items():
        if raw_key in mapping:
            return mapping[raw_key], values[0] if values else None
    return None, None


class FutureNeuroCentreOurResearchCrawler(BaseCrawler):
    site_id = "futureneurocentre-ie-our-research"
    site_name = "Custom: futureneurocentre-ie-our-research"
    base_url = "https://futureneurocentre.ie"

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn, delay=delay)
        self.detail_delay = delay

    def _curl_text(self, url, context, *, method="GET", data=None, timeout=45):
        for attempt in range(3):
            try:
                cmd = [
                    "curl",
                    "--tls-max",
                    "1.3",
                    "-skL",
                    "--compressed",
                    "--max-time",
                    str(timeout),
                    "-w",
                    "\n__CURL_STATUS__:%{http_code}\n__CURL_EFFECTIVE_URL__:%{url_effective}",
                    "-H",
                    f"User-Agent: {self.USER_AGENT}",
                    "-H",
                    "Accept: text/html,application/xhtml+xml,application/json,*/*;q=0.8",
                    "-H",
                    "Accept-Language: en-US,en;q=0.9",
                ]
                if method.upper() == "POST":
                    cmd.extend(
                        [
                            "-X",
                            "POST",
                            "-H",
                            "Content-Type: application/x-www-form-urlencoded; charset=UTF-8",
                            "-H",
                            "X-Requested-With: XMLHttpRequest",
                            "-e",
                            START_URL,
                            "--data",
                            urlencode(data or {}, doseq=True),
                        ]
                    )
                cmd.append(url)

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 15,
                )
                text = result.stdout.decode("utf-8", errors="replace")
                body, status, effective_url = self._split_curl_output(text, url)
                status_int = int(status) if status.isdigit() else 0
                if result.returncode == 0 and body.strip() and status_int < 400:
                    return body, effective_url
                if context.startswith("item ") and status_int in (401, 403, 404, 410):
                    print(f"[{self.site_id}] curl {context} non-retryable http={status_int}")
                    return "", effective_url

                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                err = stderr or f"curl exit={result.returncode} http={status or 'unknown'}"
                print(f"[{self.site_id}] curl {context} attempt {attempt + 1}/3 failed: {err}")
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] curl {context} attempt {attempt + 1}/3 failed: {exc}")

            if attempt < 2:
                time.sleep(BACKOFFS[attempt])

        print(f"[{self.site_id}] curl {context} gave up after 3 attempts: {url}")
        return "", url

    def _split_curl_output(self, text, fallback_url):
        status_marker = "\n__CURL_STATUS__:"
        effective_marker = "\n__CURL_EFFECTIVE_URL__:"
        status_idx = text.rfind(status_marker)
        if status_idx == -1:
            return text, "", fallback_url
        body = text[:status_idx]
        rest = text[status_idx + len(status_marker):]
        effective_idx = rest.rfind(effective_marker)
        if effective_idx == -1:
            return body, rest.strip(), fallback_url
        status = rest[:effective_idx].strip()
        effective_url = rest[effective_idx + len(effective_marker):].strip() or fallback_url
        return body, status, effective_url

    def _extract_seed_config(self, html):
        nonce = None
        nonce_match = re.search(r'var\s+params\s*=\s*\{[^}]*"ajax_nonce"\s*:\s*"([^"]+)"', html)
        if nonce_match:
            nonce = nonce_match.group(1)
        if not nonce:
            nonce_match = re.search(r'"ajax_nonce"\s*:\s*"([^"]+)"', html)
            nonce = nonce_match.group(1) if nonce_match else None

        soup = _make_soup(html)
        config = {
            "nonce": nonce,
            "featured_id": "",
            "per_page": 12,
            "max_pages": SAFETY_CAP_PAGES,
        }
        if soup is None:
            return config

        holder = soup.select_one(".search-filter-holder--js")
        if holder:
            config["featured_id"] = holder.get("data-featured-id") or ""
            per_page = holder.get("data-filter-per-page")
            if per_page and per_page.isdigit():
                config["per_page"] = int(per_page)

        load_more = soup.select_one(".load-more-btn a[data-post-type='publications']")
        if load_more:
            max_pages = load_more.get("data-max-pages")
            per_page = load_more.get("data-per-page")
            if max_pages and max_pages.isdigit():
                config["max_pages"] = min(int(max_pages), SAFETY_CAP_PAGES)
            if per_page and per_page.isdigit():
                config["per_page"] = int(per_page)

        return config

    def _fetch_ajax_page(self, nonce, current_page, per_page, featured_id):
        data = {
            "action": "load_more_posts",
            "security": nonce,
            "postType": "publications",
            "perPage": str(per_page),
            "currPage": str(current_page),
            "excludePost": featured_id or "",
            "selectedTag": "",
            "selectedAuthor": "",
            "sortOrder": "date-desc",
            "searchPhrase": "",
        }
        raw, _ = self._curl_text(
            AJAX_URL,
            f"ajax page {current_page + 1}",
            method="POST",
            data=data,
            timeout=45,
        )
        if not raw:
            return "", None
        try:
            payload = json.loads(raw)
        except Exception as exc:
            print(f"[{self.site_id}] ajax page {current_page + 1} JSON failed: {exc}")
            return "", None
        return payload.get("html") or "", payload

    def _parse_cards(self, html):
        soup = _make_soup(html)
        if soup is None:
            return []

        cards = []
        for card in soup.select("article.publication-cards__single"):
            try:
                info = card.select_one("a.publication-cards__info")
                if not info:
                    continue
                external_url = _absolute_url(info.get("href"))
                title = _first_text(info, "h3") or _clean_text(info.get("aria-label"))
                if not external_url or not title:
                    continue

                time_tag = card.find("time")
                listed_date = _clean_text(time_tag.get("datetime")) if time_tag else None
                listed_date_raw = listed_date or (_clean_text(time_tag.get_text(" ", strip=True)) if time_tag else None)

                tags = []
                tag_taxonomies = []
                for tag in card.select(".publication-cards__tags a.tag"):
                    text = _clean_text(tag.get_text(" ", strip=True))
                    if not text:
                        continue
                    tags.append(text)
                    tax_name, tax_id = _tag_taxonomy_from_href(tag.get("href"))
                    tag_taxonomies.append(
                        {
                            "name": text,
                            "taxonomy": tax_name,
                            "term_id": tax_id,
                            "href": tag.get("href"),
                        }
                    )

                authors = [
                    _clean_text(a.get_text(" ", strip=True))
                    for a in card.select(".publication-cards__authors-drop a")
                    if _clean_text(a.get_text(" ", strip=True))
                ]

                lay_summary = None
                for link in card.select(".publication-btn-holder a[href]"):
                    href = _absolute_url(link.get("href"))
                    if href and urlparse(href).netloc.endswith("futureneurocentre.ie"):
                        lay_summary = href
                        break

                cards.append(
                    {
                        "title": title,
                        "external_url": external_url,
                        "detail_url": lay_summary or external_url,
                        "lay_summary_url": lay_summary,
                        "listed_date": listed_date,
                        "listed_date_raw": listed_date_raw,
                        "authors": authors,
                        "tags": tags,
                        "tag_taxonomies": tag_taxonomies,
                        "doi": _extract_doi(external_url),
                    }
                )
            except Exception as exc:
                print(f"[{self.site_id}] card parse failed: {exc}")
                continue

        return cards

    def _parse_internal_detail(self, html, url):
        soup = _make_soup(html)
        if soup is None:
            return {}

        body_classes = " ".join(soup.body.get("class", [])) if soup.body else ""
        post_id = None
        match = re.search(r"\bpostid-(\d+)\b", body_classes)
        if match:
            post_id = match.group(1)
        if not post_id:
            shortlink = soup.find("link", rel=lambda v: v and "shortlink" in v)
            match = re.search(r"[?&]p=(\d+)", shortlink.get("href", "") if shortlink else "")
            post_id = match.group(1) if match else None

        title = _first_text(soup, ".publication-page-single__title")
        if not title and soup.title:
            title = re.sub(r"\s+[\-–]\s+FutureNeuro\s*$", "", _clean_text(soup.title.get_text(" ", strip=True)))

        external_url = None
        for link in soup.select("a.btn[href]"):
            text = _clean_text(link.get_text(" ", strip=True)).lower()
            href = _absolute_url(link.get("href"))
            if href and "read full publication" in text:
                external_url = href
                break

        taxonomies = {}
        for row in soup.select(".publication-page-single__tax-row"):
            label = _first_text(row, ".publication-page-single__tax-col--title").rstrip(":")
            values = [
                _clean_text(a.get_text(" ", strip=True))
                for a in row.select(".publication-page-single__tax-col a")
                if _clean_text(a.get_text(" ", strip=True))
            ]
            if label and values:
                taxonomies[label] = values

        abstract_parts = []
        text_holder = soup.select_one(".post-page-single__content--publication .post-page-single__text")
        if text_holder:
            heading = None
            for child in text_holder.find_all(["h2", "h3", "h4", "p", "li"], recursive=True):
                text = _clean_text(child.get_text(" ", strip=True))
                if not text:
                    continue
                if child.name in ("h2", "h3", "h4"):
                    heading = text.rstrip(":")
                elif heading:
                    abstract_parts.append(f"{heading}: {text}")
                else:
                    abstract_parts.append(text)

        abstract = " ".join(abstract_parts)
        canonical = soup.find("link", rel=lambda v: v and "canonical" in v)

        return {
            "post_id": post_id,
            "title": title or None,
            "abstract": abstract or None,
            "external_url": external_url,
            "canonical_url": canonical.get("href") if canonical else url,
            "taxonomies": taxonomies,
            "journal": None,
            "journal_raw": None,
            "publisher": _publisher_from_url(external_url),
            "doi": _extract_doi(external_url),
            "published_date": None,
            "series": None,
            "volume": None,
            "issue": None,
        }

    def _meta_values(self, soup, *names):
        values = []
        wanted = {name.lower() for name in names}
        for meta in soup.find_all("meta"):
            key = (meta.get("name") or meta.get("property") or "").lower()
            if key in wanted:
                content = _clean_text(meta.get("content"))
                if content:
                    values.append(content)
        return values

    def _parse_external_detail(self, html, url):
        soup = _make_soup(html)
        if soup is None:
            return {}

        title = None
        title_values = self._meta_values(soup, "citation_title", "dc.title", "og:title")
        if title_values:
            title = title_values[0]
        elif soup.title:
            title = _clean_text(soup.title.get_text(" ", strip=True))

        abstract_values = self._meta_values(
            soup,
            "citation_abstract",
            "dc.description",
            "description",
            "og:description",
            "twitter:description",
        )
        abstract = abstract_values[0] if abstract_values else None
        if not abstract:
            for selector in (
                "#Abs1-content",
                "#abstract",
                "section[id*='abstract' i]",
                "div[class*='abstract' i]",
                "section[class*='abstract' i]",
            ):
                node = soup.select_one(selector)
                text = _clean_text(node.get_text(" ", strip=True)) if node else ""
                if len(text) >= MIN_ABSTRACT_CHARS:
                    abstract = text
                    break

        authors = self._meta_values(soup, "citation_author", "dc.creator")
        journal_values = self._meta_values(soup, "citation_journal_title", "prism.publicationname")
        doi_values = self._meta_values(soup, "citation_doi", "dc.identifier")
        date_values = self._meta_values(
            soup,
            "citation_publication_date",
            "citation_online_date",
            "article:published_time",
            "dc.date",
        )
        pdf_values = self._meta_values(soup, "citation_pdf_url")

        return {
            "post_id": None,
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "external_url": url,
            "canonical_url": url,
            "taxonomies": {},
            "journal": journal_values[0] if journal_values else None,
            "journal_raw": journal_values[0] if journal_values else None,
            "publisher": _publisher_from_url(url),
            "doi": _extract_doi(doi_values[0]) if doi_values else _extract_doi(url),
            "published_date": self._normalize_date(date_values[0]) if date_values else None,
            "pdf_url": pdf_values[0] if pdf_values else None,
            "series": None,
            "volume": None,
            "issue": None,
        }

    def _normalize_date(self, value):
        text = _clean_text(value)
        if not text:
            return None
        match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
        if match:
            return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
        match = re.search(r"(\d{4})[-/](\d{1,2})", text)
        if match:
            return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-01"
        match = re.search(r"\b(\d{4})\b", text)
        return f"{int(match.group(1)):04d}-01-01" if match else None

    def _is_internal_publication_url(self, url):
        parsed = urlparse(url or "")
        return parsed.netloc.endswith("futureneurocentre.ie") and "/publications/" in parsed.path

    def _build_external_id(self, detail, card):
        post_id = detail.get("post_id")
        if post_id:
            return post_id, post_id

        detail_url = card.get("detail_url") or card.get("external_url")
        slug = _slug_from_url(detail_url)
        doi = detail.get("doi") or card.get("doi")
        if slug and self._is_internal_publication_url(detail_url):
            return slug, slug
        if doi:
            return doi.lower(), doi.lower()
        digest = hashlib.sha1((detail_url or card.get("title", "")).encode("utf-8", errors="replace")).hexdigest()
        return digest, slug

    def _record_from_detail(self, card, detail, effective_detail_url):
        external_url = detail.get("external_url") or card.get("external_url")
        doi = detail.get("doi") or card.get("doi") or _extract_doi(external_url)
        external_id, post_number = self._build_external_id(detail, card)

        detail_title = detail.get("title")
        title = detail_title if detail_title and len(detail_title) > 8 else card.get("title")
        abstract = _clean_text(detail.get("abstract"))
        listed_date = card.get("listed_date") or self._normalize_date(card.get("listed_date_raw"))
        published_date = detail.get("published_date") or listed_date

        taxonomies = detail.get("taxonomies") or {}
        tags = list(card.get("tags") or [])
        for values in taxonomies.values():
            for value in values:
                if value not in tags:
                    tags.append(value)

        authors = detail.get("authors") or card.get("authors") or []
        publisher = detail.get("publisher") or _publisher_from_url(external_url)
        journal = detail.get("journal")
        pdf_url = detail.get("pdf_url")
        original_filename = _filename_from_url(pdf_url)
        category = None
        if taxonomies.get("Thematic Area"):
            category = "; ".join(taxonomies["Thematic Area"])
        elif tags:
            category = tags[0]

        metadata = {
            "posted_date": card.get("listed_date_raw") or listed_date,
            "listed_date": listed_date,
            "originalFilename": original_filename,
            "journal_raw": detail.get("journal_raw") or journal,
            "series": detail.get("series"),
            "volume": detail.get("volume"),
            "issue": detail.get("issue"),
            "post_number": post_number,
            "node_id": detail.get("post_id"),
            "post_id": detail.get("post_id"),
            "slug": _slug_from_url(effective_detail_url),
            "external_article_url": external_url,
            "lay_summary_url": card.get("lay_summary_url"),
            "effective_detail_url": effective_detail_url,
            "source_list_title": card.get("title"),
            "futureNeuroAuthors": card.get("authors") or [],
            "tags": tags,
            "tag_taxonomies": card.get("tag_taxonomies") or [],
            "detail_taxonomies": taxonomies,
            "doi": doi,
            "publisher_raw": publisher,
            "native_fields": {
                "postid": detail.get("post_id"),
                "shortlink_id": detail.get("post_id"),
            },
        }

        return {
            "id": f"{self.site_id}:{external_id}",
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": "; ".join(authors) if authors else None,
            "publisher": publisher,
            "department": "FutureNeuro Research Ireland Centre",
            "journal": journal,
            "url": effective_detail_url,
            "pdf_url": pdf_url,
            "keywords": ", ".join(tags) if tags else None,
            "category": category or "Publications",
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        }

    def _parse_detail(self, html, url):
        if self._is_internal_publication_url(url):
            return self._parse_internal_detail(html, url)
        return self._parse_external_detail(html, url)

    def _deadline_near(self, start_time):
        return time.time() - start_time >= MAX_CRAWL_SECONDS - 30

    def crawl(self, limit=None):
        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                print(f"[{self.site_id}] invalid limit {limit!r}; using unlimited")
                limit = None
        if limit is not None and limit <= 0:
            return 0

        saved = 0
        page = 1
        start_time = time.time()
        limit_or_inf = str(limit) if limit is not None else "inf"
        seen_urls = set()

        seed_html, _ = self._curl_text(START_URL, "seed page", timeout=60)
        if not seed_html:
            print(f"[{self.site_id}] seed page fetch failed")
            return saved

        config = self._extract_seed_config(seed_html)
        nonce = config.get("nonce")
        if not nonce:
            print(f"[{self.site_id}] AJAX nonce not found; cannot paginate")
            return saved

        max_pages = min(config.get("max_pages") or SAFETY_CAP_PAGES, SAFETY_CAP_PAGES)
        per_page = config.get("per_page") or 12
        featured_id = config.get("featured_id") or ""
        print(
            f"[{self.site_id}] list endpoint: {AJAX_URL} "
            f"per_page={per_page} max_pages={max_pages} featured_id={featured_id}"
        )

        while page <= SAFETY_CAP_PAGES:
            if self._deadline_near(start_time):
                print(f"[{self.site_id}] approaching 25-minute budget, exiting cleanly")
                break
            if limit is not None and saved >= limit:
                break

            if page == 1:
                page_html = seed_html
            else:
                page_html, _payload = self._fetch_ajax_page(
                    nonce,
                    page - 1,
                    per_page,
                    featured_id,
                )
                if not page_html.strip():
                    print(f"[{self.site_id}] page {page}: empty response, end of pagination")
                    break

            cards = self._parse_cards(page_html)
            if not cards:
                print(f"[{self.site_id}] page {page}: no records, end of pagination")
                break

            new_records = 0
            for idx, card in enumerate(cards, start=1):
                url_key = card.get("detail_url") or card.get("external_url")
                if not url_key or url_key in seen_urls:
                    continue
                seen_urls.add(url_key)
                new_records += 1

                if limit is not None and saved >= limit:
                    break
                if self._deadline_near(start_time):
                    print(f"[{self.site_id}] approaching 25-minute budget during detail fetch")
                    break

                try:
                    detail_url = card.get("detail_url") or card.get("external_url")
                    detail_html, effective_url = self._curl_text(
                        detail_url,
                        f"item {page}-{idx} detail",
                        timeout=35 if self._is_internal_publication_url(detail_url) else 20,
                    )
                    if not detail_html:
                        print(f"[{self.site_id}] item {page}-{idx} skipped: empty detail")
                        time.sleep(self.detail_delay)
                        continue

                    detail = self._parse_detail(detail_html, effective_url or detail_url)
                    record = self._record_from_detail(card, detail, effective_url or detail_url)
                    if len(record.get("abstract") or "") < MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {page}-{idx} skipped: "
                            f"abstract too short ({len(record.get('abstract') or '')})"
                        )
                        time.sleep(self.detail_delay)
                        continue

                    self._save_paper(record)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {record['title'][:80]}")
                    time.sleep(self.detail_delay)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {page}-{idx} failed: {exc}")
                    continue

            if new_records == 0:
                print(f"[{self.site_id}] page {page}: 0 new records, stopping")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            if page >= max_pages:
                print(f"[{self.site_id}] page {page}: reached site max_pages={max_pages}")
                break

            page += 1

        if page > SAFETY_CAP_PAGES:
            print(f"[{self.site_id}] safety cap of {SAFETY_CAP_PAGES} pages reached")

        print(f"[{self.site_id}] done: saved {saved}/{limit_or_inf}")
        return saved

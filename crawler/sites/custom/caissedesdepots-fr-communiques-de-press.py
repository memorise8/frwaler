# -*- coding: utf-8 -*-
"""Caisse des Dépôts - Communiqués de presse crawler with source pagination and HTML press details."""

import json
import os
import re
import subprocess
import time
from datetime import date
from urllib.parse import unquote, urljoin, urlsplit, parse_qs

from crawler.base_crawler import BaseCrawler

_FRENCH_MONTHS = {
    'janvier': '01', 'février': '02', 'mars': '03', 'avril': '04',
    'mai': '05', 'juin': '06', 'juillet': '07', 'août': '08',
    'septembre': '09', 'octobre': '10', 'novembre': '11', 'décembre': '12',
}


def _parse_french_date(raw: str) -> str:
    """Parse '11 mai 2026' → '2026-05-11'. Returns an empty value when the complete date is invalid."""
    raw = raw.strip()
    parts = raw.split()
    if len(parts) == 3:
        day, month_fr, year = parts
        month = _FRENCH_MONTHS.get(month_fr.lower())
        if month:
            try:
                return date(int(year), int(month), int(day)).isoformat()
            except ValueError:
                pass
    return ""


class CaisseDesDepotsCommPresseCrawler(BaseCrawler):
    site_id = "caissedesdepots-fr-communiques-de-press"
    site_name = "Custom: caissedesdepots-fr-communiques-de-press"
    base_url = "https://www.caissedesdepots.fr"

    _LIST_URL = "https://www.caissedesdepots.fr/presse/communiques-de-presse"

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: fr-FR,fr;q=0.9,en;q=0.8",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < 2:
                    wait = (attempt + 1) * 3
                    print(f"[{self.site_id}] Empty response for {url}, "
                          f"retrying in {wait}s...")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < 2:
                    wait = (attempt + 1) * 3
                    print(f"[{self.site_id}] curl error: {exc}, "
                          f"retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # HTML parsing
    # ------------------------------------------------------------------

    def _parse_page(self, html: str) -> list[dict]:
        """Parse one list page; return list of raw item dicts."""
        try:
            from bs4 import BeautifulSoup, NavigableString
        except ImportError as exc:
            print(f"[{self.site_id}] BeautifulSoup not available: {exc}")
            return []

        soup = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                soup = BeautifulSoup(html, parser)
                break
            except Exception:
                continue
        if soup is None:
            return []

        items = []
        for article in soup.select('article[data-component-id="cdc:card"]'):
            try:
                link = article.select_one("a.c-card__link")
                if not link:
                    continue

                pdf_href = link.get("href", "")
                if not pdf_href:
                    continue

                # Title: collect bare NavigableString children of <a>, skip spans
                title_parts = []
                for child in link.children:
                    if isinstance(child, NavigableString):
                        t = str(child).strip()
                        if t:
                            title_parts.append(t)
                title = " ".join(title_parts).strip()

                # Fallback: strip prefix/suffix from title attribute
                if not title:
                    title_attr = link.get("title", "")
                    if title_attr.startswith("Télécharger "):
                        title_attr = title_attr[len("Télécharger "):]
                    title = re.sub(
                        r"\s*\(PDF\s*-[^)]+\),?\s*Nouvelle\s+fenêtre\s*$",
                        "", title_attr, flags=re.IGNORECASE,
                    ).strip()

                if not title:
                    continue

                # Resolve absolute PDF URL
                if pdf_href.startswith("/"):
                    pdf_url = self.base_url + pdf_href
                elif pdf_href.startswith("http"):
                    pdf_url = pdf_href
                else:
                    pdf_url = self.base_url + "/" + pdf_href

                # Date (French: "11 mai 2026")
                date_el = article.select_one("p.c-card__date")
                if date_el is None and urlsplit(pdf_url).path.lower().endswith(".pdf"):
                    date_el = article.select_one("p.c-card__desc")
                date_raw = date_el.get_text(strip=True) if date_el else ""
                date_iso = _parse_french_date(date_raw) if date_raw else ""

                # Category tag
                tag_el = article.select_one("p.c-card__tag")
                category = tag_el.get_text(strip=True) if tag_el else ""

                # Original filename from URL path
                path_segment = pdf_href.split("?")[0].split("/")[-1]
                original_filename = unquote(path_segment)

                items.append({
                    "title": title,
                    "pdf_url": pdf_url,
                    "pdf_href": pdf_href,
                    "date_raw": date_raw,
                    "date_iso": date_iso,
                    "category": category,
                    "original_filename": original_filename,
                    "url": pdf_url,
                    "summary": article.select_one("p.c-card__desc").get_text(" ", strip=True) if article.select_one("p.c-card__desc") else "",
                })
            except Exception as exc:
                print(f"[{self.site_id}] article parse error: {exc}")
                continue

        return items

    @staticmethod
    def _soup(raw):
        from bs4 import BeautifulSoup
        return BeautifulSoup(raw, "lxml")

    def _next_page(self, raw, current_url):
        links = self._soup(raw).select('.pager__item--next a[href], a[rel="next"]')
        if not links:
            return None
        urls = {urljoin(current_url, a["href"]) for a in links}
        if len(urls) != 1:
            raise ValueError("ambiguous next page")
        target = urls.pop()
        current, following = urlsplit(current_url), urlsplit(target)
        query = parse_qs(following.query, keep_blank_values=True)
        if (following.scheme != "https" or following.netloc != urlsplit(self.base_url).netloc
                or following.path != urlsplit(self._LIST_URL).path
                or set(query) != {"page"} or len(query["page"]) != 1
                or not query["page"][0].isdigit()
                or int(query["page"][0]) != int(parse_qs(current.query).get("page", ["0"])[0]) + 1):
            raise ValueError("next page leaves the source catalogue or skips a page")
        return target

    def _iter_pages(self, start_url=None):
        url = start_url or self._LIST_URL + "?page=0"
        seen = set()
        while url:
            self._check_cancelled()
            if url in seen:
                raise ValueError("source pagination repeated")
            seen.add(url)
            raw = self._curl_get(url)
            self._check_cancelled()
            if not raw or not self._soup(raw).select_one('main .results__title'):
                raise ValueError("source catalogue missing or invalid")
            items = self._parse_page(raw)
            next_url = self._next_page(raw, url)
            if not items and next_url:
                raise ValueError("empty source page still has a next page")
            yield url, items
            url = next_url
            if url:
                time.sleep(self._delay)

    def _parse_detail(self, raw, item):
        soup = self._soup(raw)
        main = soup.select_one("main")
        if main is None or main.select_one("h1.page__title") is None:
            raise ValueError("press detail is missing its main title")
        paragraphs = []
        for block in main.select('[data-component-id="cdc:wysiwyg"]'):
            text = " ".join(block.get_text(" ", strip=True).split())
            if text:
                paragraphs.append(text)
        abstract = "\n\n".join(paragraphs)
        date_node = main.select_one(".c-metatag--date")
        date_raw = " ".join(date_node.get_text(" ", strip=True).split()) if date_node else ""
        published_raw = re.sub(r"^Date de publication\s*", "", date_raw).strip()
        # Only the detail's explicit publication label establishes this role.
        published = _parse_french_date(published_raw) if date_raw.startswith("Date de publication") else ""
        pdfs = []
        for a in main.select("a[href]"):
            href = urljoin(item["url"], a["href"])
            if urlsplit(href).scheme in {"http", "https"} and urlsplit(href).path.lower().endswith(".pdf") and href not in pdfs:
                pdfs.append(href)
        if not abstract and not pdfs:
            raise ValueError("press detail has neither source body nor PDF attachment")
        title = " ".join(main.select_one("h1.page__title").get_text(" ", strip=True).split())
        return dict(title=title, abstract=abstract, published_date=published,
                    publication_date_raw=date_raw, pdf_urls=pdfs,
                    abstract_source="original HTML press body" if abstract else
                    "no HTML body; source PDF attachment full text pending")

    def _paper(self, item):
        url = item["url"]
        direct_pdf = urlsplit(url).path.lower().endswith(".pdf")
        if direct_pdf:
            detail = dict(title=item["title"], abstract=item["summary"],
                          published_date=item["date_iso"], publication_date_raw=item["date_raw"],
                          pdf_urls=[url], abstract_source="original PDF listing description; full text not fetched")
        else:
            self._check_cancelled()
            time.sleep(self._delay)
            raw = self._curl_get(url)
            self._check_cancelled()
            if not raw:
                raise ValueError("press detail fetch failed")
            detail = self._parse_detail(raw, item)
        pdf_url = detail["pdf_urls"][0] if detail["pdf_urls"] else None
        filename = unquote(urlsplit(pdf_url).path.rsplit("/", 1)[-1]) if pdf_url else None
        # Keep the original list href and slug identities, even though the
        # attachment filename now correctly comes from the actual PDF URL.
        old_filename = item["original_filename"]
        post_number = old_filename.rsplit(".", 1)[0] if "." in old_filename else old_filename
        return dict(site_id=self.site_id, external_id=item["pdf_href"], post_number=post_number,
                    title=detail["title"], abstract=detail["abstract"],
                    published_date=detail["published_date"], listed_date=item["date_iso"],
                    url=url, pdf_url=pdf_url, original_filename=filename,
                    publisher="Caisse des Dépôts et Consignations", authors=None,
                    category=item["category"] or "Communiqué de presse", keywords=None,
                    doi=None, department=None, journal=None,
                    metadata=json.dumps(dict(posted_date=item["date_raw"],
                        publication_date_raw=detail["publication_date_raw"],
                        originalFilename=filename, source_pdf_urls=detail["pdf_urls"],
                        abstract_source=detail["abstract_source"]), ensure_ascii=False))

    # Pure enumeration entry points; crawl() and the parsers above stay unchanged.
    def _enumeration_items(self, html, url=None):
        """One catalogue page through crawl()'s checks and _parse_page (no fetching)."""
        if not html or not self._soup(html).select_one('main .results__title'):
            raise ValueError("source catalogue missing or invalid")
        items = self._parse_page(html)
        if not items and self._next_page(html, url or self._LIST_URL + "?page=0"):
            raise ValueError("empty source page still has a next page")
        return items

    def _enumeration_detail(self, html, item, url=None):
        """crawl()'s _paper with the detail page the framework already fetched
        (direct-PDF cards use only their listing fields, as in _paper)."""
        def fetched(_url):
            return html
        self._curl_get = fetched
        try:
            return self._paper(item)
        finally:
            del self._curl_get

    def _save_document(self, document):
        # The legacy shared upsert deletes stored files when pdf_url changes.
        # Historical HTML-as-PDF rows need a separate evidence-backed repair;
        # a normal crawl must retain their IDs, summaries and file links.
        existing = self._conn.execute(
            "SELECT pdf_url FROM documents WHERE site_id = ? AND external_id = ?",
            (self.site_id, document.get("external_id")),
        ).fetchall()
        if len(existing) > 1:
            raise ValueError("existing document identity is ambiguous; repair review required")
        if existing and (existing[0][0] or "") != (document.get("pdf_url") or ""):
            raise ValueError("existing attachment URL differs; preserve files pending repair review")
        return super()._save_document(document)

    def crawl(self, limit=None):
        self._check_cancelled()
        if limit is not None and limit <= 0:
            return 0
        self._source_failure = None
        self._exhausted = False
        started = time.monotonic()
        max_seconds = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
        saved, seen = 0, set()
        try:
            for page_url, items in self._iter_pages():
                for item in items:
                    self._check_cancelled()
                    if limit is not None and saved >= limit:
                        return saved
                    if time.monotonic() - started > max_seconds:
                        raise TimeoutError("source traversal time budget reached before completion")
                    if item["url"] in seen:
                        raise ValueError("source catalogue repeats a document URL")
                    paper = self._paper(item)
                    self._save_paper(paper)
                    if self._last_save_created:
                        saved += 1
                    seen.add(item["url"])
                if limit is not None and saved >= limit:
                    return saved
            self._check_cancelled()
            self._mark_exhausted()
        except Exception as exc:
            self._report_source_failure("invalid_response")
            raise RuntimeError(f"press collection incomplete: {exc}") from exc
        return saved

# -*- coding: utf-8 -*-
"""CD Howe Institute – Research Insights crawler.

Starting URL: https://cdhowe.org/research-insights/
Listing pagination: /research-insights/page/N/
Detail pages:       /publication/{slug}/

The site is a WordPress + Elementor install.  All content is server-rendered,
so no JS engine is required.  JSON-LD (application/ld+json) is the cleanest
source for title / date / authors; the full abstract lives in
div.elementor-shortcode paragraphs.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_SITE_ID = "cdhowe-org-research-insights"
_BASE_URL = "https://cdhowe.org"
_LIST_URL  = "https://cdhowe.org/research-insights/"
_PUB_PAT   = re.compile(r'href="(https://cdhowe\.org/publication/[^"?#\s]+)"')
_ABSTRACT_MIN = 50   # chars — items below this are skipped (not saved)
_MAX_PAGES    = 200
_MAX_MINUTES  = 25


class CDHoweResearchInsightsCrawler(BaseCrawler):
    site_id   = _SITE_ID
    site_name = "Custom: cdhowe-org-research-insights"
    base_url  = _BASE_URL

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url: str, *, referer: str | None = None, timeout: int = 45) -> str | None:
        """GET via curl with TLS-max-1.3 and 3-attempt exponential back-off."""
        cmd = [
            "curl",
            "--tls-max", "1.3",
            "-skL",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: en-CA,en;q=0.9",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        waits = [1, 3, 9]
        last_err = "unknown"
        for attempt, wait in enumerate(waits, start=1):
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 10,
                    check=False,
                )
            except Exception as exc:
                last_err = str(exc)
            else:
                raw = proc.stdout.decode("utf-8", errors="replace")
                if proc.returncode == 0 and raw.strip():
                    return raw
                stderr = proc.stderr.decode("utf-8", errors="replace").strip()
                last_err = f"exit={proc.returncode} stderr={stderr[:120]}"

            if attempt < len(waits):
                print(
                    f"[{_SITE_ID}] curl attempt {attempt}/{len(waits)} for {url}: "
                    f"{last_err}; retry in {wait}s"
                )
                time.sleep(wait)

        print(f"[{_SITE_ID}] curl failed after 3 attempts for {url}: {last_err}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _soup(self, raw: str | None):
        """BeautifulSoup with html5lib → lxml → html.parser fallback."""
        if not raw:
            return None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
        return None

    @staticmethod
    def _clean(text: str) -> str:
        text = unescape(text or "")
        text = text.replace("\xa0", " ")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    # ------------------------------------------------------------------
    # Listing page helpers
    # ------------------------------------------------------------------

    def _list_url(self, page: int) -> str:
        return _LIST_URL if page == 1 else f"https://cdhowe.org/research-insights/page/{page}/"

    def _extract_pub_urls(self, raw: str) -> list[str]:
        """Return ordered, deduplicated publication URLs from a listing page."""
        seen: set[str] = set()
        out: list[str] = []
        for m in _PUB_PAT.finditer(raw):
            u = m.group(1).rstrip("/") + "/"
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    # ------------------------------------------------------------------
    # Detail page parser
    # ------------------------------------------------------------------

    def _parse_detail(self, url: str, raw: str) -> dict | None:
        soup = self._soup(raw)
        if soup is None:
            return None

        # ---- Title / date / authors from JSON-LD ----
        title = ""
        published_date = ""
        authors_list: list[str] = []
        json_ld_extra: dict = {}

        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                # Some pages wrap multiple schemas in a list
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("headline"):
                            data = item
                            break
                if not isinstance(data, dict) or not data.get("headline"):
                    continue
                title = data.get("headline", "")
                raw_date = data.get("datePublished") or data.get("dateModified") or ""
                if raw_date:
                    published_date = raw_date[:10]  # ISO YYYY-MM-DD
                auth_field = data.get("author") or []
                if isinstance(auth_field, dict):
                    auth_field = [auth_field]
                for a in auth_field:
                    name = a.get("name", "") if isinstance(a, dict) else str(a)
                    if name:
                        authors_list.append(name)
                json_ld_extra = {
                    "json_ld_type": data.get("@type", ""),
                    "json_ld_url": data.get("url", ""),
                }
                break
            except Exception:
                continue

        # Fallbacks for title
        if not title:
            og = soup.find("meta", property="og:title")
            if og:
                title = og.get("content") or ""
        if not title:
            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(strip=True)
        title = self._clean(title)

        # ---- Abstract ----
        # Priority 1: paragraphs inside elementor-shortcode (the article body)
        abstract = ""
        for div in soup.find_all("div", class_=lambda c: c and "elementor-shortcode" in c.split()):
            text = div.get_text(separator="\n", strip=True)
            # Skip widget blocks that are just buttons (Print / Download / Cite)
            if text and not re.match(r"\s*(Print|Download|Cite|Share)\b", text, re.I):
                abstract = self._clean(text)
                if len(abstract) >= _ABSTRACT_MIN:
                    break
                abstract = ""

        # Priority 2: elementor-widget-text-editor blocks
        if not abstract:
            for div in soup.find_all(
                "div", class_=lambda c: c and "elementor-widget-text-editor" in c.split()
            ):
                text = div.get_text(separator="\n", strip=True)
                if text and len(text) >= _ABSTRACT_MIN:
                    abstract = self._clean(text)
                    break

        # Priority 3: og:description excerpt
        if not abstract:
            og_desc = soup.find("meta", property="og:description")
            if og_desc:
                abstract = self._clean(og_desc.get("content") or "")

        # Priority 4: collect all <p> text from article area
        if not abstract or len(abstract) < _ABSTRACT_MIN:
            paragraphs = [p.get_text(strip=True) for p in soup.find_all("p") if p.get_text(strip=True)]
            candidate = "\n\n".join(paragraphs)
            if len(candidate) > len(abstract):
                abstract = self._clean(candidate)

        # ---- PDF ----
        pdf_url = None
        original_filename = None
        pdf_hits = re.findall(r'href="([^"]+\.pdf(?:\?[^"]*)?)"', raw, re.IGNORECASE)
        if pdf_hits:
            pdf_url = pdf_hits[0]
            original_filename = pdf_url.split("/")[-1].split("?")[0]

        # ---- Post ID from body class "postid-{N}" ----
        post_id: str | None = None
        body_tag = soup.find("body")
        if body_tag:
            for cls in (body_tag.get("class") or []):
                m = re.match(r"postid-(\d+)$", cls)
                if m:
                    post_id = m.group(1)
                    break

        slug = url.rstrip("/").split("/")[-1]

        # ---- Publication type (from breadcrumb link) ----
        pub_type = ""
        for a in soup.find_all("a", href=True):
            if "/publication-type/" in a["href"]:
                pub_type = a.get_text(strip=True)
                break

        # ---- Topic category (from Elementor post-info widget) ----
        category = ""
        for span in soup.find_all("span", class_="elementor-post-info__terms-list-item"):
            txt = span.get_text(strip=True)
            if txt and txt != pub_type:
                category = txt
                break
        if not category:
            category = pub_type

        return {
            "id": None,
            "site_id": _SITE_ID,
            "external_id": post_id or slug,
            "post_number": post_id,          # numeric string; used for incremental cursor
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": published_date,
            "authors": "; ".join(authors_list),
            "publisher": "CD Howe Institute",
            "department": "",
            "journal": "",
            "url": url,
            "pdf_url": pdf_url,
            "keywords": "",
            "category": category,
            "doi": "",
            "original_filename": original_filename,
            "metadata": json.dumps(
                {
                    "posted_date": published_date,
                    "originalFilename": original_filename,
                    "slug": slug,
                    "node_id": post_id,
                    "pub_type": pub_type,
                    **json_ld_extra,
                },
                ensure_ascii=False,
            ),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.monotonic()
        limit_or_inf = limit if limit is not None else "∞"

        for page_num in range(1, _MAX_PAGES + 1):
            elapsed_min = (time.monotonic() - start_time) / 60
            if elapsed_min >= _MAX_MINUTES:
                print(f"[{_SITE_ID}] Time budget ({_MAX_MINUTES}min) reached. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page_num == _MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            if page_num % 10 == 0:
                print(f"[{_SITE_ID}] page {page_num}: saved {saved}/{limit_or_inf}")

            list_url = self._list_url(page_num)
            raw = self._curl(list_url, referer=_BASE_URL)
            if not raw:
                print(f"[{_SITE_ID}] Failed to fetch listing page {page_num}. Stopping.")
                break

            pub_urls = self._extract_pub_urls(raw)
            new_urls = [u for u in pub_urls if u not in seen_urls]
            if not new_urls:
                print(f"[{_SITE_ID}] No new publication URLs on page {page_num}. Done.")
                break
            seen_urls.update(new_urls)

            for pub_url in new_urls:
                if limit is not None and saved >= limit:
                    break
                if (time.monotonic() - start_time) / 60 >= _MAX_MINUTES:
                    print(f"[{_SITE_ID}] Time budget reached mid-page. Stopping.")
                    break

                try:
                    time.sleep(self._delay)
                    detail_raw = self._curl(pub_url, referer=list_url)
                    if not detail_raw:
                        print(f"[{_SITE_ID}] item {pub_url} failed: empty response")
                        continue

                    paper = self._parse_detail(pub_url, detail_raw)
                    if paper is None:
                        print(f"[{_SITE_ID}] item {pub_url} failed: parse returned None")
                        continue

                    abstract = paper.get("abstract", "")
                    if len(abstract) < _ABSTRACT_MIN:
                        print(
                            f"[{_SITE_ID}] Skipping (abstract {len(abstract)} chars "
                            f"< {_ABSTRACT_MIN}): {pub_url}"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] Saved {saved}/{limit_or_inf}: {paper.get('title', '')[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {pub_url} failed: {exc}")
                    continue

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved

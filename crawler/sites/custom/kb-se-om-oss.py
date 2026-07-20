# -*- coding: utf-8 -*-
"""Crawler for KB.se nyheter (Kungliga biblioteket – Om oss/Nyheter).

Strategy
--------
* List pages:  GET https://www.kb.se/om-oss/nyheter.html?page=N
  Each page embeds a JSON blob via AppRegistry.registerInitialState(…).
  The blob has {"config":{"num":8,…}, "params":{"page":N}, "items":[…]}.
  Items carry title, short desc, date.iso, uri, nodeId.

* Detail pages: GET https://www.kb.se{item["uri"]}
  Full article text lives in <div class="sv-text-portlet-content"> elements.
  We skip portlets that are < 100 chars (title heading, nav blurbs) or
  contain "dataskyddsförordning" (GDPR footer).
"""

import json
import re
import subprocess
import time
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup
    _HAVE_BS4 = True
except ImportError:
    _HAVE_BS4 = False


class KBSeOmOssCrawler(BaseCrawler):
    site_id = "kb-se-om-oss"
    site_name = "Custom: kb-se-om-oss"
    base_url = "https://www.kb.se"

    _LIST_URL = "https://www.kb.se/om-oss/nyheter.html"
    _MAX_PAGES = 200
    _MAX_WALL_SECONDS = 25 * 60  # 25 minutes

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with up to 3 retries and exponential back-off."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: sv,en;q=0.8",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                wait = (attempt + 1) ** 2  # 1, 4, 9 s (last attempt skipped)
                if attempt < 2:
                    print(f"[kb-se-om-oss] Empty response, retry in {wait}s…")
                    time.sleep(wait)
            except Exception as exc:
                wait = (attempt + 1) ** 2
                if attempt < 2:
                    print(f"[kb-se-om-oss] curl error: {exc}, retry in {wait}s…")
                    time.sleep(wait)
                else:
                    print(f"[kb-se-om-oss] curl failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, html: str) -> list:
        """Return items list extracted from the embedded registerInitialState JSON.

        SiteVision renders the news portlet state as:
            AppRegistry.registerInitialState('portletId', { "config": {"num": 8, …}, "items": […] })
        We locate it by the unique "config":{"num": signature, walk back to the
        opening brace, then let the stdlib JSON decoder consume the whole object.
        """
        marker = '"config":{"num":'
        idx = html.find(marker)
        if idx < 0:
            return []
        start = html.rfind("{", 0, idx)
        if start < 0:
            return []
        try:
            data, _ = json.JSONDecoder().raw_decode(html, start)
            return data.get("items") or []
        except (json.JSONDecodeError, ValueError):
            return []

    # ------------------------------------------------------------------
    # Detail-page parsing
    # ------------------------------------------------------------------

    def _make_soup(self, html: str):
        """Parse HTML with html5lib → lxml → html.parser fallback."""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    def _extract_detail(self, url: str) -> dict:
        """Fetch a detail page and return abstract + pdf_url + published_date."""
        html = self._curl_get(url)
        if not html:
            return {}

        if _HAVE_BS4:
            soup = self._make_soup(html)
            if not soup:
                return {}

            parts = []
            for div in soup.find_all("div", class_="sv-text-portlet-content"):
                text = div.get_text(separator=" ", strip=True)
                if len(text) < 100:
                    continue
                if "dataskyddsförordning" in text or "GDPR" in text:
                    continue
                parts.append(text)

            abstract = "\n\n".join(parts)

            # PDF link
            pdf_url = None
            original_filename = None
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.lower().endswith(".pdf"):
                    pdf_url = urljoin(self.base_url, href)
                    original_filename = href.rstrip("/").split("/")[-1]
                    break

            # Date from <time class="kb-article-date">
            time_tag = soup.find("time", class_="kb-article-date")
            pub_date = time_tag.get("datetime", "") if time_tag else ""

        else:
            # Fallback: regex-only (no bs4)
            portlets = re.findall(
                r'<div class="sv-text-portlet-content">(.*?)</div>\s*</div>',
                html, re.DOTALL
            )
            parts = []
            for raw in portlets:
                text = re.sub(r"<[^>]+>", " ", raw)
                text = re.sub(r"&nbsp;", " ", text)
                text = re.sub(r"&[a-zA-Z]+;", "", text)
                text = re.sub(r"\s+", " ", text).strip()
                if len(text) < 100:
                    continue
                if "dataskyddsförordning" in text or "GDPR" in text:
                    continue
                parts.append(text)
            abstract = "\n\n".join(parts)

            m_pdf = re.search(r'href="([^"]+\.pdf)"', html, re.IGNORECASE)
            pdf_url = urljoin(self.base_url, m_pdf.group(1)) if m_pdf else None
            original_filename = pdf_url.rstrip("/").split("/")[-1] if pdf_url else None

            m_date = re.search(r'<time[^>]+class="kb-article-date"[^>]+datetime="([^"]+)"', html)
            pub_date = m_date.group(1) if m_date else ""

        return {
            "abstract": abstract,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "published_date": pub_date,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        for p in range(1, self._MAX_PAGES + 1):
            if p == self._MAX_PAGES:
                print(f"[kb-se-om-oss] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                break

            if time.time() - start_time > self._MAX_WALL_SECONDS:
                print(f"[kb-se-om-oss] Wall-clock budget exceeded at page {p}. Exiting cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            if p % 10 == 0:
                print(f"[kb-se-om-oss] page {p}: saved {saved}/{limit_str}")

            url = f"{self._LIST_URL}?page={p}"
            html = self._curl_get(url)
            if not html:
                print(f"[kb-se-om-oss] Failed to fetch page {p}. Stopping.")
                break

            items = self._parse_list_page(html)
            if not items:
                print(f"[kb-se-om-oss] No items on page {p}. Done.")
                break

            new_on_page = 0

            for item in items:
                if limit is not None and saved >= limit:
                    break

                uri = item.get("uri", "")
                if not uri:
                    continue

                detail_url = self.base_url + uri

                # URL dedup — catches silent loop-back to page 1
                if detail_url in seen_urls:
                    print(f"[kb-se-om-oss] Duplicate URL on page {p}. Stopping pagination.")
                    return saved
                seen_urls.add(detail_url)
                new_on_page += 1

                title = (item.get("title") or "").strip()
                desc = (item.get("desc") or "").strip()
                date_iso = item.get("date", {}).get("iso", "")
                millis = item.get("date", {}).get("millis")
                node_id = item.get("nodeId") or item.get("id") or ""
                keywords_list = item.get("keywords") or []

                try:
                    time.sleep(self._delay)
                    detail = self._extract_detail(detail_url)

                    # Build abstract: prefer full detail body; fallback to list desc
                    abstract = detail.get("abstract") or ""
                    if len(abstract) < 50:
                        abstract = desc
                    if len(abstract) < 50:
                        print(f"[kb-se-om-oss] Abstract too short for {title[:50]!r}, skipping.")
                        continue

                    pub_date = detail.get("published_date") or date_iso

                    paper = {
                        "site_id": self.site_id,
                        "external_id": node_id,
                        "post_number": str(int(millis)) if millis is not None else node_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": pub_date,
                        "listed_date": date_iso,
                        "url": detail_url,
                        "pdf_url": detail.get("pdf_url"),
                        "original_filename": detail.get("original_filename"),
                        "authors": "",
                        "publisher": "Kungliga biblioteket",
                        "department": None,
                        "journal": None,
                        "keywords": ",".join(str(k) for k in keywords_list),
                        "category": "Nyheter",
                        "doi": None,
                        "metadata": json.dumps({
                            "posted_date": date_iso,
                            "nodeId": node_id,
                            "millis": millis,
                            "pinned": item.get("pinned", False),
                            "image_uri": (item.get("image") or {}).get("uri", ""),
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[kb-se-om-oss] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[kb-se-om-oss] Item {title[:50]!r} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[kb-se-om-oss] No new items on page {p}. Done.")
                break

        print(f"[kb-se-om-oss] Done. Total saved: {saved}")
        return saved

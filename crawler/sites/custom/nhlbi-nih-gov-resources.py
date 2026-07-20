# -*- coding: utf-8 -*-
"""NHLBI Publications and Resources crawler.

Starting URL: https://www.nhlbi.nih.gov/resources
Pagination:   ?page=N  (0-indexed, ~34 pages, 10 items/page, 331 total)
Detail pages: /resources/<slug> — JSON-LD carries description/date/keywords.
"""

import json
import re
import subprocess
import sys
import time

# Absolute import — required because spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler


def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _curl_get(url: str, user_agent: str, extra_headers=None) -> str | None:
    """GET via curl with retry (1s, 3s, 9s backoff). Returns text or None."""
    cmd = [
        "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
        "-H", f"User-Agent: {user_agent}",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9",
    ]
    if extra_headers:
        for h in extra_headers:
            cmd += ["-H", h]
    cmd.append(url)

    for attempt in range(3):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            raw = result.stdout
            if raw:
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return raw.decode("utf-8", errors="replace")
            if attempt < 2:
                wait = 3 ** attempt  # 1s, 3s
                time.sleep(wait)
        except Exception as exc:
            if attempt < 2:
                wait = 3 ** attempt
                print(f"[nhlbi-nih-gov-resources] curl error ({attempt+1}/3): {exc}")
                time.sleep(wait)
            else:
                print(f"[nhlbi-nih-gov-resources] curl failed after 3 attempts: {exc}")
    return None


def _extract_json_ld(html: str) -> dict:
    """Return the first Article-like JSON-LD block from a page."""
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL
    ):
        try:
            data = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and "@graph" in data:
            graph = data["@graph"]
            if isinstance(graph, list) and graph:
                data = graph[0]
        if isinstance(data, dict):
            return data
    return {}


def _parse_list_page(html: str, base_url: str) -> list[dict]:
    """Parse a /resources?page=N listing page.

    Returns list of {external_id, title, url}.
    """
    soup = _make_soup(html)
    if soup is None:
        return []

    items = []
    articles = soup.find_all(
        "article",
        class_=lambda c: c and "node--type-nhlbi-publication" in c
    )
    for art in articles:
        external_id = str(art.get("data-id", "")).strip()

        # Prefer the anchor with data-title inside publicationTitle div
        link = art.find("a", attrs={"data-title": True})
        if not link:
            link = art.find("a", href=re.compile(r"^/resources/"))
        if not link:
            continue

        title = (link.get("data-title") or link.get_text(separator=" ", strip=True)).strip()
        href = link.get("href", "").strip()
        if not href:
            continue
        if not href.startswith("http"):
            href = base_url + href

        if title and href:
            items.append({"external_id": external_id, "title": title, "url": href})
    return items


def _parse_detail(url: str, html: str, base_url: str) -> dict:
    """Extract abstract, keywords, date, pdf_url, category from a detail page."""
    result = {
        "abstract": "",
        "keywords": [],
        "published_date": "",
        "pdf_url": "",
        "category": "",
    }

    ld = _extract_json_ld(html)
    if ld:
        result["abstract"] = (ld.get("description") or "").strip()
        result["published_date"] = (ld.get("datePublished") or "").strip()
        about = ld.get("about", [])
        if isinstance(about, list):
            result["keywords"] = [str(a).strip() for a in about if a]
        elif isinstance(about, str) and about.strip():
            result["keywords"] = [about.strip()]

    # PDF link — first .pdf href on page
    pdf_m = re.search(r'href=["\']([^"\']*\.pdf[^"\']*)["\']', html, re.IGNORECASE)
    if pdf_m:
        pdf = pdf_m.group(1)
        if not pdf.startswith("http"):
            pdf = base_url + pdf
        result["pdf_url"] = pdf

    # data-category on any link
    if not result["category"]:
        cat_m = re.search(r'data-category=["\']([^"\']+)["\']', html)
        if cat_m:
            result["category"] = cat_m.group(1).strip()

    # Augment short abstract from BeautifulSoup body fields
    if len(result["abstract"]) < 100:
        soup = _make_soup(html)
        if soup:
            for cls in (
                "field--name-field-description",
                "field--name-body",
                "nhlbi-publication-description",
                "field--name-field",
            ):
                div = soup.find(class_=lambda c: c and cls in c)
                if div:
                    txt = div.get_text(separator=" ", strip=True)
                    if len(txt) > len(result["abstract"]):
                        result["abstract"] = txt
                    if len(result["abstract"]) >= 100:
                        break

    return result


class NHLBIResourcesCrawler(BaseCrawler):
    """Crawler for NHLBI Publications and Resources."""

    site_id = "nhlbi-nih-gov-resources"
    site_name = "Custom: nhlbi-nih-gov-resources"
    base_url = "https://www.nhlbi.nih.gov"

    _LIST_URL = "https://www.nhlbi.nih.gov/resources"
    _MAX_PAGES = 200
    _MAX_WALL_SECONDS = 25 * 60  # 25 minutes

    def crawl(self, limit=None):
        """Paginate /resources?page=N and save publications.

        Parameters
        ----------
        limit:
            Maximum number of items to save.  None means unlimited.
        """
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_label = str(limit) if limit is not None else "∞"

        for page in range(self._MAX_PAGES):
            # Wall-clock budget
            if time.time() - start_time > self._MAX_WALL_SECONDS:
                print(f"[nhlbi-nih-gov-resources] Wall-clock budget reached at page {page}. Exiting.")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[nhlbi-nih-gov-resources] page {page}: saved {saved}/{limit_label}")

            if page == self._MAX_PAGES - 1:
                print(f"[nhlbi-nih-gov-resources] Safety cap of {self._MAX_PAGES} pages reached.")

            list_url = f"{self._LIST_URL}?page={page}"
            try:
                html = _curl_get(list_url, self.USER_AGENT)
            except KeyboardInterrupt:
                raise

            if not html:
                print(f"[nhlbi-nih-gov-resources] Failed to fetch list page {page}. Stopping.")
                break

            items = _parse_list_page(html, self.base_url)
            if not items:
                print(f"[nhlbi-nih-gov-resources] No items on page {page}. Done.")
                break

            # URL deduplication — detect silent loop-back to page 0
            new_items = [it for it in items if it["url"] not in seen_urls]
            if not new_items:
                print(f"[nhlbi-nih-gov-resources] All {len(items)} items on page {page} already seen. Done.")
                break

            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                url = item["url"]
                seen_urls.add(url)

                try:
                    time.sleep(self._delay)
                    detail_html = _curl_get(url, self.USER_AGENT)
                    if not detail_html:
                        print(f"[nhlbi-nih-gov-resources] item {url} failed: empty response; skipping")
                        continue

                    detail = _parse_detail(url, detail_html, self.base_url)

                    abstract = detail["abstract"]
                    if len(abstract) < 50:
                        print(
                            f"[nhlbi-nih-gov-resources] item {url} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": item["external_id"] or url.split("/")[-1],
                        "title": item["title"],
                        "authors": json.dumps([], ensure_ascii=False),
                        "abstract": abstract,
                        "category": detail["category"],
                        "keywords": json.dumps(detail["keywords"], ensure_ascii=False),
                        "published_date": detail["published_date"],
                        "url": url,
                        "pdf_url": detail["pdf_url"],
                        "doi": "",
                        "department": "NHLBI",
                        "metadata": json.dumps(
                            {"data_id": item["external_id"]},
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[nhlbi-nih-gov-resources] Saved {saved}/{limit_label}: "
                        f"{item['title'][:60]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[nhlbi-nih-gov-resources] item {url} failed: {exc}; skipping")
                    continue

        print(f"[nhlbi-nih-gov-resources] Done. Total saved: {saved}")
        return saved

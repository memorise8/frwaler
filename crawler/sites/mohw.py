# -*- coding: utf-8 -*-
"""Ministry of Health and Welfare (보건복지부) crawler."""

import json
import re
import uuid

from bs4 import BeautifulSoup

from ..base_crawler import BaseCrawler


class MOHWCrawler(BaseCrawler):
    """Crawler for the Korean Ministry of Health and Welfare publication board."""

    site_id = "mohw"
    site_name = "보건복지부"
    base_url = "https://www.mohw.go.kr"

    _LIST_URL = "https://www.mohw.go.kr/board.es"
    _LIST_PARAMS = {"mid": "a10411010100", "bid": "0019"}
    _VIEW_URL = "https://www.mohw.go.kr/board.es"
    _VIEW_PARAMS = {"mid": "a10411010100", "bid": "0019", "act": "view"}

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    def crawl(self, limit=None):
        """Crawl the MOHW publication board and save papers to the database."""
        saved = 0
        page = 1
        stop = False

        while not stop:
            # ---- Step 1: fetch list page ----
            params = {**self._LIST_PARAMS, "nPage": page}
            response = self._request(self._LIST_URL, params=params)
            if response is None:
                print(f"[{self.site_id}] Failed to fetch list page {page}. Stopping.")
                break

            soup = BeautifulSoup(response.text, "html.parser")
            list_items = self._parse_list(soup)

            if not list_items:
                print(f"[{self.site_id}] No items found on page {page}. Stopping.")
                break

            print(f"[{self.site_id}] Page {page}: found {len(list_items)} items")

            for basic_info in list_items:
                if limit is not None and saved >= limit:
                    stop = True
                    break

                list_no = basic_info.get("list_no")
                if not list_no:
                    continue

                # ---- Step 2: fetch detail page ----
                paper = self._fetch_detail(list_no, basic_info)
                if paper:
                    self._save_paper(paper)
                    saved += 1
                    effective_label = f"{saved}/{limit}" if limit else str(saved)
                    print(f"[{self.site_id}] Saved {effective_label} papers...")

            page += 1

        print(f"[{self.site_id}] Done. Saved {saved} papers.")
        return saved

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _parse_list(self, soup: BeautifulSoup) -> list:
        """Extract list_no and basic metadata from the board list page."""
        items = []

        # The board table typically has rows with links containing list_no
        rows = soup.select("table tbody tr")
        for row in rows:
            link = row.find("a", href=True)
            if not link:
                continue

            href = link["href"]
            list_no_match = re.search(r"list_no=(\d+)", href)
            if not list_no_match:
                # Also check for onclick or data attributes
                onclick = link.get("onclick", "")
                list_no_match = re.search(r"list_no['\"]?\s*[=:]\s*['\"]?(\d+)", onclick)
            if not list_no_match:
                continue

            list_no = list_no_match.group(1)

            # Try to extract title, category, date from table cells
            cells = row.find_all("td")
            title = link.get_text(strip=True)
            category = cells[1].get_text(strip=True) if len(cells) > 1 else ""
            date_text = ""
            for cell in cells:
                text = cell.get_text(strip=True)
                if re.match(r"\d{4}[-./]\d{2}[-./]\d{2}", text):
                    date_text = text
                    break

            items.append({
                "list_no": list_no,
                "title": title,
                "category": category,
                "published_date": date_text,
            })

        return items

    # ------------------------------------------------------------------
    # Detail-page parsing
    # ------------------------------------------------------------------

    def _fetch_detail(self, list_no: str, basic_info: dict) -> dict | None:
        """Fetch the detail page and return a normalised paper dict."""
        params = {**self._VIEW_PARAMS, "list_no": list_no}
        view_url = (
            f"{self._VIEW_URL}?mid={self._VIEW_PARAMS['mid']}"
            f"&bid={self._VIEW_PARAMS['bid']}&act=view&list_no={list_no}"
        )
        response = self._request(self._VIEW_URL, params=params)
        if response is None:
            return None

        soup = BeautifulSoup(response.text, "html.parser")

        # Title - multiple h2.title exist on the page; the actual article
        # title is the last one (earlier ones are UI labels like "정보", "공유하기").
        title_tags = soup.find_all("h2", class_="title")
        title = ""
        for tag in reversed(title_tags):
            text = tag.get_text(strip=True)
            if text and text not in ("정보", "공유하기"):
                title = text
                break
        if not title:
            title = basic_info.get("title", "")

        # Date
        date_tag = soup.find("li", class_="date")
        published_date = basic_info.get("published_date", "")
        if date_tag:
            span = date_tag.find("span")
            if span:
                published_date = span.get_text(strip=True)

        # Category / department
        category = basic_info.get("category", "")
        department = ""
        dept_tag = soup.find("li", class_="department") or soup.find("li", string=re.compile("담당"))
        if dept_tag:
            department = dept_tag.get_text(strip=True)

        # Abstract / body text
        abstract = ""
        content_tag = soup.find("div", class_="view_con") or soup.find("div", class_="board_view")
        if content_tag:
            abstract = content_tag.get_text(separator=" ", strip=True)

        # PDF attachments
        pdf_urls = []
        for a_tag in soup.find_all("a", href=re.compile(r"/boardDownload\.es")):
            href = a_tag["href"]
            if not href.startswith("http"):
                href = self.base_url + href
            pdf_urls.append(href)

        pdf_url = pdf_urls[0] if pdf_urls else ""

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": list_no,
            "title": title,
            "authors": json.dumps([], ensure_ascii=False),
            "abstract": abstract,
            "category": category,
            "keywords": json.dumps([], ensure_ascii=False),
            "published_date": published_date,
            "url": view_url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": department,
            "metadata": json.dumps(
                {"all_pdf_urls": pdf_urls}, ensure_ascii=False
            ),
        }

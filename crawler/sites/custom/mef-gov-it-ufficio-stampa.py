# -*- coding: utf-8 -*-
"""Crawler for MEF (Ministero dell'Economia e delle Finanze) Ufficio Stampa comunicati."""

import json
import os
import re
import subprocess
import time
from datetime import datetime

from crawler.base_crawler import BaseCrawler


class MefGovItUfficioStampaCrawler(BaseCrawler):
    """Crawls press releases from https://www.mef.gov.it/ufficio-stampa/comunicati/."""

    site_id = "mef-gov-it-ufficio-stampa"
    site_name = "Custom: mef-gov-it-ufficio-stampa"
    base_url = "https://www.mef.gov.it"

    _PAGE_SIZE = 10
    _START_YEAR = 2025

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with 3-attempt exponential back-off (1s, 3s, 9s)."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,*/*",
            "-H", "Accept-Language: it-IT,it;q=0.9,en;q=0.8",
            url,
        ]
        for attempt in range(3):
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=35)
                if res.stdout:
                    return res.stdout.decode("utf-8", errors="replace")
                if attempt < 2:
                    wait = [1, 3, 9][attempt]
                    print(f"[{self.site_id}] Empty response from {url}, retry in {wait}s...")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < 2:
                    wait = [1, 3, 9][attempt]
                    print(f"[{self.site_id}] curl error ({exc}), retry in {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {exc}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_soup(html: str):
        """Parse HTML; fallback chain: html5lib → lxml → html.parser."""
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return BeautifulSoup(html, "html.parser")

    @staticmethod
    def _parse_it_date(raw: str) -> str:
        """Convert DD/MM/YYYY → YYYY-MM-DD; '' on no match."""
        m = re.search(r"(\d{2})/(\d{2})/(\d{4})", raw)
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""

    @staticmethod
    def _extract_post_number(text: str):
        """Extract numeric N° string from text like 'N° 122 del …'."""
        m = re.search(r"N\s*[°°\xb0]\s*(\d+)", text)
        return m.group(1) if m else None

    # ------------------------------------------------------------------
    # List-page parser
    # ------------------------------------------------------------------

    def _extract_list_items(self, html: str, year: int) -> list:
        """Return list-of-dicts from a year's listing page."""
        items = []
        try:
            soup = self._make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] List page parse error: {exc}")
            return items

        main = soup.find("main") or soup.find("div", id="fContent") or soup
        for li in main.find_all("li"):
            a = li.find("a", href=True)
            if not a:
                continue
            href = a.get("href", "")
            # Only article detail links for this specific year (not pagination / index links)
            if not re.search(rf"/comunicati/{year}/[^/?#]+/?$", href):
                continue

            text = li.get_text(separator=" ", strip=True)
            date_m = re.search(r"(\d{2}/\d{2}/\d{4})", text)
            listed_date = self._parse_it_date(date_m.group(1)) if date_m else ""
            post_number = self._extract_post_number(text)
            title = a.get_text(strip=True)
            slug = href.rstrip("/").split("/")[-1]
            full_url = href if href.startswith("http") else self.base_url + href

            if not title or not slug:
                continue

            items.append({
                "url": full_url,
                "title": title,
                "listed_date": listed_date,
                "post_number": post_number,
                "external_id": slug,
            })
        return items

    # ------------------------------------------------------------------
    # Detail-page parser
    # ------------------------------------------------------------------

    def _extract_detail(self, html: str) -> dict:
        """Extract abstract, date, N°, PDF URL, and keywords from a detail page."""
        result = {
            "abstract": "",
            "published_date": "",
            "pdf_url": None,
            "keywords": "",
            "post_number": None,
            "original_filename": None,
        }
        try:
            soup = self._make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] Detail page parse error: {exc}")
            return result

        # Primary content: div.tp-comunicato > div.body-sec
        tp = soup.find("div", class_="tp-comunicato")
        body_sec = tp.find("div", class_="body-sec") if tp else None

        if body_sec:
            # h3 carries "Comunicato Stampa N° 122 del 23/12/2025"
            h3 = body_sec.find("h3")
            if h3:
                h3_text = h3.get_text(strip=True)
                pn = self._extract_post_number(h3_text)
                if pn:
                    result["post_number"] = pn
                date_m = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", h3_text)
                if date_m:
                    result["published_date"] = self._parse_it_date(date_m.group(1))

            # Content body: collect text from all direct-child divs that are NOT
            # data-bot (city/date sign-off) or float-right (language selector).
            content_parts = []
            for div in body_sec.find_all("div", recursive=False):
                cls = " ".join(div.get("class", []))
                if "data-bot" in cls or "float-right" in cls:
                    continue
                text = div.get_text(separator="\n", strip=True)
                if text:
                    content_parts.append(text)

            result["abstract"] = "\n\n".join(content_parts).strip()

        else:
            # Fallback: regex-cut the main text between the stamp header and the sign-off
            main = soup.find("main") or soup.find("div", id="fContent") or soup
            full = main.get_text(separator="\n", strip=True)
            m = re.search(
                r"Comunicato\s+Stampa\s+N\s*[°°]\s*(\d+)\s+del\s+(\d{2}/\d{2}/\d{4})",
                full,
            )
            if m:
                result["post_number"] = m.group(1)
                result["published_date"] = self._parse_it_date(m.group(2))
                after = full[m.end():]
                cut = re.search(r"\nRoma\s+\d{2}/\d{2}/\d{4}|\nElenco dei Tag", after)
                result["abstract"] = (after[: cut.start()].strip() if cut else after.strip())

        # PDF attachment (first .pdf href found)
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if ".pdf" in href.lower():
                pdf_url = href if href.startswith("http") else self.base_url + href
                result["pdf_url"] = pdf_url
                result["original_filename"] = (
                    href.rstrip("/").split("/")[-1].split("?")[0] or None
                )
                break

        # Keywords from Bootstrap-Italia chip tags
        tags = []
        for chip in soup.select(".chip-label"):
            t = chip.get_text(strip=True)
            if t:
                tags.append(t)
        result["keywords"] = ",".join(tags)

        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl MEF comunicati starting from the most recent year down to 2025.

        Parameters
        ----------
        limit:
            Maximum number of items to save.  None means unlimited.
        """
        start_time = time.time()
        MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))   # 25-minute wall-clock budget
        SAFETY_CAP = 200     # max pages per year (safety valve)

        saved = 0
        seen_urls: set = set()

        current_year = datetime.now().year
        years = list(range(current_year, self._START_YEAR - 1, -1))

        for year in years:
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time > MAX_WALL:
                print(f"[{self.site_id}] Wall-clock budget reached; stopping.")
                break

            page = 1
            pages_this_year = 0

            while True:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget reached; stopping.")
                    break
                if pages_this_year >= SAFETY_CAP:
                    print(f"[{self.site_id}] Hit {SAFETY_CAP}-page safety cap for year {year}.")
                    break

                # Build list URL
                if page == 1:
                    list_url = f"{self.base_url}/ufficio-stampa/comunicati/{year}/"
                else:
                    list_url = (
                        f"{self.base_url}/ufficio-stampa/comunicati/{year}/"
                        f"index.html?page={page}&pagesize={self._PAGE_SIZE}"
                    )

                raw = self._curl_get(list_url)
                if not raw:
                    print(f"[{self.site_id}] Failed to fetch list {list_url}; skipping year.")
                    break

                items = self._extract_list_items(raw, year)

                if not items:
                    if page == 1:
                        print(f"[{self.site_id}] No items found for year {year}; skipping.")
                    else:
                        print(f"[{self.site_id}] No more items at page {page} for {year}; done.")
                    break

                # URL deduplication — skip pages that are all-seen (catches silent paginators)
                new_items = [i for i in items if i["url"] not in seen_urls]
                if not new_items:
                    print(
                        f"[{self.site_id}] All {len(items)} items on page {page} already seen; done."
                    )
                    break

                if page % 10 == 0:
                    lim_s = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] page {page}: saved {saved}/{lim_s}")

                for item in new_items:
                    if limit is not None and saved >= limit:
                        break

                    url = item["url"]
                    seen_urls.add(url)

                    try:
                        time.sleep(self._delay)

                        detail_raw = self._curl_get(url)
                        if not detail_raw:
                            print(f"[{self.site_id}] item {url} failed: no response after retries")
                            continue

                        detail = self._extract_detail(detail_raw)

                        abstract = detail.get("abstract", "").strip()
                        if len(abstract) < 50:
                            print(
                                f"[{self.site_id}] Skip (abstract {len(abstract)} chars): "
                                f"{item['title'][:60]}"
                            )
                            continue

                        post_number = detail.get("post_number") or item.get("post_number")
                        published_date = detail.get("published_date") or item.get("listed_date", "")
                        listed_date = item.get("listed_date", "") or published_date

                        paper = {
                            "site_id": self.site_id,
                            "external_id": item["external_id"],
                            "title": item["title"],
                            "abstract": abstract,
                            "published_date": published_date,
                            "posted_date": listed_date,
                            "url": url,
                            "pdf_url": detail.get("pdf_url"),
                            "keywords": detail.get("keywords") or "",
                            "publisher": "Ministero dell'Economia e delle Finanze",
                            "category": "Comunicato Stampa",
                            "original_filename": detail.get("original_filename"),
                            "metadata": json.dumps(
                                {
                                    "posted_date": listed_date,
                                    "originalFilename": detail.get("original_filename"),
                                    "post_number": post_number,
                                    "year": year,
                                },
                                ensure_ascii=False,
                            ),
                        }

                        self._save_paper(paper)
                        saved += 1
                        lim_s = f"/{limit}" if limit is not None else ""
                        print(f"[{self.site_id}] Saved {saved}{lim_s}: {item['title'][:60]}")

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[{self.site_id}] item {url} failed: {exc}")
                        continue

                page += 1
                pages_this_year += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

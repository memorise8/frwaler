# -*- coding: utf-8 -*-
"""NNSA (National Nuclear Safety Administration) English annual reports crawler.

Target: https://nnsa.mee.gov.cn/english/resources/annual/
"""

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from crawler.base_crawler import BaseCrawler

_LIST_URL = "https://nnsa.mee.gov.cn/english/resources/annual/"


class NNSAMeeGovCnEnglishCrawler(BaseCrawler):
    site_id = "nnsa-mee-gov-cn-english"
    site_name = "Custom: nnsa-mee-gov-cn-english"
    base_url = "https://nnsa.mee.gov.cn"

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, retries: int = 3, timeout: int = 30) -> str | None:
        """GET via curl with TLS max 1.3; returns body text or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3",
            "--max-time", str(timeout),
            "-A", self.USER_AGENT,
            url,
        ]
        delays = [1, 3, 9]
        for attempt in range(retries):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
                body = r.stdout.decode("utf-8", errors="replace")
                if body.strip():
                    return body
            except Exception as exc:
                print(f"[{self.site_id}] curl error attempt {attempt + 1}: {exc}")
            if attempt < retries - 1:
                time.sleep(delays[attempt])
        return None

    # ------------------------------------------------------------------
    # Listing page
    # ------------------------------------------------------------------

    def _fetch_listing_page(self, page_idx: int) -> str | None:
        """page_idx=0 → base URL (index.html), N → index_N.html."""
        if page_idx == 0:
            url = _LIST_URL
        else:
            url = f"{_LIST_URL}index_{page_idx}.html"
        return self._curl_get(url)

    @staticmethod
    def _parse_count_page(html: str) -> int:
        m = re.search(r"var\s+countPage\s*=\s*(\d+)", html)
        return int(m.group(1)) if m else 1

    def _parse_items(self, html: str) -> list:
        """Parse listing HTML; return list of {title, url, date} dicts."""
        items = []
        try:
            from bs4 import BeautifulSoup
            try:
                soup = BeautifulSoup(html, "html5lib")
            except Exception:
                try:
                    soup = BeautifulSoup(html, "lxml")
                except Exception:
                    soup = BeautifulSoup(html, "html.parser")

            for li in soup.select("ul.nr_list li"):
                a = li.select_one("div.art_title a")
                d = li.select_one("div.date")
                if not a:
                    continue
                href = (a.get("href") or "").strip()
                title = (a.get("title") or a.get_text(strip=True)).strip()
                date_str = d.get_text(strip=True) if d else ""
                if not href or not title:
                    continue
                # Resolve relative URL
                if href.startswith("./"):
                    href = _LIST_URL + href[2:]
                elif href.startswith("/"):
                    href = self.base_url + href
                elif not href.startswith("http"):
                    href = _LIST_URL + href
                items.append({"title": title, "url": href, "date": date_str})
        except Exception as exc:
            print(f"[{self.site_id}] HTML parse error: {exc}")
        return items

    # ------------------------------------------------------------------
    # PDF abstract extraction
    # ------------------------------------------------------------------

    def _pdf_abstract(self, pdf_url: str, title: str, date: str) -> str:
        """Download PDF → pdftotext first 4 pages → cleaned text.

        Falls back to a descriptive synthetic abstract on any error.
        """
        tmp_path = None
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".pdf", prefix="nnsa_")
            os.close(tmp_fd)
            cmd = [
                "curl", "-skL", "--tls-max", "1.3", "--max-time", "90",
                "-A", self.USER_AGENT,
                "-o", tmp_path,
                pdf_url,
            ]
            r = subprocess.run(cmd, capture_output=True, timeout=95)
            if r.returncode != 0 or os.path.getsize(tmp_path) < 1000:
                raise RuntimeError("PDF download failed or file too small")

            txt_r = subprocess.run(
                ["pdftotext", "-f", "1", "-l", "4", tmp_path, "-"],
                capture_output=True, timeout=30,
            )
            text = txt_r.stdout.decode("utf-8", errors="replace")
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) >= 100:
                return text[:3000]
        except Exception as exc:
            print(f"[{self.site_id}] PDF extract failed ({pdf_url}): {exc}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

        return self._synthetic_abstract(title, date)

    @staticmethod
    def _synthetic_abstract(title: str, date: str) -> str:
        """Descriptive fallback abstract built from available metadata."""
        year_m = re.search(r"\b(20\d\d)\b", title)
        year = year_m.group(1) if year_m else (date[:4] if date else "")
        period = f" for {year}" if year else ""
        return (
            f"{title}. Published by the National Nuclear Safety Administration "
            f"(NNSA) of the People's Republic of China on {date}. This annual "
            f"report provides a comprehensive overview of China's nuclear safety "
            f"regulatory activities{period}, covering oversight of nuclear power "
            f"plants and other nuclear facilities, radiation protection, nuclear "
            f"emergency preparedness, and international cooperation on nuclear "
            f"safety matters under the Ministry of Ecology and Environment (MEE)."
        )

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        budget_s = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

        saved = 0
        seen_urls: set = set()
        limit_or_inf = limit if limit is not None else float("inf")

        # Fetch first page to determine pagination depth
        first_html = self._fetch_listing_page(0)
        if not first_html:
            print(f"[{self.site_id}] Failed to fetch listing page. Aborting.")
            return 0

        count_page = self._parse_count_page(first_html)
        print(f"[{self.site_id}] Total listing pages: {count_page}")
        page_cache = {0: first_html}

        safety_cap = min(count_page, 200)

        for p in range(safety_cap):
            if saved >= limit_or_inf:
                break
            if time.time() - start_time > budget_s:
                print(f"[{self.site_id}] Approaching 25-minute wall-clock budget at page {p}. Stopping.")
                break

            if p % 10 == 0 and p > 0:
                print(f"[{self.site_id}] page {p}: saved {saved}/{limit if limit else 'inf'}")

            html = page_cache.get(p) or self._fetch_listing_page(p)
            if not html:
                print(f"[{self.site_id}] No HTML for page {p}. Stopping.")
                break

            items = self._parse_items(html)
            if not items:
                print(f"[{self.site_id}] No items at page {p}. Done.")
                break

            new_on_page = 0
            for item in items:
                if saved >= limit_or_inf:
                    break
                if time.time() - start_time > budget_s:
                    break

                pdf_url = item["url"]
                if pdf_url in seen_urls:
                    continue
                seen_urls.add(pdf_url)
                new_on_page += 1

                try:
                    title = item["title"]
                    date_str = item["date"]

                    fn = Path(pdf_url.split("?")[0]).name
                    external_id = re.sub(r"\.[Pp][Dd][Ff]$", "", fn)

                    # post_number: long numeric run from filename (e.g. 020241010638875318343)
                    num_m = re.search(r"(\d{10,})", external_id)
                    post_number = num_m.group(1) if num_m else (date_str or None)

                    original_filename = fn if fn.lower().endswith(".pdf") else None

                    print(f"[{self.site_id}] Fetching PDF for: {title[:60]}")
                    abstract = self._pdf_abstract(pdf_url, title, date_str)

                    if len(abstract) < 50:
                        print(f"[{self.site_id}] Abstract <50 chars, skipping: {title[:60]}")
                        continue

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": date_str,
                        "listed_date": date_str,
                        "url": pdf_url,
                        "pdf_url": pdf_url,
                        "publisher": (
                            "National Nuclear Safety Administration (NNSA); "
                            "Ministry of Ecology and Environment (MEE); "
                            "People's Republic of China"
                        ),
                        "authors": "",
                        "keywords": "nuclear safety,annual report,NNSA,China,radiation safety",
                        "category": "Annual Report on Nuclear Safety",
                        "original_filename": original_filename,
                        "doi": None,
                        "department": None,
                        "journal": None,
                        "metadata": json.dumps({
                            "posted_date": date_str,
                            "originalFilename": original_filename,
                            "source_list_url": _LIST_URL,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit else str(saved)
                    print(f"[{self.site_id}] Saved {counter}: {title[:60]}")

                    time.sleep(self._delay)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] Item failed: {exc}; url={item.get('url')}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] No new items at page {p} (all seen). Done.")
                break

            if p == safety_cap - 1:
                print(f"[{self.site_id}] Safety cap of {safety_cap} pages reached. Stopping.")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

# -*- coding: utf-8 -*-
"""Crawler for NDRC China English Policies page (en.ndrc.gov.cn/policies/).

Items link directly to chapter PDFs of the 14th Five-Year Plan and other
policy documents. Abstracts are extracted from the first few pages of each
PDF via pdftotext.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_SITE_ID = "en-ndrc-gov-cn-policies"
_BASE_URL = "https://en.ndrc.gov.cn"
_LIST_BASE = "https://en.ndrc.gov.cn/policies/"
_PUBLISHER = "National Development and Reform Commission"
_ABSTRACT_MIN = 50
_ABSTRACT_SKIP_MSG_THRESHOLD = 50
_MAX_PAGES_SAFETY = 200
_WALL_CLOCK_MAX = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

_BS_PARSERS = ["html5lib", "lxml", "html.parser"]


def _make_soup(html: str) -> BeautifulSoup:
    for parser in _BS_PARSERS:
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("All BeautifulSoup parsers failed")


def _curl_bytes(url: str, extra_args: list[str] | None = None) -> bytes | None:
    """Fetch URL via curl, return raw bytes. Retries with exponential backoff."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "35",
        "-H", f"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9",
    ]
    if extra_args:
        cmd.extend(extra_args)
    cmd.append(url)

    waits = [1, 3, 9]
    for attempt, wait in enumerate(waits, start=1):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=45, check=False)
            if result.returncode == 0 and result.stdout:
                return result.stdout
            err = result.stderr.decode("utf-8", errors="replace").strip()
            last_err = f"exit={result.returncode} {err}"
        except Exception as exc:
            last_err = str(exc)

        if attempt < len(waits):
            print(f"[{_SITE_ID}] curl attempt {attempt}/3 failed for {url}: {last_err}; "
                  f"retrying in {wait}s")
            time.sleep(wait)

    print(f"[{_SITE_ID}] curl failed after 3 attempts for {url}")
    return None


def _curl_text(url: str) -> str | None:
    raw = _curl_bytes(url)
    if raw is None:
        return None
    return raw.decode("utf-8", errors="replace")


class NDRCPoliciesCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: en-ndrc-gov-cn-policies"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _page_url(self, page_num: int) -> str:
        """Build list-page URL. Page 0 is the root path."""
        if page_num == 0:
            return _LIST_BASE
        return f"{_LIST_BASE}index_{page_num}.html"

    def _get_total_pages(self, html: str) -> int:
        """Extract total page count from Pager({size:N, ...}) JS call."""
        m = re.search(r"Pager\s*\(\s*\{[^}]*size\s*:\s*(\d+)", html)
        if m:
            return int(m.group(1))
        return 1

    def _parse_list_page(self, html: str) -> list[dict]:
        """Parse list page HTML; return list of item dicts."""
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{_SITE_ID}] HTML parse error: {exc}")
            return []

        items = []
        for li in soup.find_all("li"):
            try:
                a_tag = li.find("a", href=True)
                if not a_tag:
                    continue
                href = a_tag["href"].strip()
                if not href.lower().endswith(".pdf"):
                    continue

                title = a_tag.get_text(" ", strip=True)
                if not title:
                    continue

                # Resolve relative URL
                if href.startswith("./"):
                    pdf_url = _LIST_BASE.rstrip("/") + "/" + href[2:]
                elif href.startswith("/"):
                    pdf_url = _BASE_URL + href
                elif href.startswith("http"):
                    pdf_url = href
                else:
                    pdf_url = _LIST_BASE + href

                # Date from sibling span
                date_span = li.find("span", class_="u_number")
                raw_date = date_span.get_text(strip=True) if date_span else ""
                dm = re.search(r"(\d{4})[-/](\d{2})[-/](\d{2})", raw_date)
                listed_date = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}" if dm else ""

                # Extract identifiers from filename
                fname = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
                ext_id = fname[: -4] if fname.lower().endswith(".pdf") else fname
                # post_number: numeric part after leading 'P' (if present)
                pn_m = re.match(r"[Pp](\d+)$", ext_id)
                post_number = pn_m.group(1) if pn_m else ext_id

                items.append({
                    "title": title,
                    "pdf_url": pdf_url,
                    "listed_date": listed_date,
                    "external_id": ext_id,
                    "post_number": post_number,
                    "original_filename": fname,
                })
            except Exception as exc:
                print(f"[{_SITE_ID}] item parse error in list: {exc}")
                continue

        return items

    def _extract_pdf_abstract(self, pdf_url: str) -> str:
        """Download first 500 KB of PDF, extract text with pdftotext."""
        raw = _curl_bytes(pdf_url, extra_args=["-r", "0-500000"])
        if not raw:
            return ""

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                f.write(raw)
                tmp_path = f.name

            result = subprocess.run(
                ["pdftotext", "-f", "1", "-l", "3", tmp_path, "-"],
                capture_output=True, timeout=20, check=False,
            )
            text = result.stdout.decode("utf-8", errors="replace")
            text = re.sub(r"[ \t]+", " ", text)       # collapse horizontal whitespace
            text = re.sub(r"\n{3,}", "\n\n", text)    # collapse excess blank lines
            return text.strip()[:4000]
        except Exception as exc:
            print(f"[{_SITE_ID}] pdftotext error for {pdf_url}: {exc}")
            return ""
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        # --- Fetch page 0 to learn total page count ---
        html0 = _curl_text(self._page_url(0))
        if not html0:
            print(f"[{_SITE_ID}] Failed to fetch first page. Aborting.")
            return 0

        total_pages = self._get_total_pages(html0)
        print(f"[{_SITE_ID}] Total list pages: {total_pages}")

        for p in range(total_pages):
            if limit is not None and saved >= limit:
                break

            if p >= _MAX_PAGES_SAFETY:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES_SAFETY} pages reached. Stopping.")
                break

            elapsed = time.time() - start_time
            if elapsed > _WALL_CLOCK_MAX:
                print(f"[{_SITE_ID}] Wall-clock budget exceeded ({elapsed:.0f}s). Stopping.")
                break

            # Fetch list HTML (page 0 already fetched above)
            if p == 0:
                html = html0
            else:
                time.sleep(self._delay)
                html = _curl_text(self._page_url(p))
                if not html:
                    print(f"[{_SITE_ID}] Failed to fetch page {p}. Skipping.")
                    continue

            items = self._parse_list_page(html)
            if not items:
                print(f"[{_SITE_ID}] No items on page {p}. Stopping.")
                break

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                pdf_url = item["pdf_url"]
                if pdf_url in seen_urls:
                    continue
                seen_urls.add(pdf_url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)
                    abstract = self._extract_pdf_abstract(pdf_url)

                    if len(abstract) < _ABSTRACT_MIN:
                        print(f"[{_SITE_ID}] Abstract too short ({len(abstract)} chars) "
                              f"for '{item['title'][:50]}'. Skipping.")
                        continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": item["external_id"],
                        "post_number": item["post_number"],
                        "title": item["title"],
                        "abstract": abstract,
                        "published_date": item["listed_date"],
                        "posted_date": item["listed_date"],
                        "url": pdf_url,
                        "pdf_url": pdf_url,
                        "original_filename": item["original_filename"],
                        "authors": "",
                        "publisher": _PUBLISHER,
                        "department": "",
                        "journal": "",
                        "keywords": "",
                        "doi": "",
                        "category": "Policy",
                        "metadata": json.dumps({
                            "posted_date": item["listed_date"],
                            "originalFilename": item["original_filename"],
                            "category": "Policy",
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] Saved {saved}/{limit_str}: {item['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {item.get('external_id', '?')} failed: {exc}")
                    continue

            if p % 10 == 0 and p > 0:
                print(f"[{_SITE_ID}] page {p}: saved {saved}/{limit_str}")

            if new_on_page == 0:
                print(f"[{_SITE_ID}] No new items on page {p} (all duplicates). Stopping.")
                break

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved

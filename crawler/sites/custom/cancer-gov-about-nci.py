# -*- coding: utf-8 -*-
"""NCI Annual Plan & Professional Judgment Budget Proposal crawler.

Target: https://www.cancer.gov/about-nci/budget/about-budget-proposal
Each annual plan PDF entry is one record.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from crawler.base_crawler import BaseCrawler

_BASE_URL = "https://www.cancer.gov"
_LANDING_URL = f"{_BASE_URL}/about-nci/budget/about-budget-proposal"

_INTRO_TEXT = (
    "Each fiscal year, NCI is required to prepare for the President and Congress "
    "its best professional judgment on the optimum funding needed to make the most "
    "rapid progress against cancer. The cancer research community—under NCI's "
    "leadership—is on the verge of pivotal advances in oncology. Additional funding "
    "is needed to pursue promising research opportunities that will improve our "
    "understanding of cancer and bring the benefits of cancer research to the public."
)

_BS_PARSERS = ["html5lib", "lxml", "html.parser"]


def _make_soup(html: str):
    from bs4 import BeautifulSoup
    for parser in _BS_PARSERS:
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _curl_get(url: str, retries: int = 3) -> str | None:
    cmd = ["curl", "--tls-max", "1.3", "-sk", "-L", "--max-time", "30", url]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            raw = result.stdout
            if raw:
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return raw.decode("utf-8", errors="replace")
        except Exception as exc:
            wait = (attempt + 1) ** 2
            if attempt < retries - 1:
                print(f"[cancer-gov-about-nci] curl error: {exc}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f"[cancer-gov-about-nci] curl failed after {retries} attempts: {exc}")
    return None


def _fy_to_year(fy_label: str) -> str | None:
    """Convert 'FY27' / 'FY2026' / '2026' → 4-digit year string."""
    m = re.search(r'FY\s*(\d{2,4})', fy_label, re.IGNORECASE)
    if m:
        yr = m.group(1)
        if len(yr) == 2:
            n = int(yr)
            return str(2000 + n if n <= 29 else 1900 + n)
        return yr
    m = re.search(r'(\d{4})', fy_label)
    if m:
        return m.group(1)
    return None


def _slug_from_url(url: str) -> str:
    """Return the PDF filename without extension as a slug."""
    path = url.split("?")[0].split("#")[0]
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"\.pdf$", "", name, flags=re.IGNORECASE)


def _parse_entries(html: str) -> list[dict]:
    """Extract all annual-plan entries from the landing page HTML."""
    entries = []

    try:
        soup = _make_soup(html)
    except Exception as exc:
        print(f"[cancer-gov-about-nci] HTML parse error: {exc}")
        return entries

    if not soup:
        print("[cancer-gov-about-nci] Could not build soup.")
        return entries

    # -- 1. Current-year summary box (FY27 as of 2025) --
    summary_box = soup.find("div", class_="usa-summary-box")
    if summary_box:
        heading_tag = summary_box.find(class_="usa-summary-box__heading")
        body_tag = summary_box.find("div", class_="usa-prose")
        if heading_tag:
            title_text = heading_tag.get_text(strip=True)
            body_text = (body_tag.get_text(" ", strip=True) if body_tag else "").strip()
            # Find the main PDF link (no page anchor)
            pdf_link = None
            for a in summary_box.find_all("a", href=re.compile(r"\.pdf", re.I)):
                if "#page=" not in a.get("href", ""):
                    pdf_link = a
                    break
            if pdf_link:
                pdf_href = pdf_link["href"]
                if not pdf_href.startswith("http"):
                    pdf_href = _BASE_URL + pdf_href
                fy_year = _fy_to_year(title_text) or _fy_to_year(pdf_href)
                entries.append({
                    "title": title_text,
                    "pdf_url": pdf_href,
                    "highlights": [body_text] if body_text else [],
                    "fy_year": fy_year,
                })

    # -- 2. Archive list --
    archive_h2 = soup.find(
        lambda tag: tag.name in ("h2", "h3") and "Archive" in tag.get_text()
    )
    archive_ul = archive_h2.find_next("ul") if archive_h2 else None

    if archive_ul is None:
        # Fallback: first ul that has a pdf link
        for ul in soup.find_all("ul"):
            if ul.find("a", href=re.compile(r"\.pdf", re.I)):
                archive_ul = ul
                break

    if archive_ul:
        for li in archive_ul.find_all("li", recursive=False):
            # Main PDF link: first <a> whose href has .pdf and no #page=
            pdf_link = None
            for a in li.find_all("a", href=re.compile(r"\.pdf", re.I)):
                if "#page=" not in a.get("href", ""):
                    pdf_link = a
                    break
            if not pdf_link:
                continue

            title_text = pdf_link.get_text(strip=True)
            pdf_href = pdf_link["href"]
            if not pdf_href.startswith("http"):
                pdf_href = _BASE_URL + pdf_href

            fy_year = _fy_to_year(title_text) or _fy_to_year(pdf_href)

            # Highlights from nested sub-lists (skip "Highlighted Scientific Opportunities:" label)
            highlights = []
            for sub_li in li.find_all("li"):
                a = sub_li.find("a")
                hl = (a.get_text(strip=True) if a else sub_li.get_text(strip=True))
                if hl and "Highlighted Scientific" not in hl and hl not in highlights:
                    highlights.append(hl)

            entries.append({
                "title": title_text,
                "pdf_url": pdf_href,
                "highlights": highlights,
                "fy_year": fy_year,
            })

    return entries


class CancerGovAboutNciCrawler(BaseCrawler):
    site_id = "cancer-gov-about-nci"
    site_name = "Custom: cancer-gov-about-nci"
    base_url = "https://www.cancer.gov"

    def crawl(self, limit=None):
        start_time = time.time()
        max_wall_seconds = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
        saved = 0
        seen_urls: set[str] = set()
        page = 1
        max_pages = 200

        while page <= max_pages:
            if time.time() - start_time > max_wall_seconds:
                print(f"[cancer-gov-about-nci] 25-min wall budget exceeded. Exiting cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                limit_str = str(limit) if limit is not None else "inf"
                print(f"[cancer-gov-about-nci] page {page}: saved {saved}/{limit_str}")

            # All content is on the single landing page; stop after processing it.
            if page > 1:
                break

            try:
                raw = _curl_get(_LANDING_URL)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[cancer-gov-about-nci] Fetch error: {exc}. Aborting.")
                break

            if not raw:
                print("[cancer-gov-about-nci] Empty response from landing page. Aborting.")
                break

            # Extract page-level updated date from <time datetime="...">
            article_date = None
            article_date_raw = None
            article_datetime_raw = None
            try:
                m = re.search(r'<time[^>]+datetime="(\d{4}-\d{2}-\d{2})', raw)
                if m:
                    article_date = m.group(1)
                time_node = _make_soup(raw).find("time")
                if time_node:
                    article_date_raw = time_node.get_text(" ", strip=True)
                    article_datetime_raw = time_node.get("datetime")
            except Exception:
                pass

            try:
                entries = _parse_entries(raw)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[cancer-gov-about-nci] Parse error: {exc}. Aborting.")
                break

            if not entries:
                print("[cancer-gov-about-nci] No entries parsed. Stopping.")
                break

            for entry in entries:
                if limit is not None and saved >= limit:
                    break

                try:
                    pdf_url = entry["pdf_url"]
                    dedup_key = pdf_url.split("#")[0].lower()
                    if dedup_key in seen_urls:
                        continue
                    seen_urls.add(dedup_key)

                    title = entry["title"]
                    fy_year = entry.get("fy_year")
                    highlights = entry.get("highlights") or []

                    # Compose abstract — guaranteed >= 100 chars via intro text (350+ chars)
                    parts = [title, _INTRO_TEXT]
                    if highlights:
                        parts.append("Highlighted scientific opportunities: " + "; ".join(highlights))
                    abstract = " ".join(parts)

                    if len(abstract) < 50:
                        print(f"[cancer-gov-about-nci] Abstract too short for '{title}', skipping.")
                        continue

                    original_filename = pdf_url.split("#")[0].rstrip("/").rsplit("/", 1)[-1]
                    external_id = _slug_from_url(pdf_url)

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": fy_year,
                        "title": title,
                        "abstract": abstract,
                        # A budget fiscal year is a coverage period. The
                        # catalogue's Updated label belongs to the web page.
                        # Neither establishes a document publication date.
                        "published_date": None,
                        "listed_date": None,
                        "authors": "",
                        "publisher": "National Cancer Institute",
                        "department": "National Cancer Institute",
                        "journal": None,
                        "url": _LANDING_URL,
                        "pdf_url": pdf_url.split("#")[0],
                        "keywords": "NCI,Annual Plan,Budget Proposal,Professional Judgment,Cancer Research,Fiscal Year",
                        "category": "Budget & Appropriations",
                        "doi": None,
                        "original_filename": original_filename,
                        "metadata": json.dumps({
                            "fy_year": fy_year,
                            "highlights": highlights,
                            "posted_date": None,
                            "fiscal_year_role": "budget_coverage_period",
                            "landing_updated_date": article_date,
                            "landing_updated_date_raw": article_date_raw,
                            "landing_updated_datetime_raw": article_datetime_raw,
                            "landing_updated_date_role": "catalogue_page_update",
                            "originalFilename": original_filename,
                            "landing_url": _LANDING_URL,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    if self._last_save_created:
                        saved += 1
                    limit_str = str(limit) if limit is not None else "inf"
                    print(f"[cancer-gov-about-nci] Saved {saved}/{limit_str}: {title[:70]}")
                    time.sleep(0.1)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[cancer-gov-about-nci] Item '{entry.get('title', '?')}' failed: {exc}; continuing.")
                    continue

            page += 1

        if page > max_pages:
            print(f"[cancer-gov-about-nci] Safety cap of {max_pages} pages reached. Stopping.")

        print(f"[cancer-gov-about-nci] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Pure enumeration contract (L3b-20261018). crawl() is unchanged; these
    # methods only parse the already fetched landing page (no network).
    # ------------------------------------------------------------------

    def _enumeration_items(self, html, url=None):
        """Entries of the single landing page in crawl() order, each carrying the
        page-level Updated date crawl() reads (same regex/soup code). Repeated PDF
        links are dropped exactly as crawl()'s dedup_key does."""
        from crawler.enumeration.http import PermanentFailure
        if not isinstance(html, str):
            raise PermanentFailure("listing_parse_error:expected_html")
        raw = html

        article_date = None
        article_date_raw = None
        article_datetime_raw = None
        try:
            m = re.search(r'<time[^>]+datetime="(\d{4}-\d{2}-\d{2})', raw)
            if m:
                article_date = m.group(1)
            time_node = _make_soup(raw).find("time")
            if time_node:
                article_date_raw = time_node.get_text(" ", strip=True)
                article_datetime_raw = time_node.get("datetime")
        except Exception:
            pass

        rows = []
        seen_urls = set()
        for entry in _parse_entries(raw):
            pdf_url = entry["pdf_url"]
            dedup_key = pdf_url.split("#")[0].lower()
            if dedup_key in seen_urls:
                continue
            seen_urls.add(dedup_key)
            rows.append(dict(entry, external_id=_slug_from_url(pdf_url),
                             article_date=article_date, article_date_raw=article_date_raw,
                             article_datetime_raw=article_datetime_raw))
        return rows

    def _enumeration_paper(self, item):
        """Paper from one landing-page entry: crawl()'s per-entry body copied
        verbatim (external_id = PDF file slug, post_number = fiscal year)."""
        entry = item
        article_date = item.get("article_date")
        article_date_raw = item.get("article_date_raw")
        article_datetime_raw = item.get("article_datetime_raw")

        pdf_url = entry["pdf_url"]

        title = entry["title"]
        fy_year = entry.get("fy_year")
        highlights = entry.get("highlights") or []

        # Compose abstract — guaranteed >= 100 chars via intro text (350+ chars)
        parts = [title, _INTRO_TEXT]
        if highlights:
            parts.append("Highlighted scientific opportunities: " + "; ".join(highlights))
        abstract = " ".join(parts)

        if len(abstract) < 50:
            return None

        original_filename = pdf_url.split("#")[0].rstrip("/").rsplit("/", 1)[-1]
        external_id = _slug_from_url(pdf_url)

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": fy_year,
            "title": title,
            "abstract": abstract,
            # A budget fiscal year is a coverage period. The
            # catalogue's Updated label belongs to the web page.
            # Neither establishes a document publication date.
            "published_date": None,
            "listed_date": None,
            "authors": "",
            "publisher": "National Cancer Institute",
            "department": "National Cancer Institute",
            "journal": None,
            "url": _LANDING_URL,
            "pdf_url": pdf_url.split("#")[0],
            "keywords": "NCI,Annual Plan,Budget Proposal,Professional Judgment,Cancer Research,Fiscal Year",
            "category": "Budget & Appropriations",
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps({
                "fy_year": fy_year,
                "highlights": highlights,
                "posted_date": None,
                "fiscal_year_role": "budget_coverage_period",
                "landing_updated_date": article_date,
                "landing_updated_date_raw": article_date_raw,
                "landing_updated_datetime_raw": article_datetime_raw,
                "landing_updated_date_role": "catalogue_page_update",
                "originalFilename": original_filename,
                "landing_url": _LANDING_URL,
            }, ensure_ascii=False),
        }

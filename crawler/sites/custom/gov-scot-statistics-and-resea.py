# -*- coding: utf-8 -*-
"""Crawler for gov.scot Statistics and Research publications.

HTML-only (no JSON API). Paginates via ?page=N on the listing page.
10 items per page; 5,893 total publications as of 2026-05.
"""

import json
import re
import subprocess
import sys
import os
import time
from datetime import datetime
from urllib.parse import urljoin, unquote

# Absolute import required — spec_from_file_location has no package context
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler


_SITE_ID = "gov-scot-statistics-and-resea"
_BASE_URL = "https://www.gov.scot"
_LISTING_URL = "https://www.gov.scot/statistics-and-research/"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_CRAWL_BUDGET_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes


def _bs4(raw, parsers=("html5lib", "lxml", "html.parser")):
    """Parse HTML with fallback parsers: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in parsers:
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _curl_get(url, retries=3):
    """Fetch URL via curl with TLS 1.3 workaround and exponential-backoff retry."""
    backoffs = [1, 3, 9]
    for attempt in range(retries):
        try:
            result = subprocess.run(
                [
                    "curl", "--tls-max", "1.3", "-sk", "-L",
                    "--max-time", "30",
                    "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    url,
                ],
                capture_output=True,
                timeout=35,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(backoffs[attempt])
    return None


def _parse_date(raw):
    """Parse '14 May 2026' or ISO-8601 prefix to YYYY-MM-DD. Returns '' on failure."""
    if not raw:
        return ""
    raw = raw.strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return m.group(1)
    for fmt in ("%d %B %Y", "%B %Y"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d") if "%d" in fmt else dt.strftime("%Y-%m-01")
        except ValueError:
            continue
    return raw[:10] if len(raw) >= 10 else raw


class GovScotStatisticsCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: gov-scot-statistics-and-resea"
    base_url = _BASE_URL

    def crawl(self, limit=None):
        limit_val = limit if limit is not None else float("inf")
        saved = 0
        seen_urls = set()
        start_time = time.time()

        for page in range(1, _MAX_PAGES + 1):
            if time.time() - start_time > _CRAWL_BUDGET_SECS:
                print(f"[{_SITE_ID}] 25-min budget reached at page {page}, stopping.")
                break

            if saved >= limit_val:
                break

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_val}")

            url = (
                f"{_LISTING_URL}?page={page}"
                "&sort=date&type=research-and-analysis&type=statistics"
            )
            raw = _curl_get(url)
            if not raw:
                print(f"[{_SITE_ID}] page {page}: fetch failed, stopping.")
                break

            soup = _bs4(raw)
            if soup is None:
                print(f"[{_SITE_ID}] page {page}: parse failed, stopping.")
                break

            results = soup.select("li.ds_search-result")
            if not results:
                print(f"[{_SITE_ID}] page {page}: no results, end of pagination.")
                break

            new_items = []
            for item in results:
                link = item.select_one("a.ds_search-result__link")
                if not link:
                    continue
                href = link.get("href", "")
                if not href:
                    continue
                detail_url = urljoin(_BASE_URL, href)
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)

                summary_el = item.select_one("p.ds_search-result__summary")
                listing_summary = summary_el.get_text(strip=True) if summary_el else ""

                dds = item.select("dd.ds_metadata__value")
                if len(dds) >= 2:
                    format_type = dds[0].get_text(strip=True)
                    listed_date_raw = dds[1].get_text(strip=True)
                elif len(dds) == 1:
                    format_type = ""
                    listed_date_raw = dds[0].get_text(strip=True)
                else:
                    format_type = ""
                    listed_date_raw = ""

                new_items.append({
                    "detail_url": detail_url,
                    "listing_summary": listing_summary,
                    "listed_date_raw": listed_date_raw,
                    "format_type": format_type,
                })

            if not new_items:
                print(f"[{_SITE_ID}] page {page}: all items already seen (loop detected), stopping.")
                break

            for item_data in new_items:
                if saved >= limit_val:
                    break
                if time.time() - start_time > _CRAWL_BUDGET_SECS:
                    break
                try:
                    ok = self._process_item(item_data)
                    if ok:
                        saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {item_data.get('detail_url', '?')} failed: {exc}")
                    continue
                time.sleep(self._delay)

        print(f"[{_SITE_ID}] crawl complete: saved {saved} documents.")
        return saved

    def _process_item(self, item_data):
        detail_url = item_data["detail_url"]
        listing_summary = item_data["listing_summary"]
        listed_date_raw = item_data["listed_date_raw"]
        format_type = item_data["format_type"]

        slug = detail_url.rstrip("/").rsplit("/", 1)[-1]

        raw = _curl_get(detail_url)
        if not raw:
            print(f"[{_SITE_ID}] item {slug}: fetch failed, skipping.")
            return False

        soup = _bs4(raw)
        if soup is None:
            print(f"[{_SITE_ID}] item {slug}: parse failed, skipping.")
            return False

        # JSON-LD Article block — richest machine-readable source
        jsonld = {}
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                candidates = data if isinstance(data, list) else [data]
                for candidate in candidates:
                    if isinstance(candidate, dict) and candidate.get("@type") == "Article":
                        jsonld = candidate
                        break
                if jsonld:
                    break
            except Exception:
                continue

        # Title
        title_el = soup.select_one("h1.ds_page-header__title")
        title = title_el.get_text(strip=True) if title_el else jsonld.get("headline", "")
        if not title:
            print(f"[{_SITE_ID}] item {slug}: no title, skipping.")
            return False

        # Abstract: ds_leader → JSON-LD description → listing summary → body paragraphs
        abstract = ""
        leader_el = soup.select_one("p.ds_leader")
        if leader_el:
            abstract = leader_el.get_text(strip=True)
        if len(abstract) < 100:
            jdesc = jsonld.get("description", "")
            if jdesc and len(jdesc) > len(abstract):
                abstract = jdesc
        if len(abstract) < 100 and listing_summary and len(listing_summary) > len(abstract):
            abstract = listing_summary
        # Last resort: first few body paragraphs
        if len(abstract) < 100:
            parts = []
            for p_el in soup.select(
                "div.ds_cb__text p, "
                "div.ds_layout__content p, "
                "div.ds_layout p"
            )[:6]:
                t = p_el.get_text(strip=True)
                if t and t != abstract:
                    parts.append(t)
                    if len(" ".join(parts)) >= 150:
                        break
            if parts:
                candidate = " ".join(parts)
                if len(candidate) > len(abstract):
                    abstract = candidate

        if not abstract or len(abstract) < 50:
            print(
                f"[{_SITE_ID}] item {slug}: abstract too short "
                f"({len(abstract)} chars), skipping."
            )
            return False

        # Dates
        published_date = _parse_date(jsonld.get("datePublished", ""))
        listed_date = _parse_date(listed_date_raw)
        if not published_date:
            published_date = listed_date

        # Metadata dl key→value map
        meta_map = {}
        for dt_el in soup.select("dt.ds_metadata__key"):
            dd_el = dt_el.find_next_sibling("dd")
            if dd_el:
                k = dt_el.get_text(strip=True).lower().strip()
                meta_map[k] = dd_el.get_text(" ", strip=True)

        # Authors from "From" metadata entries
        from_roles = []
        for dt_el in soup.select("dt.ds_metadata__key"):
            if dt_el.get_text(strip=True).lower() == "from":
                dd_el = dt_el.find_next_sibling("dd")
                if dd_el:
                    links = dd_el.select("a")
                    if links:
                        from_roles += [a.get_text(strip=True) for a in links]
                    else:
                        txt = dd_el.get_text(strip=True)
                        if txt:
                            from_roles.append(txt)

        if from_roles:
            authors = "; ".join(r for r in from_roles if r)
        else:
            ja = jsonld.get("author", {})
            if isinstance(ja, dict):
                authors = ja.get("name", "The Scottish Government")
            elif isinstance(ja, list):
                authors = "; ".join(
                    a.get("name", "") for a in ja if isinstance(a, dict)
                )
            else:
                authors = "The Scottish Government"

        department = meta_map.get("directorate", "")
        topic = meta_map.get("topic", "")
        isbn = meta_map.get("isbn", "")
        category = topic or format_type
        keywords = topic

        # PDF: fetch /documents/ sub-page
        pdf_url = None
        original_filename = None
        docs_url = detail_url.rstrip("/") + "/documents/"
        try:
            docs_raw = _curl_get(docs_url)
            if docs_raw:
                docs_soup = _bs4(docs_raw)
                if docs_soup:
                    for file_div in docs_soup.select("div.ds_file-download"):
                        link_el = file_div.select_one("a.ds_file-download__title")
                        if not link_el:
                            continue
                        href = link_el.get("href", "")
                        ft_dd = file_div.select_one("dd.ds_metadata__value")
                        ft_text = ft_dd.get_text(strip=True).lower() if ft_dd else ""
                        if "pdf" in ft_text or href.lower().endswith(".pdf"):
                            pdf_url = urljoin(_BASE_URL, href)
                            path_tail = href.rstrip("/").split("/")[-1].split("?")[0]
                            original_filename = unquote(path_tail) if path_tail else None
                            break
        except Exception as pdf_err:
            print(f"[{_SITE_ID}] PDF lookup failed for {slug}: {pdf_err}")

        metadata = {
            "posted_date": listed_date_raw,
            "originalFilename": original_filename,
            "node_id": slug,
            "topic": topic,
            "from_role": "; ".join(from_roles) if from_roles else "",
            "format_type": format_type,
            "isbn": isbn,
            "date_modified": jsonld.get("dateModified", ""),
        }

        paper = {
            "id": None,
            "site_id": self.site_id,
            "external_id": slug,
            "post_number": slug,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": listed_date,
            "authors": authors,
            "publisher": "Scottish Government",
            "department": department,
            "journal": "",
            "url": detail_url,
            "pdf_url": pdf_url or "",
            "keywords": keywords,
            "category": category,
            "doi": isbn,
            "original_filename": original_filename or "",
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

        self._save_paper(paper)
        return True

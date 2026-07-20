# -*- coding: utf-8 -*-
"""Crawler for transformation.gouv.fr/espace-presse (Communiqués de presse).

Loaded via spec_from_file_location — uses absolute imports only.
Each press release card links directly to a PDF (no separate detail page).
Abstract is extracted from the PDF via pdftotext.
"""

import json
import os
import subprocess
import tempfile
import time
import urllib.parse

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

from crawler.base_crawler import BaseCrawler

_SITE_ID = "transformation-gouv-fr-espace-presse"
_BASE_URL = "https://www.transformation.gouv.fr"
_LIST_PATH = "/espace-presse"
_FILTER_PARAM = "field_type_presse_target_id%5B17%5D=17"
_MAX_PAGES = 200
_RETRY_DELAYS = (1, 3, 9)

_FRENCH_MONTHS = {
    "janvier": "01", "février": "02", "mars": "03", "avril": "04",
    "mai": "05", "juin": "06", "juillet": "07", "août": "08",
    "septembre": "09", "octobre": "10", "novembre": "11", "décembre": "12",
}


class TransformationGouvFrEspacePresseCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: transformation-gouv-fr-espace-presse"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def _soup(self, html):
        if BeautifulSoup is None:
            return None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    def _curl_text(self, url, retries=3):
        for attempt in range(retries):
            try:
                res = subprocess.run(
                    ["curl", "--tls-max", "1.3", "-sk",
                     "-A", "Mozilla/5.0 (compatible; crawler/1.0)",
                     "--max-time", "30", url],
                    capture_output=True, timeout=35,
                )
                if res.returncode == 0 and res.stdout:
                    return res.stdout.decode("utf-8", errors="replace")
            except Exception as exc:
                print(f"[{_SITE_ID}] text fetch attempt {attempt + 1}/{retries}: {exc}")
            if attempt < retries - 1:
                time.sleep(_RETRY_DELAYS[attempt])
        return None

    def _pdf_text(self, pdf_url, retries=3):
        """Download PDF and extract plain text via pdftotext. Returns str or None."""
        pdf_bytes = None
        for attempt in range(retries):
            try:
                res = subprocess.run(
                    ["curl", "--tls-max", "1.3", "-sk",
                     "-A", "Mozilla/5.0 (compatible; crawler/1.0)",
                     "--max-time", "60", pdf_url],
                    capture_output=True, timeout=65,
                )
                if res.returncode == 0 and res.stdout:
                    pdf_bytes = res.stdout
                    break
            except Exception as exc:
                print(f"[{_SITE_ID}] PDF fetch attempt {attempt + 1}/{retries}: {exc}")
            if attempt < retries - 1:
                time.sleep(_RETRY_DELAYS[attempt])

        if not pdf_bytes:
            return None

        tmp = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                f.write(pdf_bytes)
                tmp = f.name
            res2 = subprocess.run(
                ["pdftotext", tmp, "-"],
                capture_output=True, timeout=30,
            )
            if res2.returncode == 0:
                text = res2.stdout.decode("utf-8", errors="replace").strip()
                return text or None
        except Exception as exc:
            print(f"[{_SITE_ID}] pdftotext error for {pdf_url}: {exc}")
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
        return None

    @staticmethod
    def _parse_date(raw):
        """'DD mois YYYY' (French) → 'YYYY-MM-DD', or None."""
        parts = raw.strip().lower().split()
        if (len(parts) == 3
                and parts[0].isdigit()
                and parts[1] in _FRENCH_MONTHS
                and parts[2].isdigit()):
            return f"{parts[2]}-{_FRENCH_MONTHS[parts[1]]}-{int(parts[0]):02d}"
        return None

    # ------------------------------------------------------------------
    # Page parsing
    # ------------------------------------------------------------------

    def _parse_page(self, html):
        """Parse a list page. Returns (items: list[dict], has_next: bool)."""
        soup = self._soup(html)
        if not soup:
            return [], False

        items = []
        for article in soup.select("article.node--type-presse"):
            try:
                title_el = article.select_one("span.field--name-title")
                a_el = article.select_one("a[download][href]")
                if not title_el or not a_el:
                    continue

                title = title_el.get_text(strip=True)
                if not title:
                    continue

                href = a_el["href"]
                pdf_url = (_BASE_URL + href) if href.startswith("/") else href

                date_str = None
                for p in article.select("p.fr-card__detail"):
                    d = self._parse_date(p.get_text(" ", strip=True))
                    if d:
                        date_str = d
                        break

                tag_el = article.select_one("p.fr-tag")
                category = tag_el.get_text(strip=True) if tag_el else None

                fn_raw = href.rstrip("/").split("/")[-1].split("?")[0]
                original_filename = urllib.parse.unquote(fn_raw)

                items.append({
                    "title": title,
                    "pdf_url": pdf_url,
                    "pdf_href": href,
                    "published_date": date_str,
                    "category": category,
                    "original_filename": original_filename,
                })
            except Exception as exc:
                print(f"[{_SITE_ID}] item parse error: {exc}")

        # Next-page detection: DSFR pagination link without aria-disabled
        has_next = False
        for a in soup.select("a.fr-pagination__link--next"):
            if a.get("aria-disabled") not in ("true", True) and a.get("href"):
                has_next = True
                break

        return items, has_next

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        limit_eff = float("inf") if limit is None else limit
        deadline = time.time() + 25 * 60  # 25-minute wall-clock budget

        for page in range(_MAX_PAGES):
            if time.time() >= deadline:
                print(f"[{_SITE_ID}] page {page}: 25-min budget reached, stopping")
                break
            if saved >= limit_eff:
                break

            # page=0 → first page (no explicit page= param needed, but &page=0 also works)
            url = (f"{_BASE_URL}{_LIST_PATH}?{_FILTER_PARAM}" if page == 0
                   else f"{_BASE_URL}{_LIST_PATH}?{_FILTER_PARAM}&page={page}")

            html = self._curl_text(url)
            if not html:
                print(f"[{_SITE_ID}] page {page}: fetch failed, stopping")
                break

            items, has_next = self._parse_page(html)
            if not items:
                print(f"[{_SITE_ID}] page {page}: no items found, stopping")
                break

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_eff}")

            new_on_page = 0
            for item in items:
                if saved >= limit_eff or time.time() >= deadline:
                    break

                pdf_url = item["pdf_url"]
                if pdf_url in seen_urls:
                    continue
                seen_urls.add(pdf_url)
                new_on_page += 1

                try:
                    text = self._pdf_text(pdf_url)
                    if not text or len(text) < 100:
                        print(f"[{_SITE_ID}] abstract too short for '{item['title'][:60]}', skipping")
                        continue

                    abstract = text[:3000]
                    pub_date = item.get("published_date")
                    external_id = item["original_filename"]

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "title": item["title"],
                        "abstract": abstract,
                        "url": pdf_url,
                        "pdf_url": pdf_url,
                        "published_date": pub_date,
                        "posted_date": pub_date,
                        "publisher": "Ministère de la Transformation publique et de la Réforme de l'État",
                        "category": item.get("category"),
                        "original_filename": item["original_filename"],
                        "metadata": json.dumps({
                            "posted_date": pub_date,
                            "category": item.get("category"),
                            "original_filename": item["original_filename"],
                            "pdf_href": item.get("pdf_href"),
                        }, ensure_ascii=False),
                    }
                    self._save_paper(paper)
                    saved += 1
                    time.sleep(self._delay)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item error ('{item.get('title', '')[:60]}'): {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{_SITE_ID}] page {page}: all items already seen, stopping")
                break
            if not has_next:
                print(f"[{_SITE_ID}] page {page}: last page, stopping")
                break

        print(f"[{_SITE_ID}] done: saved={saved}")
        return saved

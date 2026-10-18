# -*- coding: utf-8 -*-
"""Bundesministerium für Finanzen (BMF) — Nationale Finanzbildungsstrategie Downloads."""

import json
import os
import re
import subprocess
import tempfile
import time
from datetime import date
from urllib.parse import unquote, urljoin

from crawler.base_crawler import BaseCrawler

_START_URL = (
    "https://www.bmf.gv.at/ministerium/nationale-finanzbildungsstrategie/"
    "uebersicht-nationale-finanzbildungsstrategie/downloads.html"
)
_PUBLISHER = "Bundesministerium für Finanzen (BMF)"
_WALL_BUDGET = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

_MONTH_DE = {
    "jänner": "01", "januar": "01", "februar": "02", "märz": "03",
    "april": "04", "mai": "05", "juni": "06", "juli": "07",
    "august": "08", "september": "09", "oktober": "10",
    "november": "11", "dezember": "12",
}


def _make_soup(html):
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("all BS4 parsers failed")


def _publication_date_evidence(text):
    """Read explicit frontmatter datelines without promoting cited/report years."""
    months = dict(_MONTH_DE, january="01", february="02", march="03",
                  may="05", june="06", july="07", october="10", december="12")
    month_names = "|".join(months)
    value_pattern = (
        r"20\d{2}-\d{2}-\d{2}|"
        r"(?:\d{1,2}\.?\s+)?(?:" + month_names + r")\s+20\d{2}|20\d{2}"
    )
    pattern = (
        r"^\s*(?:Wien|Vienna|Veröffentlicht|Erscheinungsdatum|Publication date|Published)"
        r"\s*[,：:]\s*(?P<value>" + value_pattern + r")\s*\.?\s*$"
    )
    candidates = []
    for match in re.finditer(pattern, text[:3000], re.IGNORECASE | re.MULTILINE):
        raw_value = match.group("value")
        value = None
        precision = None
        if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", raw_value):
            try:
                value = date.fromisoformat(raw_value).isoformat()
                precision = "day"
            except ValueError:
                continue
        elif re.fullmatch(r"20\d{2}", raw_value):
            value, precision = raw_value, "year"
        else:
            parts = re.fullmatch(
                r"(?:(\d{1,2})\.?\s+)?(" + month_names + r")\s+(20\d{2})",
                raw_value, re.IGNORECASE,
            )
            if not parts:
                continue
            day, month, year = parts.groups()
            month = months[month.lower()]
            if day:
                try:
                    value = date(int(year), int(month), int(day)).isoformat()
                    precision = "day"
                except ValueError:
                    continue
            else:
                value, precision = f"{year}-{month}", "month"
        candidates.append({"raw": match.group(0).strip(), "value": value,
                           "precision": precision, "role": "publication_dateline"})
    distinct = {candidate["value"] for candidate in candidates}
    chosen = candidates[0] if len(distinct) == 1 else None
    return {"value": chosen["value"] if chosen else None,
            "raw": chosen["raw"] if chosen else None,
            "precision": chosen["precision"] if chosen else None,
            "candidates": candidates, "conflict": len(distinct) > 1}


def _extract_date(text, filename=""):
    """Keep source precision; filenames and unlabelled years are not dates."""
    return _publication_date_evidence(text)["value"]


class BmfGvAtMinisteriumCrawler(BaseCrawler):
    site_id = "bmf-gv-at-ministerium"
    site_name = "Custom: bmf-gv-at-ministerium"
    base_url = "https://www.bmf.gv.at"

    # ------------------------------------------------------------------ #
    # Networking helpers
    # ------------------------------------------------------------------ #

    def _curl_get_text(self, url, retries=3, timeout=60):
        """GET via curl; returns decoded str or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        for attempt in range(retries):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
                if r.stdout:
                    return r.stdout.decode("utf-8", errors="replace")
            except Exception as exc:
                print(f"[bmf-gv-at-ministerium] curl attempt {attempt+1} error: {exc}")
            if attempt < retries - 1:
                time.sleep(3 ** attempt)
        return None

    def _extract_pdf_text(self, pdf_url):
        """Download PDF to a temp file and extract text via pdftotext."""
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
                tmp = fh.name
            cmd = [
                "curl", "-skL", "--tls-max", "1.3", "--max-time", "60",
                "-H", f"User-Agent: {self.USER_AGENT}",
                "-o", tmp, pdf_url,
            ]
            ok = False
            for attempt in range(3):
                try:
                    subprocess.run(cmd, capture_output=True, timeout=65, check=True)
                    if os.path.getsize(tmp) > 200:
                        ok = True
                        break
                except Exception as exc:
                    print(f"[bmf-gv-at-ministerium] PDF download attempt {attempt+1}: {exc}")
                if attempt < 2:
                    time.sleep(3 ** attempt)
            if not ok:
                return ""
            result = subprocess.run(
                ["pdftotext", "-q", tmp, "-"],
                capture_output=True, timeout=30,
            )
            return result.stdout.decode("utf-8", errors="replace").strip()
        except Exception as exc:
            print(f"[bmf-gv-at-ministerium] PDF text extraction failed: {exc}")
            return ""
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass

    # ------------------------------------------------------------------ #
    # Parsing
    # ------------------------------------------------------------------ #

    @staticmethod
    def _jcr_id(href):
        m = re.search(r'/dam/jcr:([a-f0-9-]{36})/', href)
        return m.group(1) if m else None

    def _parse_page(self, html):
        """Return list of item dicts from the downloads page HTML."""
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[bmf-gv-at-ministerium] parse error: {exc}")
            return []

        main = soup.find("main") or soup.find(id="content") or soup
        items = []
        current_category = "Dokumente"

        for child in main.children:
            if not hasattr(child, "name") or child.name is None:
                continue
            if child.name == "h3":
                current_category = child.get_text(strip=True)
            elif child.name == "ul":
                for li in child.find_all("li", recursive=False):
                    a = li.find("a", href=True)
                    if not a or "/dam/" not in a["href"]:
                        continue
                    href = a["href"]
                    raw_title = a.get_text(" ", strip=True)
                    # Strip trailing file info "(PDF, 1 MB)"
                    title = re.sub(
                        r'\s*\(\s*(?:PDF|Word|Excel|XLSX|DOCX|docx|xlsx)[^)]*\)\s*$',
                        "", raw_title, flags=re.IGNORECASE,
                    ).strip()
                    filename = unquote(href.rsplit("/", 1)[-1])
                    is_pdf = filename.lower().endswith(".pdf")
                    items.append({
                        "title": title,
                        "href": href,
                        "url": urljoin("https://www.bmf.gv.at", href),
                        "jcr_id": BmfGvAtMinisteriumCrawler._jcr_id(href),
                        "filename": filename,
                        "is_pdf": is_pdf,
                        "category": current_category,
                    })
        return items

    # ------------------------------------------------------------------ #
    # Main crawl
    # ------------------------------------------------------------------ #

    def crawl(self, limit=None):
        start_ts = time.time()
        saved = 0
        seen_urls = set()
        limit_inf = float("inf") if limit is None else limit

        print(f"[bmf-gv-at-ministerium] Fetching {_START_URL}")
        html = self._curl_get_text(_START_URL)
        if not html:
            print("[bmf-gv-at-ministerium] Failed to fetch page. Aborting.")
            return 0

        items = self._parse_page(html)
        print(f"[bmf-gv-at-ministerium] Found {len(items)} file links on page")

        for idx, item in enumerate(items):
            if saved >= limit_inf:
                break

            # wall-clock budget
            if time.time() - start_ts > _WALL_BUDGET:
                print(f"[bmf-gv-at-ministerium] 25-min wall budget reached. Stopping.")
                break

            url = item["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)

            try:
                title = item["title"]
                if not title:
                    print(f"[bmf-gv-at-ministerium] item {idx}: empty title, skipping")
                    continue

                filename = item["filename"]
                category = item["category"]
                jcr_id = item["jcr_id"]
                is_pdf = item["is_pdf"]
                pdf_url = url if is_pdf else None

                # --- abstract & date ---
                abstract = ""
                published_date = None
                date_evidence = _publication_date_evidence("")

                if is_pdf:
                    time.sleep(self._delay)
                    raw_text = self._extract_pdf_text(url)
                    if raw_text:
                        # Collapse whitespace; keep first 3000 chars
                        abstract = re.sub(r"\s+", " ", raw_text[:5000]).strip()
                        if len(abstract) > 3000:
                            abstract = abstract[:3000]
                        date_evidence = _publication_date_evidence(raw_text)
                        published_date = date_evidence["value"]
                else:
                    # Non-PDF: build abstract from category + title description
                    abstract = (
                        f"[{category}] {title}. "
                        f"Datei: {filename}. "
                        f"Herausgegeben vom {_PUBLISHER}."
                    )
                    published_date = _extract_date("", filename)

                if len(abstract) < 100:
                    print(
                        f"[bmf-gv-at-ministerium] '{title[:50]}': "
                        f"abstract too short ({len(abstract)} chars), skipping"
                    )
                    continue

                external_id = jcr_id or filename

                paper = {
                    "site_id": self.site_id,
                    "external_id": external_id,
                    "title": title,
                    "abstract": abstract,
                    "published_date": published_date,
                    "url": _START_URL,
                    "pdf_url": pdf_url,
                    "publisher": _PUBLISHER,
                    "authors": None,
                    "department": None,
                    "category": category,
                    "keywords": None,
                    "doi": None,
                    "original_filename": filename,
                    "metadata": json.dumps({
                        "jcr_id": jcr_id,
                        "originalFilename": filename,
                        "category": category,
                        "is_pdf": is_pdf,
                        "href": item["href"],
                        "publication_date_evidence": date_evidence,
                    }, ensure_ascii=False),
                }

                self._save_paper(paper)
                if self._last_save_created:
                    saved += 1
                limit_label = f"/{limit}" if limit is not None else ""
                print(f"[bmf-gv-at-ministerium] page 1: saved {saved}{limit_label}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[bmf-gv-at-ministerium] item {idx} failed: {exc}")
                continue

        print(f"[bmf-gv-at-ministerium] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------ #
    # Pure enumeration contract (NR-B); crawl() and its helpers are unchanged.
    # Listing = the one downloads page (_parse_page). Detail = the file itself;
    # the framework runs pdftotext (ext:pdf_text) and _enumeration_paper keeps
    # crawl()'s rules (5000/3000 chars, dateline, 100-char minimum).
    # ------------------------------------------------------------------ #

    def _enumeration_items(self, html):
        rows, seen = [], set()
        for item in self._parse_page(html):
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            if not item["title"]:
                continue  # crawl(): empty title, skipped
            rows.append(dict(item))
        return rows

    def _enumeration_paper(self, item, pdf_text):
        url = item["url"]
        title = item["title"]
        if not title:
            return None
        filename = item["filename"]
        category = item["category"]
        jcr_id = item["jcr_id"]
        is_pdf = item["is_pdf"]
        pdf_url = url if is_pdf else None
        abstract = ""
        published_date = None
        date_evidence = _publication_date_evidence("")
        if is_pdf:
            raw_text = (pdf_text or "").strip()
            if raw_text:
                abstract = re.sub(r"\s+", " ", raw_text[:5000]).strip()
                if len(abstract) > 3000:
                    abstract = abstract[:3000]
                date_evidence = _publication_date_evidence(raw_text)
                published_date = date_evidence["value"]
        else:
            abstract = (
                f"[{category}] {title}. "
                f"Datei: {filename}. "
                f"Herausgegeben vom {_PUBLISHER}."
            )
            published_date = _extract_date("", filename)
        if len(abstract) < 100:
            return None
        external_id = jcr_id or filename
        return {
            "site_id": self.site_id,
            "external_id": external_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "url": _START_URL,
            "pdf_url": pdf_url,
            "publisher": _PUBLISHER,
            "authors": None,
            "department": None,
            "category": category,
            "keywords": None,
            "doi": None,
            "original_filename": filename,
            "metadata": json.dumps({
                "jcr_id": jcr_id,
                "originalFilename": filename,
                "category": category,
                "is_pdf": is_pdf,
                "href": item["href"],
                "publication_date_evidence": date_evidence,
            }, ensure_ascii=False),
        }

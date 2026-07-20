# -*- coding: utf-8 -*-
"""RUOMO+ (University of Macedonia) DSpace-CRIS repository crawler.

Starting URL: https://ruomoplus.lib.uom.gr/simple-search?query=&location=publications
&sort_by=dc.date.issued_dt&order=desc&rpp=10&crisID=&relationName=&etal=0
&filtername=itemtype&filterquery=journal%20article&filtertype=equals

The site sits behind an Anubis (techaro.lol) proof-of-work anti-bot challenge.
This crawler solves the SHA-256 "fast" PoW itself (no browser/JS needed) and
reuses the resulting auth cookie via a persistent ``requests.Session`` for the
rest of the crawl.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
import unicodedata
from urllib.parse import unquote, urlencode

import requests
import urllib3

from crawler.base_crawler import BaseCrawler

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from bs4 import BeautifulSoup as _BS
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS = None
    _PARSERS = []


def _make_soup(raw_html: str):
    if _BS is None:
        raise RuntimeError("BeautifulSoup (bs4) is not installed")
    last_exc = None
    for parser in _PARSERS:
        try:
            return _BS(raw_html, parser)
        except Exception as exc:
            last_exc = exc
            continue
    raise RuntimeError(f"No working HTML parser (tried {_PARSERS}): {last_exc}")


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


_GREEK_MONTH3 = {
    "ιαν": 1, "φεβ": 2, "μαρ": 3, "απρ": 4, "μαι": 5,
    "αυγ": 8, "σεπ": 9, "οκτ": 10, "νοε": 11, "δεκ": 12,
}

_GREEK_DATE_RE = re.compile(r"\d{1,2}-[Α-Ωα-ωΆ-Ώά-ώ]{3,6}-\d{4}")

_LABEL_KEY_MAP = {
    "τομος": "volume",
    "τευχος": "issue",
    "πρωτη σελιδα": "firstpage",
    "τελευταια σελιδα": "lastpage",
}


class RuomoplusLibUomGrSimpleSearchCrawler(BaseCrawler):
    """Crawler for RUOMO+ (ruomoplus.lib.uom.gr) journal-article records."""

    site_id = "ruomoplus-lib-uom-gr-simple-search"
    site_name = "Custom: ruomoplus-lib-uom-gr-simple-search"
    base_url = "https://ruomoplus.lib.uom.gr"

    _START_URL = (
        "https://ruomoplus.lib.uom.gr/simple-search?query=&location=publications"
        "&sort_by=dc.date.issued_dt&order=desc&rpp=10&crisID=&relationName=&etal=0"
        "&filtername=itemtype&filterquery=journal%20article&filtertype=equals"
    )
    _RPP = 10
    _MIN_ABSTRACT = 50
    _MAX_PAGES = 200
    _MAX_WALL = 25 * 60  # seconds

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn, delay=delay)
        self._session.verify = False

    # ------------------------------------------------------------------
    # Anubis proof-of-work challenge
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_json_block(raw_html: str, elem_id: str):
        m = re.search(
            rf'<script id="{elem_id}" type="application/json">(.*?)</script>',
            raw_html, re.S,
        )
        if not m:
            return None
        try:
            return json.loads(m.group(1))
        except (ValueError, TypeError):
            return None

    def _solve_anubis(self, raw_html: str, target_url: str) -> bool:
        challenge_obj = self._extract_json_block(raw_html, "anubis_challenge")
        if not challenge_obj:
            return False
        base_prefix = self._extract_json_block(raw_html, "anubis_base_prefix") or ""

        challenge = challenge_obj.get("challenge") or {}
        rules = challenge_obj.get("rules") or {}
        random_data = challenge.get("randomData")
        difficulty = rules.get("difficulty")
        cid = challenge.get("id")
        if not random_data or difficulty is None or not cid:
            return False

        half = difficulty // 2
        odd = difficulty % 2 != 0

        nonce = 0
        digest = b""
        deadline = time.time() + 60
        found = False
        while time.time() < deadline:
            digest = hashlib.sha256((random_data + str(nonce)).encode("utf-8")).digest()
            ok = all(digest[i] == 0 for i in range(half))
            if ok and odd:
                ok = (digest[half] >> 4) == 0
            if ok:
                found = True
                break
            nonce += 1
        if not found:
            print(f"[{self.site_id}] Anubis PoW solve timed out")
            return False

        params = {
            "id": cid,
            "response": digest.hex(),
            "nonce": nonce,
            "redir": target_url,
            "elapsedTime": 50,
        }
        pass_url = (
            f"{self.base_url}{base_prefix}/.within.website/x/cmd/anubis/api/pass-challenge"
            f"?{urlencode(params)}"
        )
        try:
            r = self._session.get(pass_url, timeout=30, allow_redirects=True)
            r.raise_for_status()
            return True
        except requests.RequestException as exc:
            print(f"[{self.site_id}] Anubis pass-challenge failed: {exc}")
            return False

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _get(self, url: str, retries: int = 3) -> str | None:
        """GET with retries, exponential backoff, and Anubis auto-solve."""
        waits = [1, 3, 9]
        for attempt in range(retries):
            try:
                time.sleep(self._delay)
                r = self._session.get(url, timeout=30)
                text = r.content.decode("utf-8", errors="replace")
                if 'id="anubis_challenge"' in text:
                    if self._solve_anubis(text, url):
                        r2 = self._session.get(url, timeout=30)
                        return r2.content.decode("utf-8", errors="replace")
                    # solving failed; fall through to retry loop
                else:
                    return text
            except requests.RequestException as exc:
                print(f"[{self.site_id}] request error (attempt {attempt + 1}/{retries}) for {url}: {exc}")
            if attempt < retries - 1:
                wait = waits[min(attempt, len(waits) - 1)]
                print(f"[{self.site_id}] retrying in {wait}s...")
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _build_list_url(self, start: int) -> str:
        if start <= 0:
            return self._START_URL
        return f"{self._START_URL}&start={start}"

    def _parse_list_page(self, raw_html: str) -> list:
        try:
            soup = _make_soup(raw_html)
        except Exception as exc:
            print(f"[{self.site_id}] list HTML parse error: {exc}")
            return []

        items = []
        seen_handles = set()
        for a in soup.find_all("a", href=re.compile(r"/handle/\d+/\d+")):
            href = (a.get("href") or "").strip()
            m = re.search(r"/handle/(\d+/\d+)", href)
            if not m:
                continue
            handle = m.group(1)
            if handle in seen_handles:
                continue
            seen_handles.add(handle)

            tr = a.find_parent("tr")
            row_text = tr.get_text(" ", strip=True) if tr else ""
            date_match = _GREEK_DATE_RE.search(row_text)
            listed_date_raw = date_match.group(0) if date_match else ""

            full_url = href if href.startswith("http") else f"{self.base_url}{href}"
            items.append({
                "handle": handle,
                "url": full_url,
                "listed_date_raw": listed_date_raw,
            })
        return items

    @staticmethod
    def _reformat_author(name: str) -> str:
        """"Family, Given" -> "Given Family" so no author string embeds a comma.

        The shared libertree adapter re-splits a semicolon-free authors string
        on ",": a lone author in "Family, Given" form would otherwise be
        misread as two separate authors.
        """
        name = name.strip()
        m = re.match(r"^([^,]+),\s*(.+)$", name)
        if m:
            family, given = m.groups()
            name = f"{given.strip()} {family.strip()}"
        return re.sub(r"\s+", " ", name).strip()

    @staticmethod
    def _meta_list(soup, name: str) -> list:
        return [
            (m.get("content") or "").strip()
            for m in soup.find_all("meta", attrs={"name": name})
            if (m.get("content") or "").strip()
        ]

    def _meta_first(self, soup, name: str) -> str | None:
        vals = self._meta_list(soup, name)
        return vals[0] if vals else None

    @staticmethod
    def _greek_month_num(mon_raw: str) -> int | None:
        key = _strip_accents(mon_raw.lower())
        if key.startswith("ιου"):
            if len(key) > 3 and key[3] == "ν":
                return 6
            if len(key) > 3 and key[3] == "λ":
                return 7
            return None
        return _GREEK_MONTH3.get(key[:3])

    def _parse_greek_date(self, raw: str | None) -> str | None:
        if not raw:
            return None
        m = re.match(r"(\d{1,2})-([Α-Ωα-ωΆ-Ώά-ώ]+)-(\d{4})", raw.strip())
        if not m:
            return None
        day, mon_raw, year = m.groups()
        mon_num = self._greek_month_num(mon_raw)
        if not mon_num:
            return None
        try:
            return f"{int(year):04d}-{mon_num:02d}-{int(day):02d}"
        except ValueError:
            return None

    @staticmethod
    def _normalize_iso_date(raw: str | None) -> str | None:
        if not raw:
            return None
        raw = raw.strip()
        if len(raw) >= 10 and re.match(r"\d{4}-\d{2}-\d{2}", raw):
            return raw[:10]
        if re.match(r"^\d{4}-\d{2}$", raw):
            return f"{raw}-01"
        if re.match(r"^\d{4}$", raw):
            return f"{raw}-01-01"
        return None

    @staticmethod
    def _parse_label_table(soup) -> dict:
        result = {}
        for td_label in soup.find_all("td", class_="metadataFieldLabel"):
            label_text = td_label.get_text(strip=True).rstrip(":").strip()
            value_td = td_label.find_next_sibling("td", class_="metadataFieldValue")
            if value_td is None:
                continue
            norm = _strip_accents(label_text.lower())
            key = _LABEL_KEY_MAP.get(norm)
            if key:
                result[key] = value_td.get_text(strip=True)
        return result

    def _parse_detail(self, raw_html: str) -> dict:
        soup = _make_soup(raw_html)

        title = self._meta_first(soup, "DC.title") or self._meta_first(soup, "citation_title")
        if not title:
            h1 = soup.find("h1")
            title = h1.get_text(strip=True) if h1 else None

        abstract_raw = self._meta_first(soup, "DCTERMS.abstract") or ""
        abstract = re.sub(r"\s+", " ", html.unescape(abstract_raw)).strip()

        authors_list = self._meta_list(soup, "DC.creator") or self._meta_list(soup, "citation_author")
        authors_list = [self._reformat_author(a) for a in authors_list if a.strip()]
        authors = "; ".join(dict.fromkeys(a for a in authors_list if a)) or None

        publisher_list = self._meta_list(soup, "DC.publisher")
        publisher = "; ".join(dict.fromkeys(p.strip() for p in publisher_list if p.strip())) or None

        journal = self._meta_first(soup, "DCTERMS.isPartOf")

        dept_list = self._meta_list(soup, "DC.contributor")
        department = "; ".join(dict.fromkeys(d.strip() for d in dept_list if d.strip())) or None

        doi = None
        for m in soup.find_all("meta", attrs={"name": "DC.identifier"}):
            content = (m.get("content") or "").strip()
            if content and not m.get("scheme") and re.match(r"^10\.\d{4,9}/\S+$", content):
                doi = content
                break

        issued_raw = self._meta_first(soup, "DCTERMS.issued")
        published_date = self._normalize_iso_date(issued_raw)

        pdf_url = self._meta_first(soup, "citation_pdf_url")
        original_filename = None
        if pdf_url:
            tail = pdf_url.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
            original_filename = unquote(tail) or None

        subj_list = self._meta_list(soup, "DC.subject")
        category = None
        keyword_terms = []
        for s in subj_list:
            if s.upper().startswith("FRASCATI"):
                category = s.replace("FRASCATI__", "").replace("__", " / ")
            else:
                keyword_terms.append(s)
        keywords = ", ".join(dict.fromkeys(keyword_terms)) or None

        label_fields = self._parse_label_table(soup)

        return {
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "publisher": publisher,
            "journal": journal,
            "department": department,
            "doi": doi,
            "published_date": published_date,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "category": category,
            "keywords": keywords,
            "volume": label_fields.get("volume"),
            "issue": label_fields.get("issue"),
            "firstpage": label_fields.get("firstpage"),
            "lastpage": label_fields.get("lastpage"),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        page_num = 0
        start_offset = 0
        limit_str = str(limit) if limit is not None else "inf"

        try:
            while True:
                if limit is not None and saved >= limit:
                    break
                if page_num >= self._MAX_PAGES:
                    print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                    break
                if time.time() - start_time > self._MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget ({self._MAX_WALL}s) exceeded. Stopping cleanly.")
                    break

                list_url = self._build_list_url(start_offset)
                raw = self._get(list_url)
                page_num += 1
                start_offset += self._RPP

                if not raw:
                    print(f"[{self.site_id}] page {page_num}: failed to fetch list page, stopping.")
                    break

                items = self._parse_list_page(raw)
                if not items:
                    print(f"[{self.site_id}] page {page_num}: 0 items on page, stopping.")
                    break

                new_count = 0
                for item in items:
                    if limit is not None and saved >= limit:
                        break

                    url = item["url"]
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    new_count += 1

                    try:
                        detail_raw = self._get(url)
                        if not detail_raw:
                            print(f"[{self.site_id}] item {url} failed: empty response after retries")
                            continue

                        fields = self._parse_detail(detail_raw)

                        title = fields.get("title")
                        if not title:
                            print(f"[{self.site_id}] item {url}: no title found, skipping")
                            continue

                        abstract = fields.get("abstract") or ""
                        if len(abstract) < self._MIN_ABSTRACT:
                            print(f"[{self.site_id}] item {url}: abstract too short ({len(abstract)} chars), skipping")
                            continue

                        listed_date = (
                            self._parse_greek_date(item.get("listed_date_raw"))
                            or fields.get("published_date")
                        )

                        handle = item["handle"]
                        handle_tail = handle.split("/")[-1]
                        post_number = handle_tail if handle_tail.isdigit() else handle

                        meta = {"handle": handle, "node_id": handle}
                        if listed_date:
                            meta["posted_date"] = listed_date
                        if item.get("listed_date_raw"):
                            meta["listed_date_raw"] = item["listed_date_raw"]
                        if fields.get("journal"):
                            meta["journal_raw"] = fields["journal"]
                        if fields.get("volume"):
                            meta["volume"] = fields["volume"]
                        if fields.get("issue"):
                            meta["issue"] = fields["issue"]
                        if fields.get("firstpage"):
                            meta["firstpage"] = fields["firstpage"]
                        if fields.get("lastpage"):
                            meta["lastpage"] = fields["lastpage"]
                        if fields.get("original_filename"):
                            meta["originalFilename"] = fields["original_filename"]

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": handle,
                            "post_number": post_number,
                            "title": title,
                            "abstract": abstract,
                            "published_date": fields.get("published_date"),
                            "listed_date": listed_date,
                            "authors": fields.get("authors"),
                            "publisher": fields.get("publisher"),
                            "department": fields.get("department"),
                            "journal": fields.get("journal"),
                            "url": url,
                            "pdf_url": fields.get("pdf_url"),
                            "keywords": fields.get("keywords"),
                            "category": fields.get("category"),
                            "doi": fields.get("doi"),
                            "original_filename": fields.get("original_filename"),
                            "metadata": json.dumps(meta, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                    except Exception as exc:
                        print(f"[{self.site_id}] item {url} failed: {exc}")
                        continue

                if new_count == 0:
                    print(f"[{self.site_id}] page {page_num}: 0 new records, stopping.")
                    break

                if page_num % 10 == 0:
                    print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit_str}")

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

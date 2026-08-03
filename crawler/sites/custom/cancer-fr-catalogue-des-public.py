# -*- coding: utf-8 -*-
"""Crawler for cancer.fr catalogue publication collections 89 and 101.

The supplied catalogue URL is a normal server-rendered Ibexa page. It reports
23 filtered results and exposes classic ``page=`` pagination, but many result
slots currently render as empty ``li`` elements. To keep the crawl useful while
staying tied to the target collections, this crawler uses:

1. the supplied filtered HTML catalogue pages as the canonical list source;
2. each candidate's real detail page as the source of truth;
3. the site's public Algolia publication index only as a last-resort rescue
   source if the filtered HTML pages yield no usable detail records.
"""

from __future__ import annotations

import os
import html
import json
import re
import subprocess
import time
import unicodedata
import urllib.parse
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from crawler.base_crawler import BaseCrawler


class CancerFrCataloguePublicCrawler(BaseCrawler):
    site_id = "cancer-fr-catalogue-des-public"
    site_name = "Custom: cancer-fr-catalogue-des-public"
    base_url = "https://www.cancer.fr"

    _START_URL = (
        "https://www.cancer.fr/catalogue-des-publications"
        "?publications_catalog%5Bfilters%5D%5Bsearch_text%5D="
        "&publications_catalog%5Bfilters%5D%5Bcollection%5D%5B%5D=89"
        "&publications_catalog%5Bfilters%5D%5Bcollection%5D%5B%5D=101"
        "&search="
    )
    _PAGE_CAP = 200
    _PAGE_SIZE = 50
    _WALL_CLOCK_BUDGET = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

    _ALGOLIA_APP_ID = "9O0WH8HVSU"
    _ALGOLIA_API_KEY = os.environ.get("CANCER_FR_CATALOGUE_DES_PUBLIC_KEY", "")
    _ALGOLIA_INDEX = "production_fre-FR_default"
    _ALGOLIA_URL = (
        "https://9O0WH8HVSU-dsn.algolia.net"
        "/1/indexes/production_fre-FR_default/query"
    )
    _ALGOLIA_QUERIES = ("détection précoce cancers peau", "Fiches repère")

    _TARGET_COLLECTION_IDS = ("89", "101")
    _TARGET_COLLECTIONS = {
        "fiches repere",
        "notes d'analyse",
        "notes d analyse",
    }

    _MONTHS_FR = {
        "janvier": "01",
        "fevrier": "02",
        "février": "02",
        "mars": "03",
        "avril": "04",
        "mai": "05",
        "juin": "06",
        "juillet": "07",
        "aout": "08",
        "août": "08",
        "septembre": "09",
        "octobre": "10",
        "novembre": "11",
        "decembre": "12",
        "décembre": "12",
    }

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(
        self,
        url: str,
        method: str = "GET",
        body: Optional[str] = None,
        headers: Optional[Iterable[str]] = None,
        timeout: int = 35,
    ) -> Optional[bytes]:
        """Fetch bytes with curl, retrying one URL up to three times."""
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--max-time",
            str(timeout),
            "-H",
            f"User-Agent: {self.USER_AGENT}",
            "-H",
            "Accept: text/html,application/xhtml+xml,application/json,*/*;q=0.8",
            "-H",
            "Accept-Language: fr-FR,fr;q=0.9,en;q=0.8",
        ]
        if headers:
            for header in headers:
                cmd.extend(["-H", header])
        if method.upper() == "POST":
            cmd.extend(["-X", "POST"])
        if body is not None:
            cmd.extend(["-d", body])
        cmd.append(url)

        waits = (1, 3, 9)
        last_error = ""
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 5,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout
                last_error = (
                    result.stderr.decode("utf-8", errors="replace").strip()
                    or f"curl exit {result.returncode}"
                )
            except Exception as exc:
                last_error = str(exc)

            if attempt < 2:
                wait = waits[attempt]
                print(
                    f"[{self.site_id}] network error for {url} "
                    f"(attempt {attempt + 1}/3): {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] network failed after 3 attempts for {url}: {last_error}")
        return None

    def _curl_get(self, url: str) -> Optional[bytes]:
        return self._curl(url)

    def _curl_post_json(self, url: str, payload: Dict) -> Optional[bytes]:
        headers = [
            "Content-Type: application/json",
            f"X-Algolia-Application-Id: {self._ALGOLIA_APP_ID}",
            f"X-Algolia-API-Key: {self._ALGOLIA_API_KEY}",
        ]
        return self._curl(url, method="POST", body=json.dumps(payload), headers=headers)

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _decode(raw: bytes) -> str:
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return str(raw)

    @classmethod
    def _make_soup(cls, raw: bytes):
        text = cls._decode(raw)
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return None

        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _clean_text(value: str) -> str:
        if not value:
            return ""
        value = html.unescape(str(value)).replace("\xa0", " ")
        value = re.sub(r"\s+", " ", value)
        return value.strip()

    @classmethod
    def _node_text(cls, node) -> str:
        if not node:
            return ""
        return cls._clean_text(node.get_text(" ", strip=True))

    @classmethod
    def _normalize(cls, value: str) -> str:
        value = cls._clean_text(value).lower()
        value = unicodedata.normalize("NFKD", value)
        value = "".join(ch for ch in value if not unicodedata.combining(ch))
        value = value.replace("’", "'")
        return value

    @classmethod
    def _is_target_collection(cls, collection: str) -> bool:
        norm = cls._normalize(collection)
        return any(target in norm for target in cls._TARGET_COLLECTIONS)

    @classmethod
    def _parse_date(cls, value: str) -> Optional[str]:
        value = cls._clean_text(value)
        if not value:
            return None
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
        if m:
            return m.group(0)
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", value)
        if m:
            return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"

        low = value.lower()
        for month_name, month_num in cls._MONTHS_FR.items():
            if month_name in low:
                m = re.search(r"(\d{1,2})\s+" + re.escape(month_name) + r"\s+(\d{4})", low)
                if m:
                    return f"{m.group(2)}-{month_num}-{int(m.group(1)):02d}"
                m = re.search(re.escape(month_name) + r"\s+(\d{4})", low)
                if m:
                    return f"{m.group(1)}-{month_num}-01"
        m = re.search(r"\b(19|20)\d{2}\b", value)
        if m:
            return f"{m.group(0)}-01-01"
        return None

    @staticmethod
    def _ts_to_iso(value) -> Optional[str]:
        if value in (None, ""):
            return None
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            return None

    @staticmethod
    def _absolute_url(base_url: str, href: str) -> str:
        return urllib.parse.urljoin(base_url + "/", href)

    @staticmethod
    def _filename_from_url(url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        path = urllib.parse.urlparse(url).path
        filename = urllib.parse.unquote(path.rstrip("/").rsplit("/", 1)[-1])
        return filename if filename else None

    @staticmethod
    def _slug_from_url(url: str) -> Optional[str]:
        path = urllib.parse.urlparse(url).path.rstrip("/")
        if not path:
            return None
        return path.rsplit("/", 1)[-1] or None

    @staticmethod
    def _extract_numeric_tail(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        m = re.search(r"(\d+)$", str(value))
        return m.group(1) if m else None

    @classmethod
    def _abstract_from_algolia_text(cls, meta_text: str, title: str) -> str:
        if not meta_text:
            return ""
        title_norm = cls._normalize(title)
        parts: List[str] = []
        for line in meta_text.replace("\xa0", " ").splitlines():
            clean = cls._clean_text(line)
            if not clean:
                continue
            if cls._normalize(clean) == title_norm:
                continue
            if re.fullmatch(r"\d{1,10}", clean):
                continue
            parts.append(clean)
        return cls._clean_text(" ".join(parts))

    # ------------------------------------------------------------------
    # List/detail parsing
    # ------------------------------------------------------------------

    def _list_url(self, page: int) -> str:
        if page <= 1:
            return self._START_URL
        parts = urllib.parse.urlsplit(self._START_URL)
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        query = [(k, v) for (k, v) in query if k != "page"]
        query.append(("page", str(page)))
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), "")
        )

    def _parse_list_page(self, raw: bytes, page_url: str) -> Tuple[List[Dict], Optional[str]]:
        soup = self._make_soup(raw)
        if soup is None:
            return [], None

        records: List[Dict] = []
        for li in soup.select("li.list-articles-item"):
            link = li.select_one("h3.card-title a[href]") or li.select_one(
                'a[href*="/catalogue-des-publications/"]'
            )
            if not link:
                continue
            href = link.get("href", "")
            if not href or href.rstrip("/") == "/catalogue-des-publications":
                continue
            detail_url = self._absolute_url(self.base_url, href)
            title = self._node_text(link)
            if not title:
                title = self._node_text(li.select_one(".card-title"))
            summary = ""
            for p in li.select(".card-body p"):
                if p.find("a", href=True):
                    continue
                summary = self._node_text(p)
                if summary:
                    break
            pdf_link = li.select_one('a[href*="/content/download/"][href*=".pdf"]')
            pdf_url = self._absolute_url(self.base_url, pdf_link["href"]) if pdf_link else None
            records.append(
                {
                    "source": "html_list",
                    "url": detail_url,
                    "title": title,
                    "list_summary": summary,
                    "list_pdf_url": pdf_url,
                    "list_page_url": page_url,
                }
            )

        next_link = soup.select_one('a[rel="next"][href]') or soup.select_one(
            ".js-next-page a[href]"
        )
        next_url = self._absolute_url(self.base_url, next_link["href"]) if next_link else None
        return records, next_url

    def _parse_detail(self, raw: bytes, detail_url: str) -> Dict:
        result = {
            "title": "",
            "abstract": "",
            "published_date": None,
            "published_date_raw": "",
            "pdf_url": None,
            "original_filename": None,
            "collection": "",
            "public_target": "",
            "reference": "",
            "format": "",
            "language": "",
            "node_id": None,
            "pdf_download_id": None,
        }

        soup = self._make_soup(raw)
        if soup is None:
            return result

        h1 = soup.find("h1")
        if h1:
            result["title"] = self._node_text(h1)

        resume = soup.select_one(".publication-block-resume")
        if resume:
            result["abstract"] = self._node_text(resume)
        if not result["abstract"]:
            meta = soup.find("meta", attrs={"name": "description"})
            if meta and meta.get("content"):
                result["abstract"] = self._clean_text(meta["content"])

        pdf_link = soup.select_one('a[href*="/content/download/"][href*=".pdf"]')
        if pdf_link:
            result["pdf_url"] = self._absolute_url(self.base_url, pdf_link["href"])
            result["original_filename"] = self._filename_from_url(result["pdf_url"])
            m = re.search(r"/content/download/(\d+)/", result["pdf_url"])
            if m:
                result["pdf_download_id"] = m.group(1)

        block = soup.select_one(".block-bg-light")
        if block:
            for li in block.find_all("li"):
                strong = li.find("strong")
                if not strong:
                    continue
                label = self._normalize(strong.get_text(" ", strip=True)).strip(": ")
                time_tag = li.find("time")
                if time_tag:
                    raw_value = (
                        time_tag.get("datetime")
                        or time_tag.get_text(" ", strip=True)
                        or ""
                    )
                    value = self._clean_text(raw_value)
                else:
                    label_text = strong.get_text(" ", strip=True)
                    value = self._clean_text(li.get_text(" ", strip=True))
                    value = re.sub(
                        r"^" + re.escape(self._clean_text(label_text)) + r"\s*:?\s*",
                        "",
                        value,
                        flags=re.IGNORECASE,
                    ).strip()

                if "collection" in label:
                    result["collection"] = value
                elif label == "public" or label.startswith("public "):
                    result["public_target"] = value
                elif "date de publication" in label or label == "date":
                    result["published_date_raw"] = value
                    result["published_date"] = self._parse_date(value)
                elif "reference" in label:
                    result["reference"] = value
                elif "format" in label:
                    result["format"] = value
                elif "langue" in label:
                    result["language"] = value

        text = self._decode(raw)
        m = re.search(r"locationPathArray\s*=\s*\[([^\]]+)\]", text)
        if m:
            ids = re.findall(r'"(\d+)"', m.group(1))
            if ids:
                result["node_id"] = ids[-1]

        return result

    def _fetch_detail_record(self, candidate: Dict) -> Optional[Dict]:
        detail_url = candidate.get("url") or ""
        if not detail_url:
            return None

        time.sleep(self._delay)
        raw = self._curl_get(detail_url)
        if not raw:
            return None

        detail = self._parse_detail(raw, detail_url)
        title = detail.get("title") or candidate.get("title") or ""
        abstract = detail.get("abstract") or candidate.get("list_summary") or ""

        if self._normalize(title) in {"oops! an error occurred", "oops an error occurred"}:
            print(f"[{self.site_id}] skip error page: {detail_url}")
            return None

        if len(abstract.strip()) < 50:
            intro = candidate.get("intro_value") or ""
            meta_text = candidate.get("meta_content__text") or ""
            abstract = intro or self._abstract_from_algolia_text(meta_text, title)
        if len(abstract.strip()) < 50:
            print(
                f"[{self.site_id}] skip short abstract "
                f"({len(abstract.strip())} chars): {title[:80]}"
            )
            return None

        pdf_url = detail.get("pdf_url") or candidate.get("list_pdf_url")
        original_filename = (
            detail.get("original_filename")
            or self._filename_from_url(pdf_url)
        )

        listed_date = (
            detail.get("published_date")
            or candidate.get("listed_date")
            or self._ts_to_iso(candidate.get("content_publication_date"))
        )
        published_date = detail.get("published_date") or listed_date

        algolia_object_id = candidate.get("objectID") or candidate.get("algolia_object_id")
        post_number = (
            self._extract_numeric_tail(algolia_object_id)
            or detail.get("node_id")
            or self._extract_numeric_tail(detail.get("pdf_download_id"))
        )
        slug = self._slug_from_url(detail_url)
        external_id = post_number or algolia_object_id or slug or detail_url

        metadata = {
            "posted_date": listed_date,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "node_id": detail.get("node_id"),
            "algolia_object_id": algolia_object_id,
            "slug": slug,
            "source": candidate.get("source"),
            "list_page_url": candidate.get("list_page_url"),
            "listed_date_raw": listed_date,
            "published_date_raw": detail.get("published_date_raw"),
            "collection": detail.get("collection"),
            "collection_ids": list(self._TARGET_COLLECTION_IDS),
            "public_target": detail.get("public_target"),
            "reference": detail.get("reference"),
            "format": detail.get("format"),
            "language": detail.get("language"),
            "pdf_download_id": detail.get("pdf_download_id"),
            "content_publication_date": candidate.get("content_publication_date"),
            "year": candidate.get("year"),
            "list_summary": candidate.get("list_summary"),
        }

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": str(external_id) if external_id is not None else None,
            "post_number": str(post_number) if post_number is not None else None,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": None,
            "publisher": "Institut national du cancer",
            "department": None,
            "journal": None,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": None,
            "category": detail.get("collection") or None,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Algolia fallback
    # ------------------------------------------------------------------

    def _algolia_page(self, query: str, page: int) -> Optional[Dict]:
        payload = {
            "query": query,
            "hitsPerPage": self._PAGE_SIZE,
            "page": page,
            "facetFilters": ["content_type_name:Publication"],
        }
        raw = self._curl_post_json(self._ALGOLIA_URL, payload)
        if not raw:
            return None
        try:
            return json.loads(self._decode(raw))
        except Exception as exc:
            print(f"[{self.site_id}] Algolia JSON parse failed on page {page}: {exc}")
            return None

    @staticmethod
    def _candidate_from_algolia(hit: Dict, query: str) -> Optional[Dict]:
        url = hit.get("url") or ""
        if not url:
            return None
        full_url = urllib.parse.urljoin("https://www.cancer.fr/", url)
        return {
            "source": f"algolia:{query or 'all'}",
            "url": full_url,
            "title": hit.get("content_name") or hit.get("title_value") or "",
            "intro_value": hit.get("intro_value") or "",
            "meta_content__text": hit.get("meta_content__text") or "",
            "objectID": hit.get("objectID"),
            "content_publication_date": hit.get("content_publication_date"),
            "listed_date": CancerFrCataloguePublicCrawler._ts_to_iso(
                hit.get("content_publication_date")
            ),
            "year": hit.get("year"),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def _limit_reached(self, saved: int, limit) -> bool:
        return limit is not None and saved >= limit

    def _budget_nearly_done(self, start_time: float) -> bool:
        return time.time() - start_time >= self._WALL_CLOCK_BUDGET - 10

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        seen_page_urls = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "inf"

        # Phase 1: the supplied filtered catalogue HTML pages.
        page = 1
        next_url: Optional[str] = self._START_URL
        while next_url and page <= self._PAGE_CAP:
            if self._limit_reached(saved, limit):
                break
            if self._budget_nearly_done(start_time):
                print(f"[{self.site_id}] wall-clock budget approaching; stopping cleanly")
                print(f"[{self.site_id}] Done. Total saved: {saved}")
                return saved
            if next_url in seen_page_urls:
                print(f"[{self.site_id}] pagination loop detected at page {page}; stopping")
                break
            seen_page_urls.add(next_url)

            if page == 1 or page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            raw = self._curl_get(next_url)
            if not raw:
                print(f"[{self.site_id}] list page {page} failed; stopping HTML phase")
                break

            candidates, parsed_next_url = self._parse_list_page(raw, next_url)
            page_new = 0
            for candidate in candidates:
                if self._limit_reached(saved, limit):
                    break
                detail_url = candidate.get("url")
                if not detail_url or detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)

                try:
                    paper = self._fetch_detail_record(candidate)
                    if not paper:
                        continue
                    self._save_paper(paper)
                    saved += 1
                    page_new += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {paper['title'][:80]}")
                except Exception as exc:
                    print(f"[{self.site_id}] item {detail_url} failed: {exc}")
                    continue

            if not parsed_next_url:
                break
            if page_new == 0 and not candidates:
                print(f"[{self.site_id}] page {page} had no parseable records; following next link")
            next_url = parsed_next_url
            page += 1

        if page > self._PAGE_CAP:
            print(f"[{self.site_id}] Safety cap of {self._PAGE_CAP} pages reached")

        # Phase 2: last-resort Algolia rescue. The target HTML list is the
        # normal full-depth source; Algolia currently contains stale catalogue
        # URLs, so use it only if the filtered pages produced no usable item.
        if saved > 0:
            print(f"[{self.site_id}] Done. Total saved: {saved}")
            return saved

        progress_page = max(page, 1)
        for query in self._ALGOLIA_QUERIES:
            algolia_page = 0
            while algolia_page < self._PAGE_CAP:
                if self._limit_reached(saved, limit):
                    break
                if self._budget_nearly_done(start_time):
                    print(f"[{self.site_id}] wall-clock budget approaching; stopping cleanly")
                    print(f"[{self.site_id}] Done. Total saved: {saved}")
                    return saved

                if progress_page == 1 or progress_page % 10 == 0:
                    print(f"[{self.site_id}] page {progress_page}: saved {saved}/{limit_str}")

                data = self._algolia_page(query, algolia_page)
                if not data:
                    break
                hits = data.get("hits") or []
                if not hits:
                    break

                page_new = 0
                for hit in hits:
                    if self._limit_reached(saved, limit):
                        break
                    candidate = self._candidate_from_algolia(hit, query)
                    if not candidate:
                        continue
                    detail_url = candidate.get("url")
                    if not detail_url or detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)

                    try:
                        paper = self._fetch_detail_record(candidate)
                        if not paper:
                            continue
                        if not self._is_target_collection(paper.get("category") or ""):
                            continue
                        self._save_paper(paper)
                        saved += 1
                        page_new += 1
                        print(f"[{self.site_id}] Saved {saved}/{limit_str}: {paper['title'][:80]}")
                    except Exception as exc:
                        print(f"[{self.site_id}] item {detail_url} failed: {exc}")
                        continue

                algolia_page += 1
                progress_page += 1

                nb_pages = data.get("nbPages")
                if nb_pages is not None and algolia_page >= int(nb_pages):
                    break
                if page_new == 0 and query and algolia_page >= int(nb_pages or 0):
                    break

            if algolia_page >= self._PAGE_CAP:
                print(f"[{self.site_id}] Safety cap of {self._PAGE_CAP} pages reached")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

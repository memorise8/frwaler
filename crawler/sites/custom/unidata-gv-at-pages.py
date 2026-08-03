# -*- coding: utf-8 -*-
"""Crawler for unidata.gv.at Auswertungen.

Discovered live endpoints:
  - start page: https://unidata.gv.at/Pages/auswertungen.aspx
  - list API:   /_api/web/lists(guid'34B5E193-E012-4729-92A2-841CC62628CB')/items
  - detail API: /_api/web/lists(guid'...')/items({ID})/FieldValuesAsText

The public page renders a SharePoint document library named "Auswertungen".
Items are XLCubed report definitions rather than PDFs, so pdf_url is only set
if the library ever contains a real PDF; the report file URL is preserved in
metadata.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from html import unescape
from urllib.parse import quote, urlencode, urljoin, unquote

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - regex fallbacks still work
    BeautifulSoup = None

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_THIS_DIR)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402


_START_URL = "https://unidata.gv.at/Pages/auswertungen.aspx"
_DEFAULT_LIST_ID = "34B5E193-E012-4729-92A2-841CC62628CB"
_DEFAULT_VIEW_ID = "EF93C466-4C67-4915-8D41-E50BC3F15E7C"
_DEFAULT_LIST_PATH = "/Auswertungen"
_PAGE_SIZE = 50
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_WALL_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_ABSTRACT_MIN_CHARS = 50
_PUBLISHER = "unidata.gv.at"


class UnidataGvAtPagesCrawler(BaseCrawler):
    site_id = "unidata-gv-at-pages"
    site_name = "Custom: unidata-gv-at-pages"
    base_url = "https://unidata.gv.at"

    detail_delay = 1.0

    def crawl(self, limit=None):
        if limit is not None and limit <= 0:
            return 0

        started_at = time.time()
        saved = 0
        page = 1
        next_url = None
        seen_urls = set()
        limit_label = limit if limit is not None else "inf"

        info = self._discover_library()
        list_id = info.get("list_id") or _DEFAULT_LIST_ID

        while True:
            if limit is not None and saved >= limit:
                break
            if time.time() - started_at > _WALL_BUDGET_SECONDS - 30:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly")
                break
            if page > _MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached; stopping")
                break
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            page_data = self._fetch_list_page(list_id, next_url)
            if not page_data:
                print(f"[{self.site_id}] page {page}: list API returned no data; stopping")
                break

            items = page_data.get("items") or []
            next_url = page_data.get("next_url")
            if not items:
                print(f"[{self.site_id}] page {page}: no records; done")
                break

            new_urls_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                item_id = self._string(item.get("ID") or item.get("Id"))
                if not item_id:
                    print(f"[{self.site_id}] item without ID skipped")
                    continue

                dedupe_url = self._detail_url(item_id, info.get("list_path"))
                if dedupe_url in seen_urls:
                    continue
                seen_urls.add(dedupe_url)
                new_urls_on_page += 1

                try:
                    time.sleep(float(getattr(self, "detail_delay", self._delay)))
                    detail = self._fetch_detail(list_id, item_id)
                    if not detail:
                        print(f"[{self.site_id}] item {item_id} detail failed after retries; skipping")
                        continue

                    paper = self._build_paper(item, detail, info)
                    if not paper:
                        continue

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < _ABSTRACT_MIN_CHARS:
                        print(f"[{self.site_id}] item {item_id} short abstract ({len(abstract)} chars); skipping")
                        continue

                    self._save_paper(paper)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_id} failed: {exc}")
                    continue

            if new_urls_on_page == 0:
                print(f"[{self.site_id}] page {page}: no new URLs; stopping to avoid pagination loop")
                break
            if not next_url:
                print(f"[{self.site_id}] page {page}: last page reached")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network and endpoint discovery
    # ------------------------------------------------------------------

    def _curl_text(self, url, *, accept="text/html,application/json,*/*", retries=3, max_time=45):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--max-time",
            str(max_time),
            "-A",
            self.USER_AGENT,
            "-H",
            f"Accept: {accept}",
            "-H",
            "Accept-Language: de-AT,de;q=0.9,en;q=0.8",
            url,
        ]
        waits = (1, 3, 9)
        last_error = None
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=max_time + 10,
                    check=False,
                )
                stdout = result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace")
                if result.returncode == 0 and stdout.strip():
                    return stdout
                last_error = stderr.strip() or f"curl returned {result.returncode}"
            except Exception as exc:
                last_error = str(exc)

            if attempt < retries - 1:
                print(
                    f"[{self.site_id}] network error for {url}: {last_error}; "
                    f"retrying in {waits[attempt]}s"
                )
                time.sleep(waits[attempt])

        print(f"[{self.site_id}] network failed after 3 attempts for {url}: {last_error}")
        return None

    def _curl_json(self, url):
        raw = self._curl_text(url, accept="application/json;odata=verbose, application/json, */*")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            snippet = self._clean_text(raw[:160])
            print(f"[{self.site_id}] JSON decode failed for {url}: {exc}; body starts {snippet!r}")
            return None
        if not isinstance(data, dict):
            print(f"[{self.site_id}] unexpected JSON type for {url}: {type(data).__name__}")
            return None
        return data

    def _discover_library(self):
        info = {
            "list_id": _DEFAULT_LIST_ID,
            "view_id": _DEFAULT_VIEW_ID,
            "list_path": _DEFAULT_LIST_PATH,
            "item_count": None,
            "intro": (
                "Hier können Auswertungen zu zentralen Datenbereichen der "
                "Hochschulstatistik über Standardberichte abgerufen werden."
            ),
        }

        raw = self._curl_text(_START_URL, max_time=60)
        if not raw:
            print(f"[{self.site_id}] start page fetch failed; using discovered fallback list metadata")
            return info

        list_match = re.search(r"ctx\.listName\s*=\s*\"\\?\{([0-9a-fA-F-]{36})\\?\}\"", raw)
        view_match = re.search(r"ctx\.view\s*=\s*\"\\?\{([0-9a-fA-F-]{36})\\?\}\"", raw)
        path_match = re.search(r"ctx\.listUrlDir\s*=\s*\"([^\"]+)\"", raw)
        count_match = re.search(r'"ItemCount"\s*:\s*"(\d+)"', raw)
        if list_match:
            info["list_id"] = list_match.group(1)
        if view_match:
            info["view_id"] = view_match.group(1)
        if path_match:
            info["list_path"] = path_match.group(1).replace("\\/", "/")
        if count_match:
            info["item_count"] = count_match.group(1)

        soup = self._make_soup(raw)
        if soup is not None:
            candidates = []
            for selector in (
                "div.ms-rtestate-field p",
                "#DeltaPlaceHolderMain p",
                "main p",
                "p",
            ):
                for node in soup.select(selector):
                    text = self._clean_text(node.get_text(" ", strip=True))
                    if "Auswertungen" in text and "Hochschulstatistik" in text:
                        candidates.append(text)
                if candidates:
                    break
            if candidates:
                info["intro"] = candidates[0]

        return info

    def _fetch_list_page(self, list_id, next_url=None):
        if next_url:
            url = next_url
        else:
            endpoint = f"{self.base_url}/_api/web/lists(guid'{list_id}')/items"
            select_fields = [
                "Id",
                "ID",
                "GUID",
                "Title",
                "Sortierung",
                "Kategorie",
                "Subkategorie",
                "Created",
                "Modified",
                "File/ServerRelativeUrl",
                "File/Name",
                "File/Length",
                "File/TimeCreated",
                "File/TimeLastModified",
            ]
            params = {
                "$top": str(_PAGE_SIZE),
                "$select": ",".join(select_fields),
                "$expand": "File",
                "$orderby": "Id desc",
            }
            url = endpoint + "?" + urlencode(params, safe=",/()'")

        data = self._curl_json(url)
        if not data:
            return None

        d = data.get("d") if isinstance(data, dict) else None
        if isinstance(d, dict):
            items = d.get("results")
            next_page = d.get("__next")
        else:
            items = data.get("value") if isinstance(data, dict) else None
            next_page = data.get("odata.nextLink") if isinstance(data, dict) else None

        if not isinstance(items, list):
            return None
        return {"items": items, "next_url": next_page, "raw": data}

    def _fetch_detail(self, list_id, item_id):
        url = (
            f"{self.base_url}/_api/web/lists(guid'{list_id}')"
            f"/items({quote(str(item_id))})/FieldValuesAsText"
        )
        data = self._curl_json(url)
        d = data.get("d") if isinstance(data, dict) else None
        if isinstance(d, dict):
            return d
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if not k.startswith("odata.")}
        return None

    # ------------------------------------------------------------------
    # Parsing and mapping
    # ------------------------------------------------------------------

    def _build_paper(self, item, detail, info):
        item_id = self._string(item.get("ID") or item.get("Id"))
        if not item_id:
            return None

        file_info = item.get("File") if isinstance(item.get("File"), dict) else {}
        title = (
            self._string(item.get("Title"))
            or self._string(detail.get("Title"))
            or self._string(file_info.get("Name"))
            or f"Unidata Auswertung {item_id}"
        )
        title = self._clean_title(title)

        category = self._string(item.get("Kategorie") or detail.get("Kategorie"))
        subcategory = self._string(item.get("Subkategorie") or detail.get("Subkategorie"))
        sort_order = self._string(item.get("Sortierung") or detail.get("Sortierung"))

        created_raw = (
            self._string(detail.get("Created"))
            or self._string(item.get("Created"))
            or self._string(file_info.get("TimeCreated"))
        )
        modified_raw = (
            self._string(detail.get("Modified"))
            or self._string(item.get("Modified"))
            or self._string(file_info.get("TimeLastModified"))
        )
        listed_date = self._iso_date(created_raw)
        published_date = self._iso_date(modified_raw) or listed_date

        original_filename = (
            self._string(detail.get("FileLeafRef"))
            or self._string(file_info.get("Name"))
            or self._filename_from_path(detail.get("FileRef"))
        )
        file_path = (
            self._string(file_info.get("ServerRelativeUrl"))
            or self._string(detail.get("FileRef"))
        )
        file_url = self._absolute_url(file_path) if file_path else None
        pdf_url = file_url if (original_filename or "").lower().endswith(".pdf") else None
        detail_url = self._detail_url(item_id, info.get("list_path"))

        abstract = self._make_abstract(
            title=title,
            category=category,
            subcategory=subcategory,
            created=listed_date,
            modified=published_date,
            filename=original_filename,
            file_size=self._string(
                file_info.get("Length")
                or detail.get("File_x005f_x0020_x005f_Size")
                or detail.get("SMTotalFileStreamSize")
            ),
            intro=info.get("intro"),
        )

        keywords = self._join_keywords(
            [
                "unidata",
                "Hochschulstatistik",
                category,
                subcategory,
            ]
        )

        metadata = {
            "posted_date": created_raw,
            "posted_date_iso": listed_date,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "list_id": info.get("list_id") or _DEFAULT_LIST_ID,
            "view_id": info.get("view_id") or _DEFAULT_VIEW_ID,
            "list_path": info.get("list_path") or _DEFAULT_LIST_PATH,
            "item_count": info.get("item_count"),
            "item_id": item_id,
            "ID": item_id,
            "Id": item_id,
            "GUID": self._string(item.get("GUID") or detail.get("GUID")),
            "Sortierung": sort_order,
            "Kategorie": category,
            "Subkategorie": subcategory,
            "Created": created_raw,
            "Modified": modified_raw,
            "file": file_info,
            "file_url": file_url,
            "detail_url": detail_url,
            "detail_api": (
                f"{self.base_url}/_api/web/lists(guid'{info.get('list_id') or _DEFAULT_LIST_ID}')"
                f"/items({item_id})/FieldValuesAsText"
            ),
            "list_api": (
                f"{self.base_url}/_api/web/lists(guid'{info.get('list_id') or _DEFAULT_LIST_ID}')/items"
            ),
            "field_values_as_text": detail,
            "list_record": item,
            "source_page": _START_URL,
        }

        return {
            "id": f"{self.site_id}-{item_id}",
            "site_id": self.site_id,
            "external_id": item_id,
            "post_number": item_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": None,
            "publisher": _PUBLISHER,
            "department": None,
            "journal": None,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _make_abstract(
        self,
        *,
        title,
        category,
        subcategory,
        created,
        modified,
        filename,
        file_size,
        intro,
    ):
        parts = [
            "Unidata-Auswertung aus der österreichischen Hochschulstatistik.",
            f"Titel: {title}.",
        ]
        if category:
            parts.append(f"Kategorie: {category}.")
        if subcategory:
            parts.append(f"Subkategorie: {subcategory}.")
        if filename:
            parts.append(f"Berichtsdatei: {filename}.")
        if created:
            parts.append(f"Gelisted seit: {created}.")
        if modified:
            parts.append(f"Zuletzt veröffentlicht oder aktualisiert: {modified}.")
        if file_size:
            parts.append(f"Dateigröße laut SharePoint: {file_size} Byte.")
        if intro:
            parts.append(self._clean_text(intro))
        return self._clean_text(" ".join(parts))

    def _detail_url(self, item_id, list_path=None):
        path = (list_path or _DEFAULT_LIST_PATH).rstrip("/")
        return f"{self.base_url}{path}/Forms/DispForm.aspx?ID={quote(str(item_id))}"

    def _absolute_url(self, path):
        if not path:
            return None
        if str(path).startswith(("http://", "https://")):
            return str(path)
        return urljoin(self.base_url, quote(str(path), safe="/:%?&=#[]@!$&'()*+,;"))

    def _make_soup(self, raw):
        if BeautifulSoup is None:
            return None
        last_error = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                last_error = exc
                continue
        print(f"[{self.site_id}] BeautifulSoup parser chain failed: {last_error}")
        return None

    @staticmethod
    def _clean_title(value):
        text = UnidataGvAtPagesCrawler._clean_text(value)
        return re.sub(r"\.(?:xl3wbz|xlsx?|csv|pdf)\s*$", "", text, flags=re.I)

    @staticmethod
    def _clean_text(value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ").replace("\u200b", "")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _string(value):
        text = UnidataGvAtPagesCrawler._clean_text(value)
        return text or None

    @staticmethod
    def _filename_from_path(path):
        if not path:
            return None
        tail = str(path).rstrip("/").split("/")[-1].split("?", 1)[0].split("#", 1)[0]
        return unquote(tail) if tail else None

    @staticmethod
    def _iso_date(value):
        text = UnidataGvAtPagesCrawler._clean_text(value)
        if not text:
            return None

        m = re.search(r"\b(20\d{2}|19\d{2})-(\d{2})-(\d{2})T", text)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2}|19\d{2})\b", text)
        if m:
            day, month, year = m.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"

        m = re.search(r"\b(20\d{2}|19\d{2})-(\d{2})-(\d{2})\b", text)
        if m:
            return m.group(0)

        return None

    @staticmethod
    def _join_keywords(values):
        seen = set()
        out = []
        for value in values:
            text = UnidataGvAtPagesCrawler._clean_text(value)
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
        return ", ".join(out) if out else None

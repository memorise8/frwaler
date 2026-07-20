# -*- coding: utf-8 -*-
"""Crawler for doc.cerema.fr — Editions du Cerema publications (Syracuse/Ermes SIGB).

API: POST Portal/Recherche/Search.svc/Search
Key: FacetFilter MUST be a JSON-encoded string (not an object).
     {"_488":"Oui","_416":"Editions du Cerema"} filters for publicly available
     Cerema editions (Oui = public PDF, Editions du Cerema = publisher type).
"""

import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_SEARCH_URL = "https://doc.cerema.fr/Portal/Recherche/Search.svc/Search"
_INIT_URL = "https://doc.cerema.fr/Default/search.aspx"
_PAGE_SIZE = 50  # max supported by API (PageSizeResult: [5,10,25,50])
_MAX_PAGES = 200
_MAX_MINUTES = 25
# FacetFilter must be JSON-stringified; _488=public PDF, _416=publisher type
_FACET_FILTER = json.dumps({"_488": "Oui", "_416": "Editions du Cerema"})
_QUERY_GUID = "f6263fa6-ee7c-41d4-856d-2af7c8165659"


def _strip_html(html_text):
    """Strip HTML tags and decode common entities."""
    if not html_text:
        return ""
    try:
        text = re.sub(r"<[^>]+>", " ", html_text)
    except Exception:
        text = html_text
    for ent, val in [
        ("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
        ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"),
        ("&laquo;", "«"), ("&raquo;", "»"),
    ]:
        text = text.replace(ent, val)
    text = re.sub(r"&#[0-9]+;", "", text)
    text = re.sub(r"&[a-zA-Z]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _make_bs(raw, parsers=("html5lib", "lxml", "html.parser")):
    """Try parsers in order, return BeautifulSoup or None."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    for parser in parsers:
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _retry_curl(cmd, max_attempts=3):
    """Run a curl command with exponential backoff. Returns stdout or None."""
    import subprocess
    for attempt in range(max_attempts):
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=40
            )
            out = result.stdout
            if out:
                try:
                    return out.decode("utf-8")
                except UnicodeDecodeError:
                    return out.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[doc-cerema-fr-default] curl attempt {attempt+1}/{max_attempts} error: {exc}")
        if attempt < max_attempts - 1:
            wait = [1, 3, 9][attempt]
            time.sleep(wait)
    return None


class DocCeremaFrDefaultCrawler(BaseCrawler):
    """Crawler for doc.cerema.fr (Cerema documentation portal)."""

    site_id = "doc-cerema-fr-default"
    site_name = "Custom: doc-cerema-fr-default"
    base_url = "https://doc.cerema.fr"

    def _build_payload(self, page):
        return {
            "query": {
                "CloudTerms": [],
                "ExceptTotalFacet": True,
                # FacetFilter MUST be a JSON string, not an object
                "FacetFilter": _FACET_FILTER,
                "ForceSearch": True,
                "HiddenFacetFilter": "{}",
                "InitialSearch": False,
                "Page": page,
                "PageRange": 3,
                "QueryGuid": _QUERY_GUID,
                "QueryString": "*:*",
                "ResultSize": _PAGE_SIZE,
                "ScenarioCode": "DEFAULT",
                "ScenarioDisplayMode": "display-standard",
                "SearchGridFieldsShownOnResultsDTO": [],
                "SearchLabel": "Tous les documents",
                "SearchTerms": "*:*",
                "SiteCodeRestriction": "",
                "SortField": None,
                "SortOrder": 0,
                "TemplateParams": {
                    "Scenario": "",
                    "Scope": "Default",
                    "Size": None,
                    "Source": "",
                    "Support": "",
                    "UseCompact": False,
                },
                "UseSpellChecking": None,
            }
        }

    def _fetch_page(self, page):
        """POST to search API. Returns parsed JSON dict or None."""
        body = json.dumps(self._build_payload(page), ensure_ascii=False)
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--max-time", "35",
            "-X", "POST",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Content-Type: application/json; charset=UTF-8",
            "-H", "Accept: application/json",
            "-H", "X-Requested-With: XMLHttpRequest",
            "-H", f"Referer: {_INIT_URL}",
            "-d", body,
            _SEARCH_URL,
        ]
        raw = _retry_curl(cmd)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[doc-cerema-fr-default] JSON decode error on page {page}: {exc}")
            return None
        if not data.get("success"):
            errs = data.get("errors", [])
            print(f"[doc-cerema-fr-default] API error on page {page}: {errs}")
            return None
        return data

    def _fetch_detail(self, friendly_url):
        """Fetch detail page to extract PDF download URL.

        Returns (pdf_url_or_None, original_filename_or_None).
        """
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,*/*;q=0.8",
            "-H", f"Referer: {_INIT_URL}",
            friendly_url,
        ]
        raw = _retry_curl(cmd)
        if not raw:
            return None, None

        # PDF download link: basicfilesdownload.ashx?itemGuid=<UUID>
        m = re.search(
            r'href="(https://doc\.cerema\.fr/pro/basicfilesdownload\.ashx\?[^"]+)"',
            raw,
        )
        if not m:
            return None, None

        pdf_url = m.group(1).replace("&amp;", "&")
        # Derive filename from itemGuid
        mg = re.search(r"itemGuid=([A-Fa-f0-9-]+)", pdf_url)
        original_filename = f"{mg.group(1)}.pdf" if mg else None
        return pdf_url, original_filename

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        limit_or_inf = limit if limit is not None else "∞"
        start_time = time.time()
        page_max = None

        for p in range(_MAX_PAGES):
            if limit is not None and saved >= limit:
                break

            elapsed_min = (time.time() - start_time) / 60
            if elapsed_min >= _MAX_MINUTES:
                print(
                    f"[doc-cerema-fr-default] Wall-clock budget ({_MAX_MINUTES}m) "
                    f"reached at page {p}. Exiting."
                )
                break

            if p % 10 == 0:
                print(f"[doc-cerema-fr-default] page {p}: saved {saved}/{limit_or_inf}")

            data = self._fetch_page(p)
            if not data:
                print(f"[doc-cerema-fr-default] Failed to fetch page {p}. Stopping.")
                break

            d = data["d"]
            results = d.get("Results", []) or []

            if p == 0:
                info = d.get("SearchInfo") or {}
                page_max = info.get("PageMax")
                nb = info.get("NBResults")
                print(
                    f"[doc-cerema-fr-default] Total: {nb} records, "
                    f"PageMax: {page_max} (PageSize={_PAGE_SIZE})"
                )

            if not results:
                print(f"[doc-cerema-fr-default] No results on page {p}. Done.")
                break

            new_on_page = 0
            for item in results:
                if limit is not None and saved >= limit:
                    break

                try:
                    friendly_url = (item.get("FriendlyUrl") or "").strip()
                    if not friendly_url or friendly_url in seen_urls:
                        continue
                    seen_urls.add(friendly_url)
                    new_on_page += 1

                    res = item.get("Resource") or {}
                    rsc_id = str(res.get("RscId") or "").strip()
                    title = (res.get("Ttl") or "").strip()
                    if not title:
                        continue

                    abstract = _strip_html(res.get("Desc") or "")
                    if len(abstract) < 50:
                        print(
                            f"[doc-cerema-fr-default] Skip {rsc_id} — "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    published_date = str(res.get("Dt") or "").strip() or None
                    publisher = (res.get("Pbls") or "").strip() or "Cerema"
                    series = (res.get("Src") or "").strip() or None
                    doc_type = (res.get("Type") or "").strip() or None
                    frmt = (res.get("Frmt") or "").strip()
                    rsc_base = (res.get("RscBase") or "").strip()
                    rsc_uid = res.get("RscUid")

                    # Fetch detail page for PDF URL (per-item, with delay)
                    time.sleep(self._delay)
                    pdf_url, original_filename = self._fetch_detail(friendly_url)

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": rsc_id,
                        "post_number": rsc_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": None,
                        "authors": None,
                        "publisher": publisher,
                        "department": None,
                        "journal": series,
                        "url": friendly_url,
                        "pdf_url": pdf_url,
                        "keywords": None,
                        "category": doc_type,
                        "doi": None,
                        "original_filename": original_filename,
                        "metadata": json.dumps({
                            "posted_date": None,
                            "originalFilename": original_filename,
                            "rscBase": rsc_base,
                            "rscUid": rsc_uid,
                            "format": frmt,
                            "series": series,
                        }, ensure_ascii=False),
                    })
                    saved += 1
                    print(
                        f"[doc-cerema-fr-default] Saved {saved}/{limit_or_inf}: "
                        f"{title[:70]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[doc-cerema-fr-default] Item failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[doc-cerema-fr-default] No new records on page {p}. Done.")
                break

            if page_max is not None and p >= page_max - 1:
                print(f"[doc-cerema-fr-default] Reached PageMax={page_max}. Done.")
                break

        else:
            print(
                f"[doc-cerema-fr-default] Safety cap of {_MAX_PAGES} pages reached."
            )

        print(f"[doc-cerema-fr-default] Done. Total saved: {saved}")
        return saved

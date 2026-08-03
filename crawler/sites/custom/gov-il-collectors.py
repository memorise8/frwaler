# -*- coding: utf-8 -*-
"""Israel unified government portal (www.gov.il) collectors crawler.

``www.gov.il`` is a Cloudflare-protected SPA — the HTML shell carries no
content; every listing and article is served by a JSON API on a separate,
*non*-challenged host:

    https://openapi-gc.digital.gov.il/pub/cio/govil/rest

Two REST calls drive this crawler (both need an ``x-client-id`` public key
that is embedded verbatim in the SPA's JS bundle, plus an ``Origin`` header
of ``https://www.gov.il`` — without either the API returns
``FailedToResolveAPIKey`` / ``RF-OriginError``):

  1. LISTING — ``.../collectors/v1/api/DataCollector/GetResults``
       news:         ``CollectorType=news&Type=<NEWS_TYPE>&officeId=<GUID>``
       publications: ``CollectorType=reports&CollectorType=rfp
                        &CollectorType=drushim&CollectorType=publicsharing
                        &Type=<PUB_TYPE>&officeId=<GUID>``
     Common params: ``culture=he&skip=<N>&limit=<M>``. Response is
     ``{"total": int, "results": [ {title, description, url, tags{...}} ]}``.
     Paging: increment ``skip`` by page size until ``skip >= total``.

  2. CONTENT — ``.../contentpage/v1/api/content-pages/{urlName}?culture=he``
     where ``urlName`` is the last path segment of a result's ``url``.
     Yields ``contentHead.title/description``,
     ``contentMain.htmlContents[].sectionData`` (the HTML body, used as the
     abstract) and, when a document is attached,
     ``contentSub.filesToDownload.filesGroupItems[].items[]`` each carrying a
     ``url`` pointing at ``https://www.gov.il/BlobFolder/...`` plus
     ``fileName`` / ``fileSize`` / ``extension``.

Networking uses ``curl_cffi`` with Chrome TLS impersonation — the exact
mechanism :class:`crawler.stealth_fetcher.StealthSession` uses in its
first layer. ``StealthSession.fetch_html`` itself is unusable here because
it returns *text* classified as HTML (JSON/binary responses would be
misclassified and needlessly fall through to Playwright).

BlobFolder file downloads are guarded by a **case-sensitive** Cloudflare
WAF rule: the literal ``/BlobFolder/`` path is 403-blocked even for a real
(challenge-solved) headless browser, but the equivalent lowercase
``/blobfolder/`` path is served normally over ``curl_cffi``'s impersonated
TLS. We therefore lowercase that single path segment before downloading.
Downloaded bytes are persisted through the standard libertree blob-storage
path (:func:`crawler.blob_storage.save_pdf` +
:func:`crawler.db_libertree.update_document_pdf`).

Content is Hebrew (UTF-8) and handled as such throughout.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from bs4 import BeautifulSoup  # noqa: E402

from crawler.base_crawler import BaseCrawler  # noqa: E402
from crawler import blob_storage as _blobs  # noqa: E402
from crawler import db_libertree as _ldb  # noqa: E402


class GovIlCollectorsCrawler(BaseCrawler):
    """Crawler for www.gov.il ministry news + publications collectors."""

    site_id = "gov-il-collectors"
    site_name = "gov.il — Israeli Ministry News & Publications"
    base_url = "https://www.gov.il"

    # --- API surface -------------------------------------------------------
    _API_ROOT = "https://openapi-gc.digital.gov.il/pub/cio/govil/rest"
    _GET_RESULTS = _API_ROOT + "/collectors/v1/api/DataCollector/GetResults"
    _CONTENT_PAGE = _API_ROOT + "/contentpage/v1/api/content-pages/{urlname}"
    # Public API key lifted from the SPA JS bundle (rotates rarely).
    _X_CLIENT_ID = "9KFgciHHGDyNiqz5MdQS0eK2ApeJYMc6YnElUICpN1atirZc"

    # Collector-type / Type GUID filters (from scripts/audit/uncollected_probe.csv).
    _NEWS_TYPE = "cc21a290-f00a-4adb-8d31-575d2be9fef9"
    _PUB_TYPE = "4775599a-4bdd-43c6-a60d-0d77a2790020"
    _PUB_COLLECTOR_TYPES = ("reports", "rfp", "drushim", "publicsharing")

    # (officeId, English label) for every Israeli ministry in the probe sheet.
    OFFICES = (
        ("920e5116-ca8b-4bb5-b54c-6a518d3d94b9", "Agriculture & Rural Development"),
        ("503be068-3051-4dc0-bc61-65f0b33f0570", "Communications"),
        ("c515b1b2-3163-499e-bdbd-8849147c5ada", "Construction & Housing"),
        ("7d5d01a1-c043-4c87-b8ef-5061999e797c", "Culture & Sport"),
        ("7566b0f1-c9a4-4af7-ab01-d34ca3a52104", "Defense"),
        ("1213ca14-4263-4720-b7bf-afa8e8c05444", "Diaspora Affairs & Combating Antisemitism"),
        ("ba3bf87e-6a99-4e24-ae89-2815a450881e", "Economy & Industry"),
        ("3a4dfc8a-ac31-4ab4-8734-35090ae3bb06", "Education"),
        ("0caee7eb-1ebb-4c12-9ed8-7dce94badcb2", "Energy & Infrastructure"),
        ("7807091f-2385-4915-b1cb-fd4db6d2c1cc", "Environmental Protection"),
        ("5c7214c6-6571-43b1-9474-a0541e7afd94", "Intelligence"),
        ("6cbf57de-3976-484a-8666-995ca17899ec", "Foreign Affairs"),
        ("104cb0f4-d65a-4692-b590-94af928c19c0", "Health"),
        ("27db3169-ab0e-490c-af70-6d03133cb1f3", "Immigration & Absorption"),
        ("bbdbfb50-7ef3-4e3b-9718-f40f011283c6", "Interior"),
        ("86842de6-987b-42d4-b9c2-cbd7d0619534", "Justice"),
        ("4fa63b79-3d73-4a66-b3f5-ff385dd31cc7", "Labor / Welfare & Social Affairs"),
        ("a4558f18-657b-450e-87c3-32d70f6e07b5", "National Security"),
        ("4f415ec8-cd6c-426a-adcc-342917abae72", "Religious Services"),
        ("d6b46ce2-975b-43ee-afbd-404ba29b01e8", "Tourism"),
        ("48eaee91-0c97-4ef3-b364-50ae52e3b56f", "Transport & Road Safety"),
        ("75d0cbd7-46cf-487b-930c-2e7b12d7f846", "Innovation, Science & Technology"),
        ("90fba09e-a331-4c19-a6d3-8301993f382e", "Social Equality"),
    )

    _PAGE_SIZE = 20
    _WALL_CLOCK_BUDGET_SEC = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _PAGE_SAFETY_CAP = 500          # max listing pages per (office, mode)
    _HTTP_RETRIES = 3

    _DOWNLOADABLE_EXT = ("pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx")

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn, delay=delay)
        from curl_cffi import requests as _curl_requests
        self._curl = _curl_requests
        self._sess = _curl_requests.Session(impersonate="chrome131")
        self._sess.headers.update({
            "x-client-id": self._X_CLIENT_ID,
            "Origin": "https://www.gov.il",
            "Referer": "https://www.gov.il/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
        })

    # ------------------------------------------------------------------
    # HTTP helpers (curl_cffi Chrome-TLS impersonation)
    # ------------------------------------------------------------------

    def _get_json(self, url, params=None):
        """GET JSON with retries + polite delay. Returns dict/list or None."""
        for attempt in range(self._HTTP_RETRIES):
            time.sleep(self._delay)
            try:
                r = self._sess.get(url, params=params, timeout=30)
                if r.status_code == 200:
                    return r.json()
                reason = f"http {r.status_code}"
            except Exception as exc:  # network / decode error
                reason = f"{type(exc).__name__}: {exc}"
            if attempt < self._HTTP_RETRIES - 1:
                time.sleep((attempt + 1) * 3)
            else:
                print(f"[{self.site_id}] GET failed ({reason}): {url}")
        return None

    def _download_file(self, file_url):
        """Download a BlobFolder document. Returns bytes or None.

        Works around the case-sensitive Cloudflare WAF rule on ``/BlobFolder/``
        by requesting the lowercase ``/blobfolder/`` path instead.
        """
        url = file_url.replace("/BlobFolder/", "/blobfolder/")
        for attempt in range(self._HTTP_RETRIES):
            time.sleep(self._delay)
            try:
                r = self._sess.get(url, timeout=90)
                if r.status_code == 200 and r.content[:4] == b"%PDF":
                    return r.content
                # Non-PDF (e.g. Office doc) — accept any 200 binary that
                # isn't the Cloudflare HTML challenge page.
                ctype = (r.headers.get("Content-Type") or "").lower()
                if r.status_code == 200 and "text/html" not in ctype:
                    return r.content
                reason = f"http {r.status_code} ctype={ctype}"
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
            if attempt < self._HTTP_RETRIES - 1:
                time.sleep((attempt + 1) * 3)
            else:
                print(f"[{self.site_id}] file download failed ({reason}): {url}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _iso_date(dmy):
        """'14.07.2026' -> '2026-07-14'. Returns None on mismatch."""
        if not dmy:
            return None
        m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", dmy.strip())
        if not m:
            return None
        d, mo, y = m.groups()
        return f"{y}-{mo}-{d}"

    @staticmethod
    def _tag_titles(item, key):
        """Collect the ``title`` strings from item['tags']['metaData'][key]."""
        meta = ((item.get("tags") or {}).get("metaData") or {})
        return [e.get("title") for e in (meta.get(key) or []) if e.get("title")]

    def _published_date(self, item):
        vals = self._tag_titles(item, "תאריך פרסום")  # "publish date"
        return self._iso_date(vals[0]) if vals else None

    @staticmethod
    def _html_to_text(html):
        if not html:
            return ""
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        return re.sub(r"\s+", " ", text).strip()

    def _fetch_content(self, urlname):
        """Fetch the content-page JSON for a page urlName. Returns dict or {}."""
        j = self._get_json(
            self._CONTENT_PAGE.format(urlname=urlname), params={"culture": "he"}
        )
        return j if isinstance(j, dict) else {}

    @staticmethod
    def _extract_files(content):
        """Return [{url, fileName, fileSize, extension}] from a content page."""
        out = []
        sub = (content.get("contentSub") or {})
        ftd = (sub.get("filesToDownload") or {})
        for group in (ftd.get("filesGroupItems") or []):
            for it in (group.get("items") or []):
                if it.get("url"):
                    out.append(it)
        return out

    def _body_text(self, content):
        parts = []
        main = (content.get("contentMain") or {})
        for hc in (main.get("htmlContents") or []):
            txt = self._html_to_text(hc.get("sectionData"))
            if txt:
                parts.append(txt)
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def _list_params(self, mode, office_id, skip):
        """Build the GetResults query params for a (mode, office, page)."""
        common = [
            ("officeId", office_id),
            ("culture", "he"),
            ("skip", str(skip)),
            ("limit", str(self._PAGE_SIZE)),
        ]
        if mode == "news":
            return [("CollectorType", "news"), ("Type", self._NEWS_TYPE)] + common
        # publications = aggregate of several collector types
        params = [("CollectorType", ct) for ct in self._PUB_COLLECTOR_TYPES]
        params += [("Type", self._PUB_TYPE)] + common
        return params

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        deadline = time.monotonic() + self._WALL_CLOCK_BUDGET_SEC
        limit_label = str(limit) if limit is not None else "inf"

        for office_id, label in self.OFFICES:
            if limit is not None and saved >= limit:
                break
            if time.monotonic() > deadline:
                print(f"[{self.site_id}] wall-clock budget exhausted; stopping")
                break

            for mode in ("news", "publications"):
                if limit is not None and saved >= limit:
                    break
                saved = self._crawl_office_mode(
                    office_id, label, mode, saved, limit, seen_urls,
                    deadline, limit_label,
                )

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    def _crawl_office_mode(self, office_id, label, mode, saved, limit,
                           seen_urls, deadline, limit_label):
        skip = 0
        total = None
        page = 0
        while True:
            if limit is not None and saved >= limit:
                break
            if page >= self._PAGE_SAFETY_CAP:
                print(f"[{self.site_id}] {label}/{mode}: page cap reached")
                break
            if time.monotonic() > deadline:
                break

            data = self._get_json(
                self._GET_RESULTS, params=self._list_params(mode, office_id, skip)
            )
            if not isinstance(data, dict):
                break
            if total is None:
                total = data.get("total") or 0
                if total:
                    print(f"[{self.site_id}] {label}/{mode}: total={total}")
            results = data.get("results") or []
            if not results:
                break

            for item in results:
                if limit is not None and saved >= limit:
                    break
                try:
                    if self._process_item(item, label, mode, seen_urls):
                        saved += 1
                        print(f"[{self.site_id}] saved {saved}/{limit_label} "
                              f"[{label}/{mode}]")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item failed "
                          f"({item.get('url')}): {exc}")

            skip += self._PAGE_SIZE
            page += 1
            if skip >= (total or 0):
                break
        return saved

    def _process_item(self, item, label, mode, seen_urls):
        """Persist one collector result. Returns True if a new row was saved."""
        rel_url = (item.get("url") or "").strip()
        title = (item.get("title") or "").strip()
        if not rel_url or not title:
            return False
        if item.get("isExternal") or not rel_url.startswith("/"):
            return False  # off-portal link — skip

        meta_url = "https://www.gov.il" + rel_url
        urlname = rel_url.rstrip("/").split("/")[-1]
        if not urlname or meta_url in seen_urls:
            return False
        seen_urls.add(meta_url)

        # Skip rows already collected (also serves as cross-run dedup).
        if _ldb.find_by_dedup_key(self._conn, self.site_id, urlname, meta_url) is not None:
            return False

        published_date = self._published_date(item)
        office_he = self._tag_titles(item, "משרד")  # "office"
        publisher = office_he[0] if office_he else label
        keywords = ", ".join(self._tag_titles(item, "נושא"))  # "topic"

        content = self._fetch_content(urlname)
        body = self._body_text(content) or (item.get("description") or "").strip()
        files = self._extract_files(content)
        first_file = files[0] if files else None

        paper = {
            "site_id": self.site_id,
            "external_id": urlname,
            "post_number": urlname,
            "title": title,
            "abstract": body,
            "published_date": published_date,
            "posted_date": published_date,
            "url": meta_url,
            "pdf_url": (first_file.get("url") if first_file else None),
            "original_filename": (first_file.get("fileName") if first_file else None),
            "authors": label,
            "publisher": publisher,
            "journal": None,
            "keywords": keywords,
            "category": mode,
            "metadata": None,
        }

        seq_id = self._save_paper(paper)

        # Download the attached document (publications frequently attach one).
        if first_file and str(first_file.get("extension", "")).lower() in self._DOWNLOADABLE_EXT:
            self._download_and_store(seq_id, first_file["url"])

        return True

    def _download_and_store(self, seq_id, file_url):
        """Fetch a document and persist it through the framework blob path."""
        content = self._download_file(file_url)
        if not content:
            return
        try:
            _path, size, sha = _blobs.save_pdf(seq_id, content)
        except (TypeError, ValueError, OSError) as exc:
            print(f"[{self.site_id}] save_pdf failed (seq={seq_id}): {exc}")
            return
        _ldb.update_document_pdf(
            self._conn, seq_id, downloaded=True, size_bytes=size, sha256=sha
        )

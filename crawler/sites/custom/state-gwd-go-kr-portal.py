# -*- coding: utf-8 -*-
"""강원특별자치도 보도자료 (press releases) crawler.

Target:  https://state.gwd.go.kr/portal/briefing/pressRelease/newpressRelease
List:    POST pageIndex=N to the same URL (15 items/page).
Detail:  GET ?seq={seq}  — has title, author, date, HWP attachment URL.
Content: HWP 5.0 OLE2 PrvText stream (preview text, UTF-16LE) → abstract.
"""

import io
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, unquote

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from crawler.base_crawler import BaseCrawler  # noqa: E402

try:
    from bs4 import BeautifulSoup
    _BS4_OK = True
except ImportError:
    _BS4_OK = False

try:
    import olefile as _olefile
    _OLEFILE_OK = True
except ImportError:
    _OLEFILE_OK = False

_BASE = "https://state.gwd.go.kr"
_LIST_URL = f"{_BASE}/portal/briefing/pressRelease/newpressRelease"
_PAGE_SIZE = 15
_MAX_HWP_BYTES = 25 * 1024 * 1024  # skip files > 25 MB


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML — fallback chain: html5lib → lxml → html.parser."""
    if not _BS4_OK:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _text(el) -> str:
    return el.get_text(strip=True) if el else ""


# ---------------------------------------------------------------------------
# HWP PrvText extractor
# ---------------------------------------------------------------------------

def _extract_prvtext(hwp_bytes: bytes) -> str:
    """Extract the PrvText preview stream from an HWP 5.0 OLE2 binary.

    PrvText is a UTF-16LE plain-text summary that HWP writers embed for
    accessibility viewers — it reliably contains the full body text even
    for image-heavy documents.
    """
    if not _OLEFILE_OK or not hwp_bytes:
        return ""
    try:
        buf = io.BytesIO(hwp_bytes)
        ole = _olefile.OleFileIO(buf)
        if not ole.exists("PrvText"):
            return ""
        raw = ole.openstream("PrvText").read()
        text = raw.decode("utf-16-le", errors="replace")
        # Clean null bytes, fix line endings, collapse excessive blank lines
        text = text.replace("\x00", "")
        text = re.sub(r"\r\n|\r", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        return text.strip()
    except Exception as exc:
        return ""


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class StateGwdGoKrPortalCrawler(BaseCrawler):
    """강원특별자치도 보도자료 (press release) crawler."""

    site_id = "state-gwd-go-kr-portal"
    site_name = "Custom: state-gwd-go-kr-portal"
    base_url = _BASE

    # ------------------------------------------------------------------
    # curl helpers (TLS workaround common on Korean gov sites)
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, retries: int = 3) -> str | None:
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", f"Referer: {_LIST_URL}",
            url,
        ]
        for attempt in range(retries):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=35)
                if r.stdout and r.stdout.strip():
                    return r.stdout.decode("utf-8", errors="replace")
            except Exception:
                pass
            if attempt < retries - 1:
                wait = (2 ** attempt)
                time.sleep(wait)
        return None

    def _curl_post(self, url: str, form: dict, retries: int = 3) -> str | None:
        data_args = []
        for k, v in form.items():
            data_args += ["-d", f"{k}={v}"]
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-X", "POST",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", f"Referer: {_LIST_URL}",
        ] + data_args + [url]
        for attempt in range(retries):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=35)
                if r.stdout and r.stdout.strip():
                    return r.stdout.decode("utf-8", errors="replace")
            except Exception:
                pass
            if attempt < retries - 1:
                wait = (2 ** attempt)
                time.sleep(wait)
        return None

    def _download_bytes(self, url: str, retries: int = 3) -> bytes | None:
        """Download binary (HWP file) via curl; returns None if > _MAX_HWP_BYTES."""
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "60",
            "--max-filesize", str(_MAX_HWP_BYTES),
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        for attempt in range(retries):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=65)
                if r.stdout:
                    return r.stdout
            except Exception:
                pass
            if attempt < retries - 1:
                wait = (2 ** attempt)
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # List + detail page parsing
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page: int) -> list[dict] | None:
        """Fetch one list page; returns [] at end-of-list, None on error."""
        raw = self._curl_post(_LIST_URL, {"pageIndex": str(page)})
        if not raw:
            return None
        soup = _make_soup(raw)
        if not soup:
            return None

        items = []
        for row in soup.select("tr"):
            a = row.find("a", href=lambda h: h and "seq=" in (h or ""))
            if not a:
                continue
            href = a.get("href", "")
            m = re.search(r"seq=(\d+)", href)
            if not m:
                continue

            seq = m.group(1)
            tds = row.find_all("td")
            display_num = _text(tds[0]) if tds else ""
            title = _text(a)
            dept_td = row.find("td", class_="skinTb-part")
            date_td = row.find("td", class_="skinTb-date")

            items.append({
                "seq": seq,
                "display_num": display_num,
                "title": title,
                "department": _text(dept_td),
                "listed_date": _text(date_td),
                "url": urljoin(_BASE, href),
            })

        return items

    def _fetch_detail(self, seq: str) -> dict | None:
        """Fetch detail page and return author, published_date, attachments."""
        raw = self._curl_get(f"{_LIST_URL}?seq={seq}")
        if not raw:
            return None
        soup = _make_soup(raw)
        if not soup:
            return None

        # Focus on #content-bx to avoid nav-bar false-positives
        root = soup.find(id="content-bx") or soup

        author = _text(root.find(class_="skinTb-name"))
        published_date = _text(root.find(class_="skinTb-date"))

        attachments = []
        attach_div = root.find(class_="attachFile")
        if attach_div:
            for a in attach_div.find_all("a", href=lambda h: h and "/download" in (h or "")):
                raw_name = _text(a)
                # Strip "(다운로드: N)" suffix
                fname = re.sub(r"\s*\(다운로드[^)]*\)", "", raw_name).strip()
                attachments.append({
                    "url": urljoin(_BASE, a["href"]),
                    "filename": fname,
                })

        return {
            "author": author,
            "published_date": published_date,
            "attachments": attachments,
        }

    # ------------------------------------------------------------------
    # Per-item processing
    # ------------------------------------------------------------------

    def _process_item(self, list_item: dict) -> bool:
        """Fetch detail + HWP, build abstract, save.  Returns True if saved."""
        seq = list_item["seq"]
        title = list_item["title"]

        detail = self._fetch_detail(seq)
        if not detail:
            print(f"[{self.site_id}] Detail fetch failed for seq={seq}, skipping.")
            return False

        published_date = detail.get("published_date") or list_item["listed_date"]
        listed_date = list_item["listed_date"]
        author = detail.get("author") or list_item["department"]
        attachments = detail.get("attachments", [])

        # Pick first HWP attachment (prefer .hwp extension)
        hwp_att = None
        for att in attachments:
            if att["filename"].lower().endswith(".hwp"):
                hwp_att = att
                break
        if hwp_att is None and attachments:
            hwp_att = attachments[0]

        hwp_url = hwp_att["url"] if hwp_att else None
        original_filename = hwp_att["filename"] if hwp_att else None

        # Download HWP and extract abstract via PrvText
        abstract = ""
        if hwp_url:
            hwp_bytes = self._download_bytes(hwp_url)
            if hwp_bytes:
                abstract = _extract_prvtext(hwp_bytes)

        # Fallback: title (often < 50 chars → item will be skipped below)
        if not abstract:
            abstract = title

        if len(abstract) < 50:
            print(f"[{self.site_id}] seq={seq}: abstract too short ({len(abstract)} chars), skipping.")
            return False

        paper = {
            "site_id": self.site_id,
            "external_id": seq,
            "post_number": seq,      # used by libertree for incremental MAX()
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": listed_date,   # listed/posted date
            "url": list_item["url"],      # → meta_url via adapter
            "pdf_url": hwp_url,
            "authors": "",
            "publisher": author,
            "department": list_item["department"],
            "original_filename": original_filename,
            "keywords": "",
            "category": "보도자료",
            "doi": "",
            "metadata": json.dumps({
                "seq": seq,
                "display_num": list_item.get("display_num"),
                "posted_date": listed_date,
                "originalFilename": original_filename,
            }, ensure_ascii=False),
        }
        self._save_paper(paper)
        return True

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls: set[str] = set()
        start_time = time.time()
        max_pages = 200

        while True:
            # --- exit conditions ---
            if limit is not None and saved >= limit:
                break
            if page > max_pages:
                print(f"[{self.site_id}] Safety cap of {max_pages} pages reached. Stopping.")
                break
            if time.time() - start_time > 25 * 60:
                print(f"[{self.site_id}] 25-minute wall-clock budget exceeded. Stopping.")
                break

            # --- progress log every 10 pages ---
            if page > 1 and (page - 1) % 10 == 0:
                limit_str = str(limit) if limit is not None else "∞"
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            # --- fetch list page (with retry) ---
            items = None
            for _r in range(3):
                items = self._fetch_list_page(page)
                if items is not None:
                    break
                wait = (_r + 1) * 3
                print(f"[{self.site_id}] Page {page} fetch failed, retry in {wait}s…")
                time.sleep(wait)

            if items is None:
                print(f"[{self.site_id}] Page {page}: giving up after 3 retries. Stopping.")
                break
            if not items:
                print(f"[{self.site_id}] No items on page {page}. End of list.")
                break

            # --- deduplication ---
            new_items = [i for i in items if i["url"] not in seen_urls]
            if not new_items and page > 1:
                print(f"[{self.site_id}] Page {page}: all items already seen. Stopping.")
                break
            for i in new_items:
                seen_urls.add(i["url"])

            # --- process each item ---
            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                try:
                    ok = self._process_item(item)
                    if ok:
                        saved += 1
                        limit_str = str(limit) if limit is not None else "∞"
                        print(f"[{self.site_id}] Saved {saved}/{limit_str}: {item['title'][:60]}")
                    time.sleep(self._delay)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] Item seq={item.get('seq')} failed: {exc}")
                    continue

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

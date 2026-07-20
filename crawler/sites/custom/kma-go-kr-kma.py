# -*- coding: utf-8 -*-
"""기상청 보도자료 (본청) crawler.

Starting URL reference:
  https://www.kma.go.kr/kma/news/press_01.jsp?from=2022-10-06&to=2023-10-06&field=subject&text=

List URL pattern (no date filter → all articles, most recent first):
  https://www.kma.go.kr/kma/news/press_01.jsp?mode=list&bid=press&page={n}

Detail URL pattern:
  https://www.kma.go.kr/kma/news/press_01.jsp?bid=press&mode=view&num={num}&page={n}

Note: The site enforces a ~3-year lookback window on date-filtered queries.
      Running without a date filter returns all available articles (most recent first)
      and avoids that limitation.
"""

import json
import re
import subprocess
import time

# Absolute import required (no package context via spec_from_file_location).
from crawler.base_crawler import BaseCrawler

_BASE = "https://www.kma.go.kr"
_LIST_JSP = f"{_BASE}/kma/news/press_01.jsp"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _curl_get(url: str) -> bytes | None:
    """GET via curl with TLS workaround. Returns raw bytes or None on failure."""
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
        "-H", f"User-Agent: {_UA}",
        "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
        "-H", f"Referer: {_LIST_JSP}",
        url,
    ]
    for attempt in range(3):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            if result.returncode == 0 and result.stdout:
                return result.stdout
        except Exception:
            pass
        time.sleep(3 ** attempt)
    return None


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _make_soup(raw):
    """Parse HTML with html5lib → lxml → html.parser fallback."""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = raw
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(text, parser)
        except Exception:
            continue
    return None


def _strip_html(html_str: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", html_str)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z0-9#]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _norm_date(raw: str) -> str:
    """Normalize YYYY/MM/DD or YYYYMMDD → YYYY-MM-DD."""
    if not raw:
        return ""
    raw = raw.strip()
    m = re.match(r"(\d{4})[/.\-](\d{2})[/.\-](\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    if re.match(r"\d{8}$", raw):
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def _build_abstract(view_body, filedown, title: str) -> str:
    """Build abstract from page sections with multiple fallbacks.

    For recent articles: body text is rich HTML → good abstract.
    For older articles: content is a single JPG image → fall back to
    descriptive Korean filenames in the file-download section.
    """
    # 1. Primary: body HTML stripped to plain text.
    body_text = ""
    if view_body:
        body_text = _strip_html(str(view_body))

    # 2. Alt texts from images in body (often contain Korean descriptive filenames).
    alt_parts = []
    if view_body and len(body_text) < 100:
        for img in view_body.find_all("img"):
            alt = img.get("alt", "").strip()
            if alt and "공공누리" not in alt and len(alt) > 10:
                alt_parts.append(alt)

    # 3. File-download section spans carry Korean descriptive filenames like
    #    "20231005_보도자료_내년 세계기상기구 달력에 국내 사진 2점 수록된다.hwpx (크기:21MB , 다운로드:367)".
    file_parts = []
    if filedown and len(body_text) < 100:
        for span in filedown.find_all("span"):
            txt = span.get_text(strip=True)
            if txt and len(txt) > 10 and "첨부파일" not in txt:
                file_parts.append(txt)

    parts = []
    if body_text:
        parts.append(body_text)
    parts.extend(alt_parts)
    parts.extend(file_parts)
    abstract = " ".join(parts).strip()

    # 4. Last resort: prepend the title.
    if len(abstract) < 50:
        abstract = (title + " " + abstract).strip() if abstract else title

    return abstract


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class KMAPressCrawler(BaseCrawler):
    """기상청 보도자료 (본청) crawler."""

    site_id = "kma-go-kr-kma"
    site_name = "Custom: kma-go-kr-kma"
    base_url = "https://www.kma.go.kr"

    def crawl(self, limit=None, date_from: str = "", date_to: str = ""):
        """Crawl KMA press releases (본청).

        Parameters
        ----------
        limit:
            Maximum records to save. None = unlimited.
        date_from / date_to:
            Optional ISO date strings (YYYY-MM-DD). When omitted the site
            returns all available articles (most recent first, up to its
            ~3-year lookback window).
        """
        start_ts = time.time()
        MAX_WALL_MIN = 24
        MAX_PAGES = 200

        saved = 0
        seen_urls: set = set()
        page = 1
        limit_display = str(limit) if limit is not None else "∞"

        while True:
            # --- time budget ---
            if (time.time() - start_ts) / 60.0 > MAX_WALL_MIN:
                print(f"[{self.site_id}] Wall-clock budget exceeded. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page > MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {MAX_PAGES} pages reached. Stopping.")
                break

            if page == 1 or page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_display}")

            # --- build list URL ---
            list_url = f"{_LIST_JSP}?mode=list&bid=press&page={page}"
            if date_from:
                list_url += f"&from={date_from}"
            if date_to:
                list_url += f"&to={date_to}"

            # --- fetch list page with retry ---
            raw = None
            for attempt in range(3):
                raw = _curl_get(list_url)
                if raw:
                    break
                wait = attempt + 1
                print(f"[{self.site_id}] list page {page} fetch failed (attempt {attempt + 1}), retry in {wait}s…")
                time.sleep(wait)

            if not raw:
                print(f"[{self.site_id}] Could not fetch list page {page}. Stopping.")
                break

            soup = _make_soup(raw)
            if not soup:
                print(f"[{self.site_id}] Could not parse list page {page}. Stopping.")
                break

            tbody = soup.find("tbody")
            if not tbody:
                print(f"[{self.site_id}] No <tbody> on page {page}. Stopping.")
                break

            rows = tbody.find_all("tr")
            if not rows:
                print(f"[{self.site_id}] No rows on page {page}. Done.")
                break

            # Detect empty-result sentinel row.
            if len(rows) == 1:
                td_text = rows[0].get_text()
                if "없습니다" in td_text or "No result" in td_text.lower():
                    print(f"[{self.site_id}] No results on page {page}. Done.")
                    break

            new_on_page = 0

            for row in rows:
                if limit is not None and saved >= limit:
                    break

                try:
                    # Find title link.
                    title_td = row.find("td", class_=lambda c: c and "tal_l" in c)
                    if not title_td:
                        continue

                    a_tag = title_td.find("a", href=True)
                    if not a_tag:
                        continue

                    href = a_tag["href"]
                    num_m = re.search(r"[?&]num=(\d+)", href)
                    if not num_m:
                        continue
                    num = num_m.group(1)

                    detail_url = (
                        f"{_LIST_JSP}?bid=press&mode=view&num={num}&page={page}"
                    )
                    if date_from:
                        detail_url += f"&from={date_from}"
                    if date_to:
                        detail_url += f"&to={date_to}"

                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_on_page += 1

                    title_text = a_tag.get_text(strip=True)

                    # Date and department from list row.
                    tds = row.find_all("td")
                    listed_date = ""
                    for td in tds:
                        txt = td.get_text(strip=True)
                        if re.match(r"\d{4}/\d{2}/\d{2}", txt):
                            listed_date = _norm_date(txt)
                            break

                    dept = ""
                    for td in tds:
                        classes = td.get("class") or []
                        if "m_h" in classes:
                            txt = td.get_text(strip=True)
                            if txt and not re.match(r"^\d+$", txt) and "파일" not in txt:
                                dept = txt
                                break

                    # PDF link from list row.
                    pdf_url = None
                    original_filename = None
                    for a in row.find_all("a", href=True):
                        ah = a["href"]
                        if "NeoboardProcess" not in ah:
                            continue
                        k_m = re.search(r"[&?]k=([^&\"']+)", ah)
                        if k_m and k_m.group(1).lower().endswith(".pdf"):
                            pdf_url = (_BASE + ah) if ah.startswith("/") else ah
                            original_filename = k_m.group(1)
                            break

                    # --- fetch detail page ---
                    time.sleep(self._delay)
                    detail_raw = None
                    for attempt in range(3):
                        detail_raw = _curl_get(detail_url)
                        if detail_raw:
                            break
                        wait = 3 ** attempt
                        print(f"[{self.site_id}] detail {num} fetch failed (attempt {attempt + 1}), retry in {wait}s…")
                        time.sleep(wait)

                    if not detail_raw:
                        print(f"[{self.site_id}] item {num} failed: detail fetch failed. Skipping.")
                        continue

                    detail_soup = _make_soup(detail_raw)
                    if not detail_soup:
                        print(f"[{self.site_id}] item {num} failed: parse error. Skipping.")
                        continue

                    view_body = detail_soup.find(class_="bbs_view_body")
                    filedown = detail_soup.find(class_="bbs_view_filedown")
                    abstract = _build_abstract(view_body, filedown, title_text)

                    if len(abstract) < 50:
                        print(f"[{self.site_id}] item {num}: abstract too short ({len(abstract)} chars). Skipping.")
                        continue

                    # Date from detail if missing.
                    if not listed_date:
                        writer = detail_soup.find("span", class_="writer")
                        if writer:
                            for em in writer.find_all("em"):
                                txt = em.get_text(strip=True)
                                if re.match(r"\d{4}/\d{2}/\d{2}", txt):
                                    listed_date = _norm_date(txt)
                                    break

                    # PDF from detail if not from list.
                    if not pdf_url:
                        for a in detail_soup.find_all("a", href=True):
                            ah = a["href"]
                            if "NeoboardProcess" not in ah:
                                continue
                            k_m = re.search(r"[&?]k=([^&\"']+)", ah)
                            if k_m and k_m.group(1).lower().endswith(".pdf"):
                                pdf_url = (_BASE + ah) if ah.startswith("/") else ah
                                original_filename = k_m.group(1)
                                break

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": num,
                        "title": title_text,
                        "abstract": abstract,
                        "published_date": listed_date,
                        "listed_date": listed_date,
                        "posted_date": listed_date,
                        "url": detail_url,
                        "pdf_url": pdf_url or "",
                        "doi": "",
                        "authors": "",
                        "publisher": "기상청",
                        "department": dept,
                        "journal": "",
                        "keywords": "",
                        "category": "보도자료",
                        "original_filename": original_filename or "",
                        "metadata": json.dumps(
                            {
                                "posted_date": listed_date,
                                "num": num,
                                "board_id": "press",
                                "department": dept,
                                "originalFilename": original_filename or "",
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_display}: {title_text[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item failed: {exc}. Continuing.")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] No new items on page {page}. Done.")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

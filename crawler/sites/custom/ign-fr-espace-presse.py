# -*- coding: utf-8 -*-
"""IGN (Institut national de l'information géographique et forestière)
Espace presse crawler.

Target: https://www.ign.fr/espace-presse
Structure: Drupal 10 HTML listing (~47 items on main page + ~21 in archives);
           no JSON API, no real pagination. Detail pages contain body text,
           a published-date meta tag, optional PDF attachment, and Drupal
           node ID embedded in inline JSON.
"""

import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

# ---------------------------------------------------------------------------
# BeautifulSoup with parser fallback chain
# ---------------------------------------------------------------------------
try:
    from bs4 import BeautifulSoup as _BS

    def _make_soup(html: str):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return _BS(html, parser)
            except Exception:
                continue
        return None

except ImportError:
    def _make_soup(html: str):  # type: ignore[misc]
        return None


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------
_LISTING_URLS = [
    "https://www.ign.fr/espace-presse",
    "https://www.ign.fr/espace-presse/communiques-de-presse-archives",
]
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_SKIP_PATHS = {"/espace-presse", "/institut/espace-presse"}


# ---------------------------------------------------------------------------
# curl helper
# ---------------------------------------------------------------------------

def _curl_get(url: str, timeout: int = 30) -> str | None:
    """Fetch *url* via curl with 3-attempt exponential backoff.

    Returns decoded text, or None after all retries fail.
    """
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", f"User-Agent: {_UA}",
        "-H", "Accept: text/html,*/*;q=0.8",
        "-H", "Accept-Language: fr-FR,fr;q=0.9,en;q=0.8",
        url,
    ]
    for attempt in range(3):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            text = result.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
        except Exception:
            pass
        if attempt < 2:
            wait = (attempt + 1) ** 2  # 1s, 4s
            time.sleep(wait)
    return None


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_date(raw: str) -> str:
    """Parse French Drupal date strings → ISO YYYY-MM-DD.

    Handles: 'lun 13/04/2026 - 16:52', 'jeu 12/03/2026 - 17:19', etc.
    """
    if not raw:
        return ""
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", raw)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return m.group(1)
    return ""


def _unescape(text: str) -> str:
    """Decode common HTML entities."""
    return (
        text.replace("&#039;", "'")
            .replace("&amp;", "&")
            .replace("&quot;", '"')
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&nbsp;", " ")
    )


def _extract_links(html: str) -> list[str]:
    """Return unique article paths from a listing page HTML."""
    raw = re.findall(r'href="(/(?:institut/)?espace-presse/[^"?#]+)"', html)
    seen: set[str] = set()
    result: list[str] = []
    for path in raw:
        if path in seen or path in _SKIP_PATHS:
            continue
        if "communiques-de-presse-archives" in path:
            continue
        seen.add(path)
        result.append(path)
    return result


def _extract_node_id(html: str) -> str | None:
    """Extract Drupal node ID from inline settings JSON."""
    m = re.search(r'"currentPath"\s*:\s*"node/(\d+)"', html)
    return m.group(1) if m else None


def _extract_abstract(html: str) -> str:
    """Extract body text from the main article area."""
    soup = _make_soup(html)
    if soup:
        main = soup.find("main") or soup.find("article")
        if main:
            for tag in main.find_all(["nav", "header", "footer", "script", "style", "form"]):
                tag.decompose()
            texts = []
            for p in main.find_all("p"):
                t = re.sub(r"\s+", " ", p.get_text(separator=" ", strip=True))
                if len(t) > 20:
                    texts.append(t)
            if texts:
                return "\n\n".join(texts)

    # Regex fallback
    main_m = re.search(r"<main[^>]*>(.*?)</main>", html, re.DOTALL)
    if not main_m:
        main_m = re.search(r"<article[^>]*>(.*?)</article>", html, re.DOTALL)
    if main_m:
        raw = main_m.group(1)
        paras = re.findall(r"<p[^>]*>(.*?)</p>", raw, re.DOTALL)
        texts = []
        for p in paras:
            t = re.sub(r"<[^>]+>", " ", p)
            t = re.sub(r"&[a-z]+;", " ", t)
            t = re.sub(r"\s+", " ", t).strip()
            if len(t) > 20:
                texts.append(t)
        if texts:
            return "\n\n".join(texts)
    return ""


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class IGNEspacePresseCrawler(BaseCrawler):
    site_id = "ign-fr-espace-presse"
    site_name = "Custom: ign-fr-espace-presse"
    base_url = "https://www.ign.fr"

    def crawl(self, limit=None):
        """Crawl IGN Espace presse and save items to the database.

        Collects article URLs from the main listing and the archives page,
        then fetches each detail page. Skips items whose abstract is shorter
        than 100 characters. Honours *limit* for any value.
        """
        saved = 0
        limit_str = str(limit) if limit is not None else "∞"
        start_time = time.time()
        max_seconds = 25 * 60

        # ---- Phase 1: collect article URLs from listing pages --------------
        all_article_urls: list[str] = []
        seen_paths: set[str] = set()

        for listing_url in _LISTING_URLS:
            html = _curl_get(listing_url)
            if not html:
                print(f"[{self.site_id}] Failed to fetch listing: {listing_url}")
                continue
            links = _extract_links(html)
            added = 0
            for path in links:
                if path not in seen_paths:
                    seen_paths.add(path)
                    all_article_urls.append(f"https://www.ign.fr{path}")
                    added += 1
            print(f"[{self.site_id}] Listing {listing_url}: {added} new links")

        print(f"[{self.site_id}] Total unique articles: {len(all_article_urls)}")

        if not all_article_urls:
            print(f"[{self.site_id}] No articles found — aborting.")
            return 0

        # ---- Phase 2: fetch detail pages -----------------------------------
        seen_detail: set[str] = set()

        for idx, article_url in enumerate(all_article_urls):
            if limit is not None and saved >= limit:
                break

            if time.time() - start_time > max_seconds:
                print(f"[{self.site_id}] 25-minute budget reached, stopping early.")
                break

            if article_url in seen_detail:
                continue
            seen_detail.add(article_url)

            if idx > 0 and idx % 10 == 0:
                print(
                    f"[{self.site_id}] page {idx}: "
                    f"saved {saved}/{limit_str}"
                )

            time.sleep(self._delay)

            try:
                html = _curl_get(article_url)
                if not html:
                    print(f"[{self.site_id}] item {article_url} failed: empty response")
                    continue

                # Title
                title_m = re.search(
                    r'<meta property="og:title" content="([^"]+)"', html
                )
                title = _unescape(title_m.group(1)) if title_m else ""
                if not title:
                    t_m = re.search(r"<title>([^<]+)</title>", html)
                    if t_m:
                        title = _unescape(t_m.group(1).split(" - IGN")[0].strip())
                if not title:
                    print(f"[{self.site_id}] item {article_url} failed: no title")
                    continue

                # Node ID
                node_id = _extract_node_id(html)
                slug = article_url.rstrip("/").split("/")[-1]
                external_id = node_id or slug
                post_number = node_id  # numeric string when available

                # Dates
                pub_m = re.search(
                    r'article:published_time[^>]+content="([^"]+)"', html
                )
                pub_raw = pub_m.group(1) if pub_m else ""
                published_date = _parse_date(pub_raw)

                mod_m = re.search(
                    r'article:modified_time[^>]+content="([^"]+)"', html
                )
                mod_raw = mod_m.group(1) if mod_m else ""

                # Abstract
                abstract = _extract_abstract(html)
                if len(abstract) < 100:
                    print(
                        f"[{self.site_id}] abstract too short "
                        f"({len(abstract)} chars) for {article_url}, skipping"
                    )
                    continue

                # PDF
                local_pdfs = re.findall(r'href="(/files/[^"]*\.pdf)"', html, re.I)
                ext_pdfs = re.findall(r'href="(https?://[^"]*\.pdf)"', html, re.I)
                pdf_url: str | None = None
                original_filename: str | None = None
                if local_pdfs:
                    pdf_url = f"https://www.ign.fr{local_pdfs[0]}"
                    original_filename = local_pdfs[0].split("/")[-1]
                elif ext_pdfs:
                    pdf_url = ext_pdfs[0]
                    original_filename = ext_pdfs[0].split("/")[-1]

                # Category hint from link text on listing pages
                cat_m = re.search(
                    r"Communiqué de presse|Dossier de presse|alerte presse|actu presse",
                    html, re.I,
                )
                category = cat_m.group(0) if cat_m else "Espace presse"

                paper = {
                    "site_id": self.site_id,
                    "external_id": external_id,
                    "post_number": post_number,
                    "title": title,
                    "abstract": abstract,
                    "published_date": published_date,
                    "listed_date": published_date,
                    "url": article_url,
                    "pdf_url": pdf_url,
                    "original_filename": original_filename,
                    "publisher": (
                        "IGN - Institut national de l'information "
                        "géographique et forestière"
                    ),
                    "authors": None,
                    "department": None,
                    "journal": None,
                    "keywords": None,
                    "category": category,
                    "doi": None,
                    "metadata": json.dumps(
                        {
                            "posted_date": pub_raw,
                            "modified_date": mod_raw,
                            "node_id": node_id,
                            "slug": slug,
                            "originalFilename": original_filename,
                            "all_pdfs": (
                                [f"https://www.ign.fr{p}" for p in local_pdfs]
                                + ext_pdfs
                            ),
                        },
                        ensure_ascii=False,
                    ),
                }

                self._save_paper(paper)
                saved += 1
                print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {article_url} failed: {exc}")
                continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

# -*- coding: utf-8 -*-
"""Crawler for agriculture.gouv.fr - Recherche, développement et innovation.

Listing URL (paginated):
  https://agriculture.gouv.fr/espace-presse?query=&sort_by=created
    &thematiques=recherche-developpement-et-innovation&pg={pg}

Each page shows ~4 DSFR card components (fr-card) with:
  - fr-card__link  → article href + title
  - fr-card__desc  → excerpt
  - fr-card__date  → listed date (French: "DD mois YYYY")
  - fr-card__detail → date + article type ("Communiqué de presse", etc.)

Detail page: full body text, fr-tag keywords, PDF attachments.
"""

import json
import os
import re
import subprocess
import time
from datetime import datetime

from crawler.base_crawler import BaseCrawler

_SITE_ID = "agriculture-gouv-fr-recherche-developpem"
_BASE_URL = "https://agriculture.gouv.fr"
_LIST_URL = (
    "https://agriculture.gouv.fr/espace-presse"
    "?query=&sort_by=created"
    "&thematiques=recherche-developpement-et-innovation"
    "&pg={pg}"
)
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))  # safety cap
_WALL_BUDGET_S = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

_MOIS = {
    "janvier": "01", "février": "02", "fevrier": "02", "mars": "03",
    "avril": "04", "mai": "05", "juin": "06", "juillet": "07",
    "août": "08", "aout": "08", "septembre": "09", "octobre": "10",
    "novembre": "11", "décembre": "12", "decembre": "12",
}


_MARKER = "__HTTP_CODE__:"


def _curl_get(url: str, retries: int = 4) -> str | None:
    """Fetch URL via curl; return decoded text or None after all retries.

    Site nginx layer intermittently rate-limits (HTTP 429) and returns a
    generic non-empty error page instead of the real listing, which used to
    be mistaken for a legit "no results" page. Detect the status code via
    curl's -w and retry on 429/5xx instead of accepting the body as-is.
    """
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
        "-H", "Accept-Language: fr-FR,fr;q=0.9,en;q=0.8",
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "-w", f"\n{_MARKER}%{{http_code}}",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            raw = result.stdout.decode("utf-8", errors="replace")
            status = "000"
            if _MARKER in raw:
                raw, _, status = raw.rpartition(_MARKER)
                status = status.strip()
            if raw.strip() and status not in ("429",) and not status.startswith("5"):
                return raw
            print(
                f"[{_SITE_ID}] fetch retry ({attempt + 1}/{retries}) for {url}: "
                f"http={status} body_len={len(raw.strip())}"
            )
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error ({attempt + 1}/{retries}): {exc}")
        if attempt < retries - 1:
            wait = 2 * (2 ** attempt)  # 2, 4, 8, 16 seconds
            print(f"[{_SITE_ID}] retrying in {wait}s...")
            time.sleep(wait)
    return None


def _make_soup(html: str | bytes):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    if isinstance(html, str):
        raw = html.encode("utf-8", errors="replace")
    else:
        raw = html
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    from bs4 import BeautifulSoup
    return BeautifulSoup(raw, "html.parser")


def _parse_fr_date(text: str) -> str:
    """'13 mai 2026' → '2026-05-13'. Returns '' on failure."""
    if not text:
        return ""
    m = re.match(r"(\d{1,2})\s+(\w+)\s+(\d{4})", text.strip().lower())
    if not m:
        return ""
    day, mois_str, year = m.group(1), m.group(2), m.group(3)
    mo = _MOIS.get(mois_str, "")
    if not mo:
        return ""
    return f"{year}-{mo}-{int(day):02d}"


class AgricultureGouvFrCrawler(BaseCrawler):
    """Crawler for agriculture.gouv.fr recherche-developpement-et-innovation."""

    site_id = _SITE_ID
    site_name = "Custom: agriculture-gouv-fr-recherche-developpem"
    base_url = _BASE_URL

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.monotonic()
        limit_or_inf = limit if limit is not None else "∞"

        for pg in range(1, _MAX_PAGES + 1):
            # Wall-clock budget
            elapsed = time.monotonic() - start_time
            if elapsed > _WALL_BUDGET_S:
                print(f"[{_SITE_ID}] Wall-clock budget exceeded ({elapsed:.0f}s). Exiting.")
                break

            if limit is not None and saved >= limit:
                break

            if pg % 10 == 0:
                print(f"[{_SITE_ID}] page {pg}: saved {saved}/{limit_or_inf}")

            # --- fetch listing page ---
            list_raw = _curl_get(_LIST_URL.format(pg=pg))
            if not list_raw:
                print(f"[{_SITE_ID}] Failed to fetch listing page {pg}. Stopping.")
                break

            try:
                list_soup = _make_soup(list_raw)
            except Exception as exc:
                print(f"[{_SITE_ID}] HTML parse error on listing page {pg}: {exc}. Skipping.")
                continue

            # --- parse article cards ---
            # Article cards are identified by: div.fr-card__body that contains
            # both a fr-card__link AND a fr-card__date span.
            card_items = []
            for body_div in list_soup.find_all("div", class_="fr-card__body"):
                a_tag = body_div.find("a", class_="fr-card__link")
                date_el = body_div.find("span", class_="fr-card__date")
                if not a_tag or not date_el:
                    continue

                href = a_tag.get("href", "")
                if not href.startswith("/") or len(href) < 10:
                    continue
                # Skip system/filter links
                if any(x in href for x in ["espace-presse", "mots-cles", "thematiques", "?", "#"]):
                    continue

                full_url = _BASE_URL + href
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)

                title = a_tag.get_text(strip=True)
                listed_date_raw = date_el.get_text(strip=True)
                listed_date = _parse_fr_date(listed_date_raw)

                # Article type from detail line (everything after the date span)
                detail_el = body_div.find("p", class_="fr-card__detail")
                category = ""
                if detail_el:
                    detail_text = detail_el.get_text(strip=True)
                    if listed_date_raw:
                        detail_text = detail_text.replace(listed_date_raw, "").strip()
                    category = detail_text

                desc_el = body_div.find("p", class_="fr-card__desc")
                card_desc = desc_el.get_text(strip=True) if desc_el else ""

                slug = href.strip("/").split("/")[-1]

                card_items.append({
                    "url": full_url,
                    "slug": slug,
                    "title": title,
                    "listed_date": listed_date,
                    "listed_date_raw": listed_date_raw,
                    "category": category,
                    "card_desc": card_desc,
                })

            if not card_items:
                print(f"[{_SITE_ID}] No new cards on page {pg}. Pagination complete.")
                break

            # --- process each card ---
            for card in card_items:
                if limit is not None and saved >= limit:
                    break

                try:
                    time.sleep(self._delay)
                    detail_raw = _curl_get(card["url"])
                    if not detail_raw:
                        print(f"[{_SITE_ID}] item {card['url']} failed: empty response. Skipping.")
                        continue

                    try:
                        detail_soup = _make_soup(detail_raw)
                    except Exception as exc:
                        print(f"[{_SITE_ID}] item {card['url']} HTML parse failed: {exc}. Skipping.")
                        continue

                    # Full title from h1
                    h1 = detail_soup.find("h1")
                    full_title = h1.get_text(strip=True) if h1 else card["title"]
                    if not full_title:
                        full_title = card["title"]

                    # Meta description for abstract seed
                    meta_desc = ""
                    meta_el = detail_soup.find("meta", attrs={"name": "description"})
                    if meta_el:
                        meta_desc = meta_el.get("content", "").strip()

                    # Full article body text
                    main_el = (
                        detail_soup.find("main")
                        or detail_soup.find(id="content")
                        or detail_soup
                    )
                    # Remove noisy sub-elements
                    for noise in main_el.find_all(["nav", "header", "aside", "script", "style", "form"]):
                        noise.decompose()

                    article_el = main_el.find("article")
                    body_source = article_el if article_el else main_el
                    # Remove share/social sub-elements
                    for el in body_source.find_all(
                        True,
                        class_=lambda c: c and any(
                            x in " ".join(c) for x in ["share", "partager", "social", "breadcrumb"]
                        ),
                    ):
                        el.decompose()

                    raw_paras = [
                        p.get_text(strip=True)
                        for p in body_source.find_all("p")
                        if p.get_text(strip=True)
                    ]
                    # Filter out navigation / boilerplate short strings
                    body_paras = [
                        p for p in raw_paras
                        if len(p) >= 40 and not p.lower().startswith("partager")
                    ]

                    # Build abstract: meta desc first, then body paragraphs
                    abstract_parts: list[str] = []
                    if meta_desc and len(meta_desc) >= 50:
                        abstract_parts.append(meta_desc)
                    for para in body_paras:
                        if para not in abstract_parts:
                            abstract_parts.append(para)
                            if len("\n\n".join(abstract_parts)) >= 800:
                                break

                    abstract = "\n\n".join(abstract_parts)

                    if len(abstract) < 50:
                        print(
                            f"[{_SITE_ID}] item {card['url']} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    # Keywords from fr-tag elements
                    kw_list = []
                    for tag_el in detail_soup.find_all("a", class_="fr-tag"):
                        kw = tag_el.get_text(strip=True)
                        if kw and kw not in kw_list:
                            kw_list.append(kw)
                    keywords = ",".join(kw_list)

                    # Published date: first fr-card__date in article
                    pub_date = card["listed_date"]
                    date_spans = detail_soup.find_all("span", class_="fr-card__date")
                    if date_spans:
                        parsed = _parse_fr_date(date_spans[0].get_text(strip=True))
                        if parsed:
                            pub_date = parsed

                    # PDF / attachment links
                    pdf_url = None
                    original_filename = None
                    for a in detail_soup.find_all("a", href=True):
                        href_val = a["href"]
                        if ".pdf" in href_val.lower() or "document_administratif" in href_val:
                            pdf_url = href_val
                            fn_m = re.search(r"/([^/?#]+\.pdf)", href_val, re.IGNORECASE)
                            if fn_m:
                                original_filename = fn_m.group(1)
                            break

                    paper = {
                        "site_id": self.site_id,
                        "external_id": card["slug"],
                        "post_number": card["slug"],
                        "title": full_title,
                        "abstract": abstract,
                        "published_date": pub_date,
                        "listed_date": card["listed_date"],
                        "url": card["url"],
                        "pdf_url": pdf_url or "",
                        "original_filename": original_filename,
                        "authors": "",
                        "publisher": "Ministère de l'Agriculture et de la Souveraineté alimentaire",
                        "department": "",
                        "journal": "",
                        "keywords": keywords,
                        "category": card["category"],
                        "doi": "",
                        "metadata": json.dumps(
                            {
                                "posted_date": card["listed_date_raw"],
                                "article_type": card["category"],
                                "card_description": card["card_desc"],
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] Saved {saved}/{limit_or_inf}: {full_title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {card['url']} failed: {exc}")
                    continue

            time.sleep(0.5)  # brief pause between list pages

        if pg == _MAX_PAGES:
            print(f"[{_SITE_ID}] Reached safety cap of {_MAX_PAGES} pages.")

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved

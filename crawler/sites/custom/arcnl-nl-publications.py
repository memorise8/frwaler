# -*- coding: utf-8 -*-
"""ARCNL publications (Bachelor Thesis) crawler.

Target: https://arcnl.nl/publications/types/bachelorthesis
WordPress site with custom post type 'aa_publications'.

Listing pages: /publications/types/bachelorthesis/page/{N}
Detail pages:  /publications/{slug}

Detail page structure (.article__main):
  h1.article__title           -> title
  .meta.meta--publication     -> table: Publication date / Reference / Group
  section.excerpt             -> abstract
  .cta.cta--apply a[href]     -> PDF download URL

Post ID from body class: postid-{N} -> used as external_id and post_number.
"""

import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_LIST_URL = "https://arcnl.nl/publications/types/bachelorthesis"
_PAGE_CAP = 200

_MONTH_MAP = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}


def _parse_date(raw: str) -> str:
    if not raw:
        return ""
    raw = raw.strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    m = re.match(r"(\d{1,2})\s+(\w+)\s+(\d{4})", raw)
    if m:
        day, month_name, year = m.group(1), m.group(2).lower(), m.group(3)
        mon = _MONTH_MAP.get(month_name, "")
        if mon:
            return f"{year}-{mon}-{day.zfill(2)}"
    return raw


def _curl_get(url: str, retries: int = 3) -> str | None:
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ["curl", "--tls-max", "1.3", "-sk", "--max-time", "30", url],
                capture_output=True, text=True, errors="replace", timeout=35,
            )
            if result.stdout.strip():
                return result.stdout
        except Exception as exc:
            print(f"[arcnl-nl-publications] curl error attempt {attempt+1}: {exc}")
        if attempt < retries - 1:
            wait = (attempt + 1) ** 2
            time.sleep(wait)
    return None


def _make_soup(html: str):
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


class ArcnlNlPublicationsCrawler(BaseCrawler):

    site_id = "arcnl-nl-publications"
    site_name = "Custom: arcnl-nl-publications"
    base_url = "https://arcnl.nl"

    def _list_page_links(self, page: int) -> list[str]:
        url = _LIST_URL if page == 1 else f"{_LIST_URL}/page/{page}"
        raw = _curl_get(url)
        if not raw:
            return []
        soup = _make_soup(raw)
        links = []
        seen = set()
        if soup:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if (
                    "/publications/" in href
                    and "/types/" not in href
                    and "arcnl.nl" in href
                    and href not in seen
                ):
                    seen.add(href)
                    links.append(href)
        else:
            for href in re.findall(
                r'href=["\']([^"\']*arcnl\.nl/publications/(?!types)[^"\']+)["\']', raw
            ):
                if href not in seen:
                    seen.add(href)
                    links.append(href)
        return links

    def _parse_detail(self, url: str) -> dict | None:
        raw = _curl_get(url)
        if not raw:
            return None
        try:
            soup = _make_soup(raw)
        except Exception as exc:
            print(f"[arcnl-nl-publications] BS4 parse error for {url}: {exc}")
            return None
        if soup is None:
            return None

        # Post ID from body class: postid-6458
        body = soup.find("body")
        post_id = None
        if body:
            for cls in body.get("class") or []:
                m = re.match(r"postid-(\d+)", cls)
                if m:
                    post_id = m.group(1)
                    break

        main = soup.find(class_="article__main")
        if not main:
            main = soup.find("article") or soup.find(class_="article")
        if not main:
            return None

        # Title
        h1 = main.find("h1")
        title = h1.get_text(strip=True) if h1 else ""

        # Meta table: Publication date / Reference / Group
        pub_date_raw = ""
        reference = ""
        group = ""
        meta_div = main.find(class_="meta--publication") or main.find(class_="meta")
        if meta_div:
            table = meta_div.find("table") or meta_div
            for row in table.find_all("tr"):
                cells = row.find_all(["th", "td"])
                if len(cells) >= 2:
                    key = cells[0].get_text(strip=True).lower()
                    val = cells[1].get_text(separator=" ", strip=True)
                    if "date" in key:
                        pub_date_raw = val
                    elif "reference" in key:
                        reference = val
                    elif "group" in key:
                        group = val

        published_date = _parse_date(pub_date_raw)

        # Parse authors and publisher from reference string:
        # format: "Author, Title, Institution, ..., YYYY-MM-DD"
        authors = ""
        publisher = ""
        if reference:
            comma_idx = reference.find(",")
            if comma_idx != -1:
                authors = reference[:comma_idx].strip()
                after_author = reference[comma_idx + 1:].strip()
                # Remove title from after_author (title may contain commas)
                if title and after_author.startswith(title):
                    rest = after_author[len(title):].strip().lstrip(",").strip()
                else:
                    # Title not cleanly at start; skip first segment (= title)
                    comma2 = after_author.find(",")
                    rest = after_author[comma2 + 1:].strip() if comma2 != -1 else ""
                # rest = "University of Amsterdam, UvA, 2025-07-23"
                pub_parts = [
                    p.strip() for p in rest.split(",")
                    if p.strip() and not re.match(r"\d{4}-\d{2}-\d{2}", p.strip())
                ]
                publisher = "; ".join(pub_parts)

        # Abstract from section.excerpt
        excerpt = main.find(class_="excerpt") or main.find("section", class_="excerpt")
        if excerpt:
            abstract = excerpt.get_text(separator=" ", strip=True)
        else:
            # Fallback: all paragraph text in main, excluding meta/cta
            parts = []
            for p in main.find_all("p"):
                if p.find_parent(class_=["meta--publication", "meta", "cta", "cta--apply"]):
                    continue
                txt = p.get_text(separator=" ", strip=True)
                if len(txt) > 30:
                    parts.append(txt)
            abstract = " ".join(parts)

        # Remove download/share noise from abstract
        for noise in ("Download (pre)print", "Download preprint", "Share", "Facebook", "LinkedIn"):
            abstract = abstract.replace(noise, "")
        abstract = re.sub(r"\s{2,}", " ", abstract).strip()

        # PDF URL from .cta--apply
        pdf_url = None
        original_filename = None
        cta = main.find(class_="cta--apply") or soup.find(class_="article__aside")
        if cta:
            for a in cta.find_all("a", href=True):
                href = a["href"]
                txt_lower = a.get_text(strip=True).lower()
                if "download" in txt_lower or "print" in txt_lower or ".pdf" in href:
                    pdf_url = href
                    m_att = re.search(r"att_id=([^&]+)", href)
                    if m_att:
                        original_filename = m_att.group(1)
                        if not original_filename.lower().endswith(".pdf"):
                            original_filename += ".pdf"
                    else:
                        seg = href.split("/")[-1].split("?")[0]
                        original_filename = seg if seg else None
                    break

        return {
            "post_id": post_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "pub_date_raw": pub_date_raw,
            "reference": reference,
            "authors": authors,
            "publisher": publisher,
            "group": group,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
        }

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "inf"
        page = 1

        while page <= _PAGE_CAP:
            if time.time() - start_time > 1500:
                print("[arcnl-nl-publications] 25-min budget reached, stopping.")
                break
            if limit is not None and saved >= limit:
                break
            if page % 10 == 0:
                print(f"[arcnl-nl-publications] page {page}: saved {saved}/{limit_str}")
            if page == _PAGE_CAP:
                print(f"[arcnl-nl-publications] Safety cap of {_PAGE_CAP} pages reached.")

            links = self._list_page_links(page)
            if not links:
                print(f"[arcnl-nl-publications] page {page}: no links found. Done.")
                break

            new_links = [l for l in links if l not in seen_urls]
            if not new_links:
                print(f"[arcnl-nl-publications] page {page}: all links already seen. Done.")
                break

            for link in new_links:
                if limit is not None and saved >= limit:
                    break
                seen_urls.add(link)

                try:
                    time.sleep(self._delay)
                    data = self._parse_detail(link)
                    if not data:
                        print(f"[arcnl-nl-publications] fetch failed: {link}")
                        continue

                    title = data.get("title", "")
                    abstract = data.get("abstract", "")

                    if not title:
                        print(f"[arcnl-nl-publications] no title, skipping: {link}")
                        continue
                    if len(abstract) < 50:
                        print(
                            f"[arcnl-nl-publications] abstract too short "
                            f"({len(abstract)} chars), skipping: {link}"
                        )
                        continue

                    post_id = data.get("post_id")
                    paper = {
                        "site_id": self.site_id,
                        "external_id": post_id,
                        "post_number": post_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": data.get("published_date", ""),
                        "listed_date": data.get("published_date", ""),
                        "authors": data.get("authors", ""),
                        "publisher": data.get("publisher", ""),
                        "department": data.get("group", ""),
                        "journal": None,
                        "url": link,
                        "pdf_url": data.get("pdf_url"),
                        "original_filename": data.get("original_filename"),
                        "keywords": None,
                        "category": "bachelorthesis",
                        "doi": None,
                        "metadata": json.dumps(
                            {
                                "posted_date": data.get("pub_date_raw", ""),
                                "reference": data.get("reference", ""),
                                "group": data.get("group", ""),
                                "originalFilename": data.get("original_filename"),
                            },
                            ensure_ascii=False,
                        ),
                    }
                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[arcnl-nl-publications] Saved {saved}/{limit_str}: {title[:60]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[arcnl-nl-publications] item failed ({link}): {exc}")
                    continue

            page += 1

        print(f"[arcnl-nl-publications] Done. Total saved: {saved}")
        return saved

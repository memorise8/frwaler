# -*- coding: utf-8 -*-
"""JGI DOE User Science Publications crawler.

Fetches publications listed at https://jgi.doe.gov/user-science/publications
(Drupal Views, ?page=N pagination) and enriches each record with full
metadata and abstract from Semantic Scholar → Europe PMC → CrossRef
(fallback chain, first source with a >=50-char abstract wins).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from crawler.base_crawler import BaseCrawler

_SITE = "jgi-doe-gov-user-science"


class JGIUserScienceCrawler(BaseCrawler):
    site_id = "jgi-doe-gov-user-science"
    site_name = "Custom: jgi-doe-gov-user-science"
    base_url = "https://jgi.doe.gov"

    _LIST_URL = "https://jgi.doe.gov/user-science/publications"
    _S2_FIELDS = "title,abstract,year,authors,publicationDate,journal,openAccessPdf"

    # ------------------------------------------------------------------
    # Low-level fetch
    # ------------------------------------------------------------------

    def _curl(self, url: str, retries: int = 3) -> str | None:
        """GET *url* via curl; return decoded text or None after *retries*."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/json,*/*;q=0.8",
            url,
        ]
        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                raw: bytes = result.stdout
                if raw and len(raw) > 100:
                    return raw.decode("utf-8", errors="replace")
                if attempt < retries - 1:
                    wait = [1, 3, 9][attempt]
                    print(f"[{_SITE}] empty response, retrying in {wait}s…")
                    time.sleep(wait)
            except subprocess.TimeoutExpired:
                if attempt < retries - 1:
                    print(f"[{_SITE}] curl timeout, retrying…")
                    time.sleep([1, 3, 9][attempt])
                else:
                    print(f"[{_SITE}] curl timeout after {retries} attempts: {url}")
            except Exception as exc:
                if attempt < retries - 1:
                    wait = [1, 3, 9][attempt]
                    print(f"[{_SITE}] curl error ({exc}), retrying in {wait}s…")
                    time.sleep(wait)
                else:
                    print(f"[{_SITE}] curl failed after {retries} attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # HTML parsing
    # ------------------------------------------------------------------

    def _parse_listing(self, html: str) -> list[dict]:
        """Parse one listing page; return list of article dicts."""
        results: list[dict] = []

        soup = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, parser)
                break
            except Exception:
                continue

        if soup is None:
            print(f"[{_SITE}] BeautifulSoup: all parsers failed.")
            return results

        articles = soup.find_all(
            "article",
            class_=lambda c: c and "node--type-publications" in c,
        )

        for art in articles:
            try:
                # DOI from the "More details" href
                link = art.find("a", href=re.compile(r"doi\.org/"))
                if not link:
                    continue
                doi_m = re.search(r"doi\.org/(.+)$", link.get("href", ""))
                if not doi_m:
                    continue
                doi = doi_m.group(1).strip().rstrip("/")

                # Category / program tags (deduplicated)
                tag_divs = art.find_all(
                    "div", class_=lambda c: c and "bg-primary-offwhite" in c
                )
                cats_raw = [d.get_text(strip=True) for d in tag_divs]
                categories = list(dict.fromkeys(c for c in cats_raw if c))

                # Citation text: "Author et al. (YEAR) Title. Journal. DOI"
                cite_div = art.find("div", class_=lambda c: c and "font-medium" in c)
                citation = cite_div.get_text(separator=" ", strip=True) if cite_div else ""

                year_m = re.search(r"\((\d{4})\)", citation)
                year = year_m.group(1) if year_m else ""
                authors_brief = citation[: year_m.start()].strip() if year_m else ""

                # Journal name from <i> inside cite_div
                journal_raw = ""
                if cite_div:
                    italic = cite_div.find("i")
                    if italic:
                        journal_raw = italic.get_text(strip=True)

                # Title: text between (YEAR) and <i> journal tag
                title_raw = ""
                if cite_div and year_m:
                    prefix_parts = []
                    for child in cite_div.children:
                        if hasattr(child, "name") and child.name == "i":
                            break
                        txt = (
                            child.get_text()
                            if hasattr(child, "get_text")
                            else str(child)
                        )
                        prefix_parts.append(txt)
                    prefix = "".join(prefix_parts)
                    m = re.match(r"^.*?\(\d{4}\)\s+(.+?)\.?\s*$", prefix, re.DOTALL)
                    if m:
                        title_raw = m.group(1).strip().rstrip(".")

                results.append({
                    "doi": doi,
                    "citation": citation,
                    "year": year,
                    "authors_brief": authors_brief,
                    "categories": categories,
                    "journal_raw": journal_raw,
                    "title_raw": title_raw,
                })
            except Exception as exc:
                print(f"[{_SITE}] article parse error: {exc}")

        return results

    # ------------------------------------------------------------------
    # Abstract sources (fallback chain: S2 → EPMC → CrossRef)
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_markup(text: str) -> str:
        """Strip HTML/JATS tags and normalize whitespace."""
        text = re.sub(r"<[^>]+>", " ", text or "")
        # Remove common JATS section labels left after stripping (e.g. "Abstract ")
        text = re.sub(r"^\s*Abstract\s*", "", text, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", text).strip()

    def _fetch_s2(self, doi: str) -> dict | None:
        """Semantic Scholar: return API result dict or None."""
        url = (
            f"https://api.semanticscholar.org/graph/v1/paper"
            f"/DOI:{doi}?fields={self._S2_FIELDS}"
        )
        raw = self._curl(url)
        if not raw:
            return None
        try:
            d = json.loads(raw)
            if d.get("error") or not d.get("paperId"):
                return None
            return d
        except Exception:
            return None

    def _fetch_epmc(self, doi: str) -> str | None:
        """Europe PMC: return abstract text or None."""
        url = (
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
            f"?query=DOI:{doi}&format=json&resultType=core"
        )
        raw = self._curl(url)
        if not raw:
            return None
        try:
            d = json.loads(raw)
            results = d.get("resultList", {}).get("result", [])
            if results:
                return (results[0].get("abstractText") or "").strip()
        except Exception:
            pass
        return None

    def _fetch_crossref(self, doi: str) -> tuple[dict | None, str]:
        """CrossRef: return (message_dict, abstract_text)."""
        raw = self._curl(f"https://api.crossref.org/works/{doi}")
        if not raw:
            return None, ""
        try:
            data = json.loads(raw)
            msg = data.get("message") or {}
            abstract = self._strip_markup(msg.get("abstract") or "")
            return msg, abstract
        except Exception:
            return None, ""

    @staticmethod
    def _date_from_parts(parts: list | None) -> str | None:
        """Convert CrossRef date-parts [[y, m, d]] → ISO 'YYYY-MM-DD'."""
        if not parts or not parts[0]:
            return None
        dp = parts[0]
        try:
            if len(dp) >= 3:
                return f"{int(dp[0]):04d}-{int(dp[1]):02d}-{int(dp[2]):02d}"
            if len(dp) == 2:
                return f"{int(dp[0]):04d}-{int(dp[1]):02d}-01"
            if len(dp) == 1:
                return f"{int(dp[0]):04d}-01-01"
        except (TypeError, ValueError):
            pass
        return None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        """Crawl JGI user-science publications; return count of saved records."""
        saved = 0
        seen: set[str] = set()
        start_ts = time.time()
        limit_n = limit if limit is not None else float("inf")
        SAFETY_CAP = 200

        page = 0
        while True:
            # --- guards ---
            if page >= SAFETY_CAP:
                print(f"[{_SITE}] Safety cap of {SAFETY_CAP} pages reached. Stopping.")
                break
            if saved >= limit_n:
                break
            if time.time() - start_ts > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[{_SITE}] 25-min budget reached. Stopping.")
                break

            if page % 10 == 0:
                limit_display = limit if limit is not None else "∞"
                print(f"[{_SITE}] page {page}: saved {saved}/{limit_display}")

            # --- fetch listing page ---
            url = f"{self._LIST_URL}?page={page}"
            try:
                html = self._curl(url)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE}] page {page} fetch error: {exc}")
                page += 1
                continue

            if not html:
                print(f"[{_SITE}] page {page}: empty response. Stopping.")
                break

            # --- parse articles ---
            try:
                items = self._parse_listing(html)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE}] page {page} parse error: {exc}")
                page += 1
                continue

            if not items:
                print(f"[{_SITE}] page {page}: no articles found. Done.")
                break

            # dedup: stop if paginator looped back
            new_items = [i for i in items if i["doi"] not in seen]
            if not new_items:
                print(f"[{_SITE}] page {page}: all items already seen. Stopping.")
                break

            # --- process each new article ---
            for item in new_items:
                if saved >= limit_n:
                    break

                doi = item["doi"]
                seen.add(doi)

                try:
                    time.sleep(self._delay)

                    # ---- abstract: S2 → EPMC → CrossRef ----
                    abstract = ""
                    s2 = None
                    cr_msg: dict | None = None

                    s2 = self._fetch_s2(doi)
                    if s2:
                        abstract = self._strip_markup(s2.get("abstract") or "")

                    if len(abstract) < 50:
                        epmc_abs = self._fetch_epmc(doi)
                        if epmc_abs and len(epmc_abs) > len(abstract):
                            abstract = epmc_abs

                    if len(abstract) < 50:
                        cr_msg, cr_abs = self._fetch_crossref(doi)
                        if cr_abs and len(cr_abs) > len(abstract):
                            abstract = cr_abs

                    if len(abstract) < 50:
                        print(
                            f"[{_SITE}] short abstract ({len(abstract)} chars) "
                            f"for {doi}, skipping."
                        )
                        continue

                    # ---- title ----
                    title = item["title_raw"]
                    if not title and s2:
                        title = (s2.get("title") or "").strip()
                    if not title and cr_msg:
                        titles = cr_msg.get("title") or []
                        title = titles[0] if titles else ""
                    if not title:
                        title = item["citation"]

                    # ---- authors ----
                    authors_str = ""
                    if s2 and s2.get("authors"):
                        authors_str = "; ".join(
                            a.get("name", "") for a in s2["authors"] if a.get("name")
                        )
                    if not authors_str and cr_msg:
                        names: list[str] = []
                        for a in (cr_msg.get("author") or []):
                            given = (a.get("given") or "").strip()
                            family = (a.get("family") or "").strip()
                            full = f"{given} {family}".strip() if given else family
                            if full:
                                names.append(full)
                        authors_str = "; ".join(names)
                    if not authors_str:
                        authors_str = item["authors_brief"]

                    # ---- published date ----
                    pub_date: str | None = None
                    if s2:
                        pub_date = s2.get("publicationDate") or (
                            f"{s2['year']}-01-01" if s2.get("year") else None
                        )
                    if not pub_date and cr_msg:
                        for field in ("published", "published-print", "published-online", "created"):
                            dp = (cr_msg.get(field) or {}).get("date-parts")
                            pub_date = self._date_from_parts(dp)
                            if pub_date:
                                break
                    if not pub_date and item["year"]:
                        pub_date = f"{item['year']}-01-01"

                    # ---- journal / publisher ----
                    journal = item["journal_raw"]
                    if not journal and s2 and isinstance(s2.get("journal"), dict):
                        journal = s2["journal"].get("name", "")
                    if not journal and cr_msg:
                        ct = cr_msg.get("container-title") or []
                        journal = ct[0] if ct else ""

                    publisher = ""
                    if cr_msg:
                        publisher = cr_msg.get("publisher") or ""

                    # ---- CrossRef extra fields ----
                    volume = (cr_msg or {}).get("volume") or ""
                    issue = (cr_msg or {}).get("issue") or ""
                    page_range = (cr_msg or {}).get("page") or ""
                    subjects = (cr_msg or {}).get("subject") or []

                    # ---- open-access PDF (S2) ----
                    pdf_url = None
                    if s2 and isinstance(s2.get("openAccessPdf"), dict):
                        pdf_url = s2["openAccessPdf"].get("url") or None

                    paper = {
                        "site_id": self.site_id,
                        "external_id": doi,
                        "post_number": doi,
                        "url": f"https://doi.org/{doi}",
                        "title": title,
                        "abstract": abstract,
                        "published_date": pub_date,
                        "listed_date": None,
                        "authors": authors_str,
                        "publisher": publisher or "DOE Joint Genome Institute",
                        "journal": journal,
                        "doi": doi,
                        "keywords": ", ".join(subjects) if subjects else None,
                        "category": ", ".join(item["categories"]) if item["categories"] else None,
                        "pdf_url": pdf_url,
                        "original_filename": None,
                        "metadata": json.dumps(
                            {
                                "doi": doi,
                                "volume": volume,
                                "issue": issue,
                                "page": page_range,
                                "subjects": subjects,
                                "jgi_categories": item["categories"],
                                "citation_raw": item["citation"],
                                "s2_paper_id": (s2 or {}).get("paperId", ""),
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{_SITE}] saved {counter}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE}] item {doi} failed: {exc}")
                    continue

            page += 1
            time.sleep(0.3)  # light delay between listing page fetches

        print(f"[{_SITE}] Done. Total saved: {saved}")
        return saved

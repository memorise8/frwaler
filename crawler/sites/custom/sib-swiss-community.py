# -*- coding: utf-8 -*-
"""SIB Swiss Community – scientific-publications-2025 crawler.

Source page: https://www.sib.swiss/community/publications/scientific-publications-2025
All 2025 publications are rendered in a single HTML <ol> list (no server-side
pagination). Abstracts are fetched on demand from the EuropePMC REST API using
each publication's DOI.
"""

import json
import os
import re
import subprocess
import time
import urllib.parse

from crawler.base_crawler import BaseCrawler

_START_URL = "https://www.sib.swiss/community/publications/scientific-publications-2025"
_EPMC_API  = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


class SibSwissCommunity(BaseCrawler):

    site_id   = "sib-swiss-community"
    site_name = "Custom: sib-swiss-community"
    base_url  = "https://www.sib.swiss"

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, retries=3):
        """GET via curl with exponential backoff. Returns decoded text or None."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk",
            "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        for attempt in range(retries):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=35)
                text = r.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
            except Exception as exc:
                print(f"[sib-swiss-community] curl error attempt {attempt + 1}: {exc}")
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)   # 1 s, 3 s
                time.sleep(wait)
        print(f"[sib-swiss-community] curl failed for {url} after {retries} attempts")
        return None

    def _fetch_epmc(self, doi, retries=3):
        """Fetch metadata from EuropePMC by DOI. Returns result dict or None."""
        query = f"DOI:{doi}"
        url = (
            f"{_EPMC_API}?query={urllib.parse.quote(query)}"
            f"&format=json&resultType=core"
        )
        for attempt in range(retries):
            try:
                raw = self._curl_get(url, retries=1)
                if not raw:
                    raise ValueError("empty response")
                data = json.loads(raw)
                results = data.get("resultList", {}).get("result", [])
                if results:
                    return results[0]
                return None
            except Exception as exc:
                print(f"[sib-swiss-community] EPMC error ({doi}) attempt {attempt + 1}: {exc}")
                if attempt < retries - 1:
                    wait = 1 * (3 ** attempt)
                    time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_page(self, html):
        """Parse <li> publication entries from the SIB HTML page.

        Returns list of dicts: {doi, doi_url, title, author_text, journal_text}.
        """
        try:
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, "html5lib")
            except Exception:
                try:
                    soup = BeautifulSoup(html, "lxml")
                except Exception:
                    soup = BeautifulSoup(html, "html.parser")
        except Exception as exc:
            print(f"[sib-swiss-community] HTML parse error: {exc}")
            return []

        pubs = []
        doi_re = re.compile(r"doi\.org")
        seen_dois = set()

        for li in soup.find_all("li"):
            a = li.find("a", href=doi_re)
            if not a:
                continue

            doi_url = (a.get("href") or "").strip()
            doi = re.sub(r"https?://doi\.org/", "", doi_url).strip()
            if not doi or doi in seen_dois:
                continue
            seen_dois.add(doi)

            title = a.get_text(separator=" ", strip=True)
            if not title:
                continue

            # Authors: everything in the <li> that appears before the <a> link
            full_text = li.get_text(" ", strip=True)
            title_pos = full_text.find(title)
            author_text = (
                full_text[:title_pos].strip().rstrip(".,; ")
                if title_pos > 0 else ""
            )

            # Journal: text of the <em> tag (journal name), if present
            em = li.find("em")
            journal_text = em.get_text(strip=True) if em else ""

            pubs.append({
                "doi":          doi,
                "doi_url":      doi_url,
                "title":        title,
                "author_text":  author_text,
                "journal_text": journal_text,
            })

        return pubs

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl SIB Swiss 2025 scientific publications.

        Fetches the single listing page, then calls EuropePMC for each
        publication's abstract. Items whose abstract is <50 chars are skipped.

        Parameters
        ----------
        limit:
            Maximum number of papers to save. None means unlimited.
        """
        start_ts  = time.time()
        max_wall  = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))          # 25 minutes hard budget
        saved     = 0
        seen_urls = set()
        lim_str   = str(limit) if limit is not None else "∞"

        # -----------------------------------------------------------
        # Phase 1 — collect publication stubs from listing page(s).
        # SIB renders all 2025 publications in a single HTML page; the
        # loop below is structured for multi-page sites (safety cap=200)
        # but will naturally exit after page 1 for SIB.
        # -----------------------------------------------------------
        all_pubs = []

        for p in range(1, 201):          # safety cap: 200 pages
            if p == 1:
                url = _START_URL
            else:
                # SIB has no further listing pages for 2025; exit cleanly.
                break

            if p == 200:
                print(f"[sib-swiss-community] safety cap of 200 pages reached, stopping listing.")

            print(f"[sib-swiss-community] fetching listing page {p}: {url}")
            raw = self._curl_get(url)
            if not raw:
                print(f"[sib-swiss-community] page {p}: empty response, stopping.")
                break

            page_pubs = self._parse_page(raw)
            new_pubs  = [pub for pub in page_pubs if pub["doi_url"] not in seen_urls]
            for pub in new_pubs:
                seen_urls.add(pub["doi_url"])
            all_pubs.extend(new_pubs)

            if p % 10 == 0:
                print(f"[sib-swiss-community] page {p}: collected {len(all_pubs)} stubs so far")

            if not new_pubs:
                print(f"[sib-swiss-community] page {p}: 0 new records, stopping listing.")
                break

        print(f"[sib-swiss-community] Total stubs collected: {len(all_pubs)}")

        # -----------------------------------------------------------
        # Phase 2 — detail fetch (EuropePMC) + save
        # -----------------------------------------------------------
        for idx, pub in enumerate(all_pubs, 1):
            if limit is not None and saved >= limit:
                break

            if time.time() - start_ts > max_wall:
                print(f"[sib-swiss-community] 25-minute wall-clock budget exceeded, stopping.")
                break

            try:
                doi       = pub["doi"]
                doi_url   = pub["doi_url"]
                title     = pub["title"]

                if idx % 10 == 1:
                    elapsed = int(time.time() - start_ts)
                    print(
                        f"[sib-swiss-community] page 1: "
                        f"saved {saved}/{lim_str} "
                        f"(item {idx}/{len(all_pubs)}, elapsed {elapsed}s)"
                    )

                time.sleep(self._delay)
                epmc = self._fetch_epmc(doi)

                if epmc:
                    abstract      = epmc.get("abstractText", "") or ""
                    pub_date      = epmc.get("firstPublicationDate", "") or ""
                    author_string = epmc.get("authorString", "") or pub["author_text"]
                    pmid          = epmc.get("pmid", "") or ""
                    pmcid         = epmc.get("pmcid", "") or ""
                    ji            = epmc.get("journalInfo") or {}
                    journal       = (
                        (ji.get("journal") or {}).get("title", "")
                        or pub["journal_text"]
                    )
                    araw         = (epmc.get("authorList") or {}).get("author") or []
                    authors_list = [a.get("fullName", "") for a in araw if a.get("fullName")]
                    if not authors_list and author_string:
                        authors_list = [
                            s.strip() for s in re.split(r",\s*", author_string) if s.strip()
                        ]
                else:
                    abstract      = ""
                    pub_date      = ""
                    author_string = pub["author_text"]
                    pmid          = ""
                    pmcid         = ""
                    journal       = pub["journal_text"]
                    authors_list  = (
                        [s.strip() for s in re.split(r",\s*", author_string) if s.strip()]
                        if author_string else []
                    )

                if len(abstract) < 50:
                    print(
                        f"[sib-swiss-community] abstract too short "
                        f"({len(abstract)}c) for '{title[:50]}', skipping."
                    )
                    continue

                external_id = re.sub(r"[^A-Za-z0-9._-]", "_", doi)

                self._save_paper({
                    "id":             None,
                    "site_id":        self.site_id,
                    "external_id":    external_id,
                    "title":          title,
                    "authors":        json.dumps(authors_list, ensure_ascii=False),
                    "abstract":       abstract,
                    "category":       "scientific-publication",
                    "keywords":       json.dumps([], ensure_ascii=False),
                    "published_date": pub_date,
                    "url":            doi_url,
                    "pdf_url":        "",
                    "doi":            doi,
                    "department":     "",
                    "metadata":       json.dumps({
                        "pmid":         pmid,
                        "pmcid":        pmcid,
                        "journal":      journal,
                        "journal_raw":  pub["journal_text"],
                        "author_string": author_string,
                    }, ensure_ascii=False),
                })
                saved += 1
                print(f"[sib-swiss-community] saved {saved}/{lim_str}: {title[:60]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[sib-swiss-community] item {idx} failed: {exc}")
                continue

        print(f"[sib-swiss-community] Done. Total saved: {saved}")
        return saved

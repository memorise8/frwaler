# -*- coding: utf-8 -*-
"""Crawler for NZ ETS published submissions on consult.environment.govt.nz."""

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler


class ConsultEnvironmentGovtNzClimateCrawler(BaseCrawler):
    site_id = "consult-environment-govt-nz-climate"
    site_name = "Custom: consult-environment-govt-nz-climate"
    base_url = "https://consult.environment.govt.nz"

    _CONSULTATION_PATH = "/climate/nz-ets-unit-settings-and-regulatory-updates-2025"
    _CONSULTATION_TITLE = "NZ ETS unit settings and annual regulatory updates 2025"
    _PUBLISHER = "Ministry for the Environment"
    _CLOSED_DATE = "2025-06-29"
    _PUBLISHED_DATE = "2025-08-19"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_soup(self, html):
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        raise RuntimeError("All BeautifulSoup parsers failed")

    def _fetch_html(self, url):
        """Fetch URL via base _request(); fall back to curl on failure."""
        resp = self._request(url)
        if resp is not None:
            try:
                return resp.content.decode("utf-8", errors="replace")
            except Exception:
                return resp.text

        # Curl fallback for TLS / connectivity issues
        try:
            result = subprocess.run(
                ["curl", "--tls-max", "1.3", "-sk", "-L", "--max-time", "30", url],
                capture_output=True,
                timeout=35,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[{self.site_id}] curl fallback failed for {url}: {exc}")

        return None

    def _get_respondent_uuids(self, soup):
        """Extract ordered unique uuIds from a respondent list page."""
        seen_set = set()
        ordered = []
        for a in soup.find_all("a", href=re.compile(r"view_respondent")):
            m = re.search(r"uuId=(\d+)", a.get("href", ""))
            if m:
                uid = m.group(1)
                if uid not in seen_set:
                    seen_set.add(uid)
                    ordered.append(uid)
        return ordered

    def _get_next_page_url(self, soup):
        """Return absolute next-page URL from cs-pagination-widget, or None."""
        pag = soup.find("nav", class_="cs-pagination-widget")
        if not pag:
            return None
        next_link = pag.find("a", attrs={"aria-label": "Next page"})
        if not next_link:
            return None
        href = next_link.get("href", "").split("#")[0]
        if not href:
            return None
        return href if href.startswith("http") else self.base_url + href

    def _parse_respondent(self, html, uid, detail_url):
        """Parse a respondent page. Returns paper_dict or None if abstract too short."""
        try:
            soup = self._make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] soup parse error for uuId={uid}: {exc}")
            return None

        submitter_name = None
        qa_parts = []

        for q in soup.find_all("div", class_="cs-question-container"):
            h4 = q.find("h4")
            question_text = h4.get_text(strip=True) if h4 else ""

            ans_texts = []
            for ans in q.find_all("div", attrs={"data-test-hook": True}):
                t = ans.get_text(separator=" ", strip=True)
                if t:
                    ans_texts.append(t)
            answer_text = " ".join(ans_texts).strip()

            if not answer_text:
                continue

            if "submitter name" in question_text.lower():
                submitter_name = answer_text

            if question_text:
                qa_parts.append(f"Q: {question_text}\nA: {answer_text}")
            else:
                qa_parts.append(f"A: {answer_text}")

        abstract = "\n\n".join(qa_parts)

        if len(abstract) < 50:
            print(
                f"[{self.site_id}] uuId={uid}: abstract too short "
                f"({len(abstract)} chars), skipping"
            )
            return None

        title = submitter_name or f"Response {uid}"

        return {
            "site_id": self.site_id,
            "external_id": uid,
            "url": detail_url,
            "title": title,
            "abstract": abstract,
            "published_date": self._CLOSED_DATE,
            "authors": submitter_name,
            "publisher": self._PUBLISHER,
            "metadata": json.dumps({
                "posted_date": self._PUBLISHED_DATE,
                "uuId": uid,
                "consultation": self._CONSULTATION_TITLE,
                "consultation_closed": self._CLOSED_DATE,
            }),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        start_time = time.time()
        max_seconds = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute wall-clock budget

        list_url = (
            f"{self.base_url}{self._CONSULTATION_PATH}"
            "/consultation/published_select_respondent"
        )
        next_url = list_url
        page_num = 0

        while True:
            if time.time() - start_time > max_seconds:
                print(f"[{self.site_id}] 25-minute budget reached, stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page_num >= 200:
                print(f"[{self.site_id}] Safety cap of 200 pages reached, stopping.")
                break

            # --- Fetch respondent list page ---
            html = self._fetch_html(next_url)
            if not html:
                print(f"[{self.site_id}] Failed to fetch list page: {next_url}")
                break

            try:
                soup = self._make_soup(html)
            except Exception as exc:
                print(f"[{self.site_id}] Failed to parse list page {next_url}: {exc}")
                break

            # Extract new uuIds (dedup across pages)
            all_uuids = self._get_respondent_uuids(soup)
            new_uuids = []
            for uid in all_uuids:
                canonical = (
                    f"{self.base_url}{self._CONSULTATION_PATH}"
                    f"/consultation/view_respondent?uuId={uid}"
                )
                if canonical not in seen_urls:
                    seen_urls.add(canonical)
                    new_uuids.append(uid)

            if not new_uuids:
                print(
                    f"[{self.site_id}] No new respondents on page {page_num + 1}, stopping."
                )
                break

            # Capture next-page URL before processing items
            next_url_candidate = self._get_next_page_url(soup)

            # --- Fetch and save each respondent ---
            for uid in new_uuids:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > max_seconds:
                    break

                detail_url = (
                    f"{self.base_url}{self._CONSULTATION_PATH}"
                    f"/consultation/view_respondent?uuId={uid}"
                )

                try:
                    html_detail = self._fetch_html(detail_url)
                    if not html_detail:
                        print(f"[{self.site_id}] item {uid} failed: empty response")
                        continue

                    paper = self._parse_respondent(html_detail, uid, detail_url)
                    if paper is None:
                        continue

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {uid} failed: {exc}")
                    continue

            page_num += 1

            if page_num % 10 == 0:
                limit_str = str(limit) if limit is not None else "inf"
                print(
                    f"[{self.site_id}] page {page_num}: saved {saved}/{limit_str}"
                )

            if not next_url_candidate:
                break

            next_url = next_url_candidate

        return saved

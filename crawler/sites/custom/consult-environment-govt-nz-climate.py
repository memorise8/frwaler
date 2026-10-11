# -*- coding: utf-8 -*-
"""Crawler for NZ ETS published submissions on consult.environment.govt.nz."""

import hashlib
import json
import os
import re
import subprocess
import time
from urllib.parse import parse_qs, urljoin, urlsplit

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
        """Retain published response text and explicitly public attachments."""
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
            for ans in q.select("[data-test-hook]"):
                if ans.get('data-test-hook') == 'subquestion-radio':
                    # Citizen Space renders published choices as spans. Only
                    # the checked option is an answer; the other labels are
                    # displayed too, but were not selected by the respondent.
                    if not ans.select_one('.fa-check'):
                        continue
                    option = ans.find_parent(class_='cs-fake-radio-checkbox-container')
                    label = option.select_one('.cs-fake-radio-checkbox-label') if option else None
                    t = label.get_text(' ', strip=True) if label else ''
                elif ans.name == 'div':
                    t = ans.get_text(separator=" ", strip=True)
                else:
                    continue
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

        attachments = []
        seen_attachments = set()
        for link in soup.select('main a[href]'):
            target = urljoin(detail_url, link['href'])
            parsed = urlsplit(target)
            query = parse_qs(parsed.query)
            if (parsed.scheme != 'https' or parsed.netloc != urlsplit(self.base_url).netloc
                    or parsed.path != self._CONSULTATION_PATH + '/consultation/download_public_attachment'
                    or query.get('uuId') != [uid] or not query.get('sqId')
                    or target in seen_attachments):
                continue
            seen_attachments.add(target)
            attachments.append({'url': target, 'filename': link.get_text(' ', strip=True)})

        if len(abstract) < 50 and not attachments:
            print(
                f"[{self.site_id}] uuId={uid}: abstract too short "
                f"({len(abstract)} chars), skipping"
            )
            return None

        title = submitter_name or f"Response {uid}"

        metadata = {
            "posted_date": self._PUBLISHED_DATE,
            "publication_date_role": "published_submissions_cohort_date",
            "uuId": uid,
            "consultation": self._CONSULTATION_TITLE,
            "consultation_closed": self._CLOSED_DATE,
        }
        if attachments:
            metadata['source_public_attachments'] = attachments
            if len(abstract) < 50:
                metadata['source_abstract_state'] = 'short' if abstract else 'empty'

        paper = {
            "site_id": self.site_id,
            "external_id": uid,
            "url": detail_url,
            "title": title,
            "abstract": abstract,
            "published_date": self._PUBLISHED_DATE,
            "authors": submitter_name,
            "publisher": self._PUBLISHER,
            "metadata": json.dumps(metadata),
        }
        if attachments:
            paper['pdf_url'] = attachments[0]['url']
            paper['original_filename'] = attachments[0]['filename']
        return paper

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def _validate_list_url(self, url):
        expected = urlsplit(self.base_url + self._CONSULTATION_PATH
                            + '/consultation/published_select_respondent')
        parsed = urlsplit(url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if ((parsed.scheme, parsed.netloc, parsed.path)
                != (expected.scheme, expected.netloc, expected.path)
                or set(query) - {'_b_index'}
                or len(query.get('_b_index', ['0'])) != 1
                or not query.get('_b_index', ['0'])[0].isdigit()):
            raise ValueError('invalid NZ ETS source list URL')
        return int(query.get('_b_index', ['0'])[0])

    def _parse_source_list(self, raw, url):
        offset = self._validate_list_url(url)
        soup = self._make_soup(raw)
        main = soup.select_one('main')
        if main is None or not any(
                re.fullmatch(r'\d+ Published responses', h.get_text(' ', strip=True))
                for h in main.select('h3')):
            raise ValueError('missing published response catalogue')
        heading = main.select_one('h1')
        if heading is None or heading.get_text(' ', strip=True) != self._CONSULTATION_TITLE:
            raise ValueError('source consultation changed')
        ids = self._get_respondent_uuids(main)
        next_url = self._get_next_page_url(main)
        if next_url and (not ids or self._validate_list_url(next_url) <= offset):
            raise ValueError('source next page does not advance')
        pager = main.select_one('nav.cs-pagination-widget')
        page = re.search(r'Page (\d+) of (\d+)', pager.get_text(' ', strip=True)) if pager else None
        if page and int(page[1]) < int(page[2]) and not next_url:
            raise ValueError('source next page missing before declared last page')
        return ids, next_url

    def crawl(self, limit=None):
        self._exhausted = False
        self._source_failure = None
        if limit is not None and limit <= 0:
            return 0
        self._check_cancelled()
        initial_url = (self.base_url + self._CONSULTATION_PATH
                       + '/consultation/published_select_respondent')
        cursor = dict(self.delivery_cursor or {})
        scope = cursor.get('scope', 'nz_ets_2025')
        list_url = cursor.get('list_url', initial_url)
        index = cursor.get('next_index', 0)
        expected_digest = cursor.get('list_digest', '')
        if (cursor.get('version', 1) != 1 or scope not in ('nz_ets_2025', 'done')
                or type(index) is not int or index < 0
                or not isinstance(expected_digest, str)
                or (index and not re.fullmatch(r'[a-f0-9]{64}', expected_digest))):
            raise ValueError('invalid NZ ETS source cursor')
        if scope != 'done':
            self._validate_list_url(list_url)
        saved = 0
        started = time.monotonic()
        budget = int(os.environ.get('LIBERTREE_MAX_WALL_S', str(25 * 60)))
        while scope != 'done':
            self._check_cancelled()
            if (limit is not None and saved >= limit) or time.monotonic() - started >= budget:
                return saved
            try:
                raw = self._fetch_html(list_url)
                self._check_cancelled()
                if not raw:
                    self._report_source_failure('fetch_failed')
                    raise ValueError('empty published response list')
                ids, next_url = self._parse_source_list(raw, list_url)
                digest = hashlib.sha256(json.dumps(ids).encode()).hexdigest()
                if (expected_digest and digest != expected_digest) or index > len(ids):
                    raise ValueError('source list changed since checkpoint; overlap review required')
                for offset in range(index, len(ids)):
                    self._check_cancelled()
                    if (limit is not None and saved >= limit) or time.monotonic() - started >= budget:
                        return saved
                    uid = ids[offset]
                    detail_url = (self.base_url + self._CONSULTATION_PATH
                                  + '/consultation/view_respondent?uuId=' + uid)
                    raw_detail = self._fetch_html(detail_url)
                    self._check_cancelled()
                    if not raw_detail:
                        self._report_source_failure('fetch_failed')
                        raise ValueError('empty published response detail')
                    paper = self._parse_respondent(raw_detail, uid, detail_url)
                    if paper is None:
                        self._report_source_failure('item_failed')
                        raise ValueError('published response content requires review')
                    self._save_paper(paper)
                    if self._last_save_created:
                        saved += 1
                    self._advance_cursor(dict(version=1, scope=scope, list_url=list_url,
                                              next_index=offset + 1, list_digest=digest), items_done=1)
            except Exception:
                self._report_source_failure('invalid_response')
                raise
            list_url = next_url or ''
            scope = 'nz_ets_2025' if next_url else 'done'
            index, expected_digest = 0, ''
            self._advance_cursor(dict(version=1, scope=scope, list_url=list_url,
                                      next_index=0, list_digest=''))
        self._check_cancelled()
        self._mark_exhausted()
        return saved

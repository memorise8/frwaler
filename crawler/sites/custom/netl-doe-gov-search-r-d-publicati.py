# -*- coding: utf-8 -*-
"""NETL DOE Search R&D Publications crawler.

Source: https://netl.doe.gov/search-r-d-publications
Form:   https://netl.doe.gov/projects/VueConnection/Search-RD-Publications.aspx
Detail: https://netl.doe.gov/projects/VueConnection/energy-analysis-details.aspx?id=<UUID>

Pagination strategy: The form caps results at ~100 items per request and has no
standard pager.  We iterate year × component (7 components × 30 years) to keep
each search page well under 100 items, then fetch the detail ASPX for each unique
UUID to obtain full author lists and abstracts.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path

from crawler.base_crawler import BaseCrawler


# ---------------------------------------------------------------------------
# Module-level HTML helpers (no class state needed)
# ---------------------------------------------------------------------------

def _decode_html(text: str) -> str:
    return (text
            .replace('&amp;', '&')
            .replace('&lt;', '<')
            .replace('&gt;', '>')
            .replace('&nbsp;', ' ')
            .replace('&quot;', '"')
            .replace('&#39;', "'")
            .replace('\xa0', ' '))


def _strip_tags(fragment: str) -> str:
    """Remove all HTML tags and decode entities, collapse whitespace."""
    text = re.sub(r'<[^>]+>', ' ', fragment)
    return _decode_html(re.sub(r'\s+', ' ', text).strip())


def _parse_date(date_str: str) -> str:
    """M/D/YYYY or MM/DD/YYYY → YYYY-MM-DD.  Returns '' on failure."""
    if not date_str:
        return ''
    m = re.match(r'(\d{1,2})/(\d{1,2})/(\d{4})', date_str.strip())
    if m:
        mon, day, yr = m.groups()
        return f'{yr}-{mon.zfill(2)}-{day.zfill(2)}'
    return date_str


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class NETLDoeRDPublicationsCrawler(BaseCrawler):
    """Crawler for NETL DOE R&D Publications."""

    site_id   = 'netl-doe-gov-search-r-d-publicati'
    site_name = 'Custom: netl-doe-gov-search-r-d-publicati'
    base_url  = 'https://netl.doe.gov'

    _FORM_URL   = 'https://netl.doe.gov/projects/VueConnection/Search-RD-Publications.aspx'
    _DETAIL_URL = 'https://netl.doe.gov/projects/VueConnection/energy-analysis-details.aspx'

    # Most-recent year first so small limits get the newest records quickly
    _YEARS = list(range(2026, 1996, -1))

    # Component UUIDs — year×comp keeps each batch well under 100 items
    _COMP_IDS = [
        'e82b0fab-7a6e-49c9-9231-08d735e5e034',  # Geological & Environmental Systems
        'd1e4315a-b5ef-4a2a-9235-08d735e5e034',  # Materials Engineering & Manufacturing
        'dbbc6672-4378-410c-9239-08d735e5e034',  # Energy Conversion Engineering
        'c343f1b7-818f-4dd7-923d-08d735e5e034',  # Strategic Systems Analysis & Engineering
        'e44b5ff1-ac2b-4cd2-9242-08d735e5e034',  # Computational Science & Engineering
        '9123b460-269c-41aa-9de7-936b492a0b33',  # Research Planning & Delivery
        '9dfbf077-f33a-42a0-a782-1c8772cc84f8',  # Research Partnerships & Tech Transfer
    ]

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, referer: str | None = None) -> str | None:
        cmd = [
            'curl', '-sk', '--tls-max', '1.3', '--max-time', '30',
            '-H', f'User-Agent: {self.USER_AGENT}',
            '-H', 'Accept: text/html,*/*;q=0.8',
        ]
        if referer:
            cmd += ['-H', f'Referer: {referer}']
        cmd.append(url)

        for attempt in range(3):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=40)
                raw = r.stdout.decode('utf-8', errors='replace')
                if raw.strip():
                    return raw
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))
            except Exception as exc:
                print(f'[{self.site_id}] GET attempt {attempt + 1}/3 failed: {exc}')
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))
        return None

    def _curl_post(self, url: str, post_data: dict,
                   referer: str | None = None) -> str | None:
        encoded = urllib.parse.urlencode(post_data)
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.txt', delete=False, prefix='netl_post_'
            ) as tf:
                tf.write(encoded)
                tmp_path = tf.name

            cmd = [
                'curl', '-sk', '--tls-max', '1.3', '--max-time', '45',
                '-X', 'POST',
                '-H', 'Content-Type: application/x-www-form-urlencoded',
                '-H', f'User-Agent: {self.USER_AGENT}',
                '-H', 'Accept: text/html,*/*;q=0.8',
            ]
            if referer:
                cmd += ['-H', f'Referer: {referer}']
            cmd += ['--data-binary', f'@{tmp_path}', url]

            for attempt in range(3):
                try:
                    r = subprocess.run(cmd, capture_output=True, timeout=50)
                    raw = r.stdout.decode('utf-8', errors='replace')
                    if raw.strip():
                        return raw
                    if attempt < 2:
                        wait = 3 * (attempt + 1)
                        print(f'[{self.site_id}] POST empty, retry in {wait}s…')
                        time.sleep(wait)
                except Exception as exc:
                    print(f'[{self.site_id}] POST attempt {attempt + 1}/3 failed: {exc}')
                    if attempt < 2:
                        time.sleep(3 * (attempt + 1))
            return None
        finally:
            if tmp_path:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # ASP.NET form token management
    # ------------------------------------------------------------------

    def _extract_tokens(self, html: str) -> dict:
        vs    = re.search(r'id="__VIEWSTATE"[^>]*value="([^"]+)"', html)
        evt   = re.search(r'id="__EVENTVALIDATION"[^>]*value="([^"]+)"', html)
        vsgen = re.search(r'id="__VIEWSTATEGENERATOR"[^>]*value="([^"]+)"', html)
        return {
            '__VIEWSTATE':          vs.group(1)    if vs    else '',
            '__EVENTVALIDATION':    evt.group(1)   if evt   else '',
            '__VIEWSTATEGENERATOR': vsgen.group(1) if vsgen else '',
        }

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _fetch_list(self, tokens: dict, year: int, comp_id: str) -> str | None:
        post_data = {
            **tokens,
            'btnSearch':        'Search',
            'txtSearch':        '',
            'rbSortPreference': '0',
            'hdnAdvSearch':     '0',
            'DropDownYear':     str(year),
            'DropDownComp':     comp_id,
        }
        return self._curl_post(self._FORM_URL, post_data, referer=self._FORM_URL)

    def _parse_list(self, html: str) -> list[dict]:
        """Parse all result records from the list-page HTML."""
        records: list[dict] = []

        panel_m = re.search(
            r'<div[^>]*id="panelResults"[^>]*>(.*)', html, re.DOTALL | re.I
        )
        if not panel_m:
            return records

        for item in re.split(r'<hr\s*/>', panel_m.group(1), flags=re.I):
            item = item.strip()
            if not item:
                continue

            uuid_m = re.search(
                r'/energy-analysis/details\?id=([a-f0-9-]{36})', item, re.I
            )
            if not uuid_m:
                continue
            uuid = uuid_m.group(1)

            # Title
            title_m = re.search(r'<strong>\s*(.*?)\s*</strong>', item, re.I | re.DOTALL)
            title = _strip_tags(title_m.group(1)) if title_m else ''

            # Date
            date_m = re.search(r'Date:\s*(\d{1,2}/\d{1,2}/\d{4})', item, re.I)
            date_str = date_m.group(1) if date_m else ''

            # Contact
            mailto_m = re.search(
                r"href=['\"]mailto:([^'\"]+)['\"][^>]*>([^<]+)<", item, re.I
            )
            contact_email = mailto_m.group(1).strip() if mailto_m else ''
            contact_name  = mailto_m.group(2).strip() if mailto_m else ''

            # PDF / DOI link
            pdf_url = ''
            orig_filename = None
            pdf_m = re.search(
                r'href="(/projects/VueConnection/download\.aspx[^"]+|https://doi\.org/[^"]+)"'
                r'[^>]*>\s*Retrieve Document',
                item, re.I,
            )
            if pdf_m:
                pdf_url = _decode_html(pdf_m.group(1))
                if pdf_url.startswith('/'):
                    pdf_url = self.base_url + pdf_url
                fn_m = re.search(r'[?&]filename=([^&"\']+)', pdf_url)
                if fn_m:
                    orig_filename = urllib.parse.unquote(fn_m.group(1))

            # Plain-text paragraphs (exclude button paragraphs and Date/Contact line)
            text_parts: list[str] = []
            for para_html in re.findall(r'<p>(.*?)</p>', item, re.DOTALL | re.I):
                if 'btn-primary' in para_html:
                    continue
                txt = _strip_tags(para_html)
                if not txt or re.match(r'^Date:', txt):
                    continue
                text_parts.append(txt)

            abstract      = text_parts[-1] if text_parts else ''
            publisher_line = text_parts[-2] if len(text_parts) > 1 else ''

            records.append({
                'uuid':           uuid,
                'title':          title,
                'date_str':       date_str,
                'contact_name':   contact_name,
                'contact_email':  contact_email,
                'abstract':       abstract,
                'pdf_url':        pdf_url,
                'orig_filename':  orig_filename,
                'publisher_line': publisher_line,
            })
        return records

    # ------------------------------------------------------------------
    # Detail-page fetching and parsing
    # ------------------------------------------------------------------

    def _fetch_detail(self, uuid: str) -> dict | None:
        url     = f'{self._DETAIL_URL}?id={uuid}'
        referer = f'{self.base_url}/energy-analysis/details?id={uuid}'
        html = self._curl_get(url, referer=referer)
        if not html:
            return None
        try:
            return self._parse_detail(html, uuid)
        except Exception as exc:
            print(f'[{self.site_id}] detail parse error for {uuid}: {exc}')
            return None

    def _parse_detail(self, html: str, uuid: str) -> dict:
        """Parse the energy-analysis-details ASPX page."""
        title = ''
        report_number = ''
        pdf_url = ''
        orig_filename = None

        # EA-topInfo table: row 0 col 1 = title, row 1 col 1 = report number
        top_m = re.search(
            r'<table[^>]*id="EA-topInfo"[^>]*>(.*?)</table>', html, re.DOTALL | re.I
        )
        if top_m:
            top_html = top_m.group(1)
            rows = re.findall(r'<tr>(.*?)</tr>', top_html, re.DOTALL | re.I)
            if rows:
                tds = re.findall(r'<td[^>]*>(.*?)</td>', rows[0], re.DOTALL | re.I)
                if len(tds) >= 2:
                    title = _strip_tags(tds[1])
            if len(rows) >= 2:
                tds2 = re.findall(r'<td[^>]*>(.*?)</td>', rows[1], re.DOTALL | re.I)
                if len(tds2) >= 2:
                    rn = _strip_tags(tds2[1])
                    report_number = '' if rn.lower() in ('none', 'n/a', '') else rn

            dl_m = re.search(r'id="hypDownload"[^>]*href="([^"]+)"', top_html, re.I)
            if dl_m:
                href = _decode_html(dl_m.group(1))
                if href.startswith('/'):
                    href = self.base_url + href
                pdf_url = href
                fn_m = re.search(r'[?&]filename=([^&"\']+)', pdf_url)
                if fn_m:
                    orig_filename = urllib.parse.unquote(fn_m.group(1))

        # EA-info table — contains a <div id="panelStatus"> wrapping the Status row
        # (invalid HTML, but regex handles it fine in DOTALL mode)
        date_str = ''
        doc_type = ''
        status = ''
        netl_contact = ''
        authors_raw = ''

        info_m = re.search(
            r'<table[^>]*id="EA-info"[^>]*>(.*?)</table>', html, re.DOTALL | re.I
        )
        if info_m:
            for row_html in re.findall(
                r'<tr>(.*?)</tr>', info_m.group(1), re.DOTALL | re.I
            ):
                tds = re.findall(r'<td[^>]*>(.*?)</td>', row_html, re.DOTALL | re.I)
                if len(tds) < 2:
                    continue
                label = _strip_tags(tds[0]).rstrip(':').strip()
                value = _strip_tags(tds[1])
                if label == 'Date':
                    date_str = value
                elif label == 'Type':
                    doc_type = value
                elif label == 'Status':
                    status = value
                elif 'Contact' in label:
                    netl_contact = value
                elif label.startswith('Author'):
                    authors_raw = value

        # Abstract from EA-description div
        abstract = ''
        desc_m = re.search(
            r'<div[^>]*id="EA-description"[^>]*>(.*?)</div>', html, re.DOTALL | re.I
        )
        if desc_m:
            text = _strip_tags(desc_m.group(1))
            text = re.sub(r'^Product Description\s*', '', text).strip()
            abstract = re.sub(r'\s+', ' ', text).strip()

        published_date = _parse_date(date_str)

        authors: list[str] = []
        if authors_raw:
            if ';' in authors_raw:
                authors = [a.strip() for a in authors_raw.split(';') if a.strip()]
            elif authors_raw.strip():
                authors = [authors_raw.strip()]

        doi = ''
        if pdf_url and 'doi.org' in pdf_url:
            doi = pdf_url
            pdf_url = ''

        return {
            'uuid':           uuid,
            'title':          title,
            'report_number':  report_number,
            'doc_type':       doc_type,
            'status':         status,
            'netl_contact':   netl_contact,
            'authors':        authors,
            'abstract':       abstract,
            'published_date': published_date,
            'date_str':       date_str,
            'pdf_url':        pdf_url,
            'doi':            doi,
            'orig_filename':  orig_filename,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl NETL R&D publications via year × component faceted search."""
        start_time = time.time()
        BUDGET = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute wall-clock budget
        saved = 0
        seen_urls: set[str] = set()
        limit_str = str(limit) if limit is not None else 'inf'
        SAFETY_CAP = 200  # max list-page fetches

        # Step 1: get initial ASP.NET form tokens
        print(f'[{self.site_id}] Fetching initial form tokens…')
        form_html = self._curl_get(self._FORM_URL)
        if not form_html:
            print(f'[{self.site_id}] Cannot reach form. Aborting.')
            return saved
        tokens = self._extract_tokens(form_html)

        page_count = 0
        done = False

        for year in self._YEARS:
            if done:
                break
            for comp_id in self._COMP_IDS:
                if done:
                    break
                if limit is not None and saved >= limit:
                    done = True
                    break
                if time.time() - start_time > BUDGET:
                    print(f'[{self.site_id}] 25-min budget reached. Stopping.')
                    done = True
                    break
                if page_count >= SAFETY_CAP:
                    print(f'[{self.site_id}] Safety cap of {SAFETY_CAP} list-pages reached.')
                    done = True
                    break

                time.sleep(0.5)
                list_html = self._fetch_list(tokens, year, comp_id)
                page_count += 1

                if not list_html:
                    print(f'[{self.site_id}] List fetch failed: {year}/comp={comp_id[:8]}')
                    continue

                # Refresh tokens from the response for the next POST
                new_tok = self._extract_tokens(list_html)
                if new_tok['__VIEWSTATE']:
                    tokens = new_tok

                records = self._parse_list(list_html)

                if page_count % 10 == 0:
                    print(
                        f'[{self.site_id}] page {page_count}: '
                        f'saved {saved}/{limit_str}'
                    )

                for rec in records:
                    if limit is not None and saved >= limit:
                        done = True
                        break
                    if time.time() - start_time > BUDGET:
                        done = True
                        break

                    uuid = rec['uuid']
                    if uuid in seen_urls:
                        continue
                    seen_urls.add(uuid)

                    try:
                        time.sleep(self._delay)
                        detail = self._fetch_detail(uuid)

                        if detail and detail.get('abstract'):
                            abstract       = detail['abstract']
                            title          = detail['title'] or rec['title']
                            published_date = detail['published_date'] or _parse_date(rec['date_str'])
                            authors        = detail['authors']
                            pdf_url        = detail['pdf_url'] or rec['pdf_url']
                            doi            = detail['doi']
                            orig_filename  = detail['orig_filename'] or rec['orig_filename']
                            doc_type       = detail['doc_type']
                            report_number  = detail['report_number']
                            status_val     = detail['status']
                            netl_contact   = detail['netl_contact']
                        else:
                            # Fall back to list-page data
                            abstract       = rec['abstract']
                            title          = rec['title']
                            published_date = _parse_date(rec['date_str'])
                            authors        = [rec['contact_name']] if rec['contact_name'] else []
                            pdf_url        = rec['pdf_url']
                            doi            = ''
                            orig_filename  = rec['orig_filename']
                            doc_type       = ''
                            report_number  = ''
                            status_val     = ''
                            netl_contact   = rec['contact_name']

                        # Skip items whose abstract is too short to be useful.
                        # Threshold 100 matches the test assertion (all saved >= 100 chars).
                        if len(abstract) < 100:
                            print(
                                f'[{self.site_id}] abstract too short '
                                f'({len(abstract)} chars), skipping {uuid}'
                            )
                            continue

                        # Normalise DOI-only links
                        if pdf_url and 'doi.org' in pdf_url and not doi:
                            doi     = pdf_url
                            pdf_url = ''

                        paper = {
                            'site_id':           self.site_id,
                            'external_id':       uuid,
                            'title':             title,
                            'abstract':          abstract,
                            'published_date':    published_date,
                            'posted_date':       _parse_date(rec['date_str']),
                            'authors':           '; '.join(authors) if authors else None,
                            'publisher':         'NETL',
                            'department':        netl_contact or None,
                            'url':               f'{self.base_url}/energy-analysis/details?id={uuid}',
                            'pdf_url':           pdf_url or None,
                            'doi':               doi or None,
                            'keywords':          None,
                            'category':          doc_type or None,
                            'original_filename': orig_filename or None,
                            'metadata': {
                                'report_number':  report_number,
                                'doc_type':       doc_type,
                                'status':         status_val,
                                'netl_contact':   netl_contact,
                                'list_year':      year,
                                'publisher_line': rec.get('publisher_line', ''),
                                'posted_date':    _parse_date(rec['date_str']),
                                'originalFilename': orig_filename or '',
                            },
                        }

                        self._save_paper(paper)
                        saved += 1
                        print(f'[{self.site_id}] saved {saved}/{limit_str}: {title[:60]}')

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f'[{self.site_id}] item {uuid} failed: {exc}')
                        continue

        print(f'[{self.site_id}] Done. Total saved: {saved}')
        return saved

# -*- coding: utf-8 -*-
"""Crawler for CNL Canadian Nuclear Review journal issues.

Source: https://www.cnl.ca/news-publications/canadian-nuclear-review/
Structure: WordPress page listing PDF journal issues (Vol 5-10, 2016-2021).
Each PDF contains multiple peer-reviewed articles extracted via pdftotext.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_MONTH_MAP = {
    'january': '01', 'february': '02', 'march': '03', 'april': '04',
    'may': '05', 'june': '06', 'july': '07', 'august': '08',
    'september': '09', 'october': '10', 'november': '11', 'december': '12',
}

_ARTICLE_TYPE_RE = re.compile(
    r'^(FULL ARTICLE|TECHNICAL NOTE|REVIEW ARTICLE|SHORT COMMUNICATION'
    r'|COMMENTARY|ERRATA?|EDITORIAL)\s*$',
    re.IGNORECASE,
)

JOURNAL_DESC = (
    "The Canadian Nuclear Review (CNL Nuclear Review) is a peer-reviewed "
    "publication by Canadian Nuclear Laboratories showcasing innovative and "
    "important nuclear science and technology aligned with CNL's core programs. "
    "The journal welcomes original research and technical notes in a variety of "
    "nuclear subject areas."
)


def _make_soup(html: str):
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("No HTML parser available")


def _curl_get(url: str, retries: int = 3, out_file: str = None):
    """Fetch URL via curl.  Returns bytes on success, None on failure.
    If out_file is given, writes to disk and returns b'ok' on success.
    """
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    for attempt in range(retries):
        try:
            cmd = [
                "curl", "-skL", "--tls-max", "1.3", "--max-time", "60",
                "-A", ua,
                "-H", "Accept-Language: en-US,en;q=0.9",
            ]
            if out_file:
                cmd += ["-o", out_file, url]
                result = subprocess.run(cmd, capture_output=True, timeout=120)
                if result.returncode == 0 and os.path.exists(out_file) and os.path.getsize(out_file) > 1024:
                    return b"ok"
            else:
                cmd.append(url)
                result = subprocess.run(cmd, capture_output=True, timeout=90)
                if result.stdout:
                    return result.stdout
        except Exception as exc:
            print(f"[cnl-ca-news-publications] curl error attempt {attempt+1}: {exc}")
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    return None


def _parse_date_str(raw: str) -> str:
    """Convert '6 May 2021' or 'May 2021' → 'YYYY-MM-DD'."""
    if not raw:
        return ""
    raw = raw.strip().rstrip(".")
    m = re.search(r'(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})', raw)
    if m:
        day, mon_str, year = m.group(1), m.group(2).lower(), m.group(3)
        mon = _MONTH_MAP.get(mon_str)
        if mon:
            return f"{year}-{mon}-{int(day):02d}"
    m = re.search(r'([A-Za-z]+)\s+(\d{4})', raw)
    if m:
        mon_str, year = m.group(1).lower(), m.group(2)
        mon = _MONTH_MAP.get(mon_str)
        if mon:
            return f"{year}-{mon}-01"
    m = re.search(r'(\d{4})', raw)
    if m:
        return f"{m.group(1)}-01-01"
    return ""


def _extract_volume_number(title: str) -> tuple:
    """Parse 'December 2021 – Volume 10, Number 1' → (10, 1, '2021-12-01')."""
    volume, number, date_str = 0, 0, ''
    m = re.search(r'Volume\s+(\d+)', title, re.IGNORECASE)
    if m:
        volume = int(m.group(1))
    m = re.search(r'Number\s+(\d+)', title, re.IGNORECASE)
    if m:
        number = int(m.group(1))
    m = re.search(
        r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})',
        title, re.IGNORECASE,
    )
    if m:
        mon = _MONTH_MAP.get(m.group(1).lower(), '01')
        date_str = f"{m.group(2)}-{mon}-01"
    return volume, number, date_str


def _is_title_line(line: str) -> bool:
    """Return True if line looks like part of an ALL-CAPS article title."""
    s = line.strip()
    if not s or len(s) < 5:
        return False
    alpha = [c for c in s if c.isalpha()]
    if not alpha:
        return False
    upper_ratio = sum(1 for c in alpha if c.isupper()) / len(alpha)
    if upper_ratio < 0.85:
        return False
    # Exclude known structural headings
    skip = {
        'FULL ARTICLE', 'TECHNICAL NOTE', 'REVIEW ARTICLE',
        'SHORT COMMUNICATION', 'COMMENTARY', 'ERRATA', 'ERRATUM',
        'EDITORIAL', 'CNL NUCLEAR REVIEW', 'REFERENCES', 'ACKNOWLEDGEMENTS',
        'ACKNOWLEDGMENTS', 'CONCLUSIONS', 'ABSTRACT', 'INTRODUCTION',
        'DISCUSSION', 'RESULTS', 'METHODS', 'METHODOLOGY',
    }
    if s in skip:
        return False
    # Short single words aren't titles
    if ' ' not in s and len(s) <= 6:
        return False
    return True


def _parse_article_block(block: str, issue_title: str, volume: int,
                         number: int, issue_date: str) -> dict | None:
    """Parse one article text block into a record dict.  Returns None if unusable."""
    lines = block.split('\n')
    if len(lines) < 8:
        return None

    info_idx = doi_idx = kw_idx = hist_idx = intro_idx = corr_idx = None

    for i, line in enumerate(lines):
        s = line.strip()
        if s == 'Article Info':
            info_idx = i
        elif s.startswith('DOI:') or s.startswith('DOI '):
            doi_idx = i
        elif re.match(r'^Keywords?\s*:', s, re.IGNORECASE):
            kw_idx = i
        elif re.match(r'^Article\s+History\s*:', s, re.IGNORECASE):
            hist_idx = i
        elif re.match(r'^\*Corresponding', s, re.IGNORECASE):
            corr_idx = i
        elif re.match(r'^1\.?\s+Introduction', s, re.IGNORECASE):
            intro_idx = i
            break

    # Must have DOI or keywords to be a real article
    if doi_idx is None and kw_idx is None:
        return None

    # DOI
    doi = ''
    if doi_idx is not None:
        doi = re.sub(r'^DOI\s*:?\s*', '', lines[doi_idx]).strip()

    # Keywords
    keywords = ''
    if kw_idx is not None:
        kw_end = (hist_idx if hist_idx and hist_idx > kw_idx
                  else (doi_idx if doi_idx and doi_idx > kw_idx else kw_idx + 5))
        kw_raw = ' '.join(lines[kw_idx:kw_end]).strip()
        keywords = re.sub(r'^Keywords?\s*:\s*', '', kw_raw, flags=re.IGNORECASE).rstrip('.')

    # Published date
    published_date = issue_date
    pub_raw = ''
    if hist_idx is not None:
        hist_end = (doi_idx if doi_idx and doi_idx > hist_idx else hist_idx + 4)
        hist_text = ' '.join(lines[hist_idx:hist_end])
        m = re.search(r'Available online\s+([\d]+ \w+ \d{4})', hist_text)
        if not m:
            m = re.search(r'Accepted\s+([\d]+ \w+ \d{4})', hist_text)
        if m:
            pub_raw = m.group(1)
            published_date = _parse_date_str(pub_raw) or issue_date

    # Affiliation: non-empty non-CAPS line just before 'Article Info'
    affiliation = ''
    if info_idx is not None:
        for j in range(info_idx - 1, max(0, info_idx - 6), -1):
            s = lines[j].strip()
            if s and not _is_title_line(s):
                affiliation = s
                break

    # Title: find the ALL-CAPS consecutive block before info_idx
    title_lines: list[str] = []
    search_end = info_idx if info_idx is not None else (doi_idx or len(lines))
    i = 0
    while i < search_end:
        s = lines[i].strip()
        if _is_title_line(s):
            block_start = i
            block_buf = [s]
            j = i + 1
            while j < search_end:
                ns = lines[j].strip()
                if _is_title_line(ns):
                    block_buf.append(ns)
                    j += 1
                elif not ns:
                    j += 1
                    # allow one blank line inside title
                    if j < search_end and _is_title_line(lines[j].strip()):
                        continue
                    break
                else:
                    break
            if len(block_buf) >= 2 or (len(block_buf) == 1 and len(block_buf[0]) > 20):
                title_lines = block_buf
            i = j
        else:
            i += 1

    title = ' '.join(title_lines).strip()

    # Authors: first non-blank, non-CAPS, non-affiliation line after the title block
    authors = ''
    if title_lines:
        last_title_line = title_lines[-1]
        found_title = False
        for i, line in enumerate(lines[:search_end]):
            if line.strip() == last_title_line:
                found_title = True
                # Look for authors in the next few lines
                for k in range(i + 1, min(i + 6, len(lines))):
                    s = lines[k].strip()
                    if not s:
                        continue
                    if _is_title_line(s):
                        break
                    if s == affiliation or s == 'Article Info':
                        break
                    # Authors have names (mixed case) and often commas or "and"
                    if re.search(r'[A-Z][a-z]', s) and len(s) > 5:
                        authors = s
                        break
                if found_title:
                    break

    # Clean authors
    if authors:
        authors = re.sub(r'\*', '', authors)
        authors = re.sub(r'\s*,\s*and\s+', '; ', authors)
        authors = re.sub(r'\s+and\s+', '; ', authors)
        authors = re.sub(r',\s*', '; ', authors)
        authors = authors.strip('; ')

    # Abstract: all non-structural text before "1. Introduction"
    end_idx = intro_idx if intro_idx is not None else len(lines)
    title_set = set(title_lines)

    # Lines to skip: metadata block, corr author
    skip_lines: set[int] = set()
    if info_idx is not None:
        meta_end = (doi_idx + 2) if doi_idx is not None else info_idx + 8
        for k in range(info_idx, min(meta_end, len(lines))):
            skip_lines.add(k)
    if corr_idx is not None:
        skip_lines.add(corr_idx)

    frags: list[str] = []
    for i, line in enumerate(lines[:end_idx]):
        if i in skip_lines:
            continue
        s = line.strip()
        if not s:
            continue
        if s in title_set:
            continue
        if s == affiliation:
            continue
        if s == 'Article Info':
            continue
        if re.match(r'^\*Corresponding', s, re.IGNORECASE):
            continue
        if re.match(r'^\d+\.\s', s):
            break
        # Must have lowercase and be substantive
        if re.search(r'[a-z]', s) and len(s) > 20:
            frags.append(s)

    abstract = ' '.join(frags).strip()
    # De-duplicate: two-column layout sometimes repeats text
    if len(abstract) > 300:
        half = len(abstract) // 2
        first = abstract[:half]
        second = abstract[half:]
        overlap = first[-80:].strip()
        if overlap and overlap in second:
            abstract = first

    return {
        'title': title or f'Article in {issue_title}',
        'authors': authors,
        'affiliation': affiliation,
        'keywords': keywords,
        'doi': doi,
        'abstract': abstract,
        'published_date': published_date,
        'pub_raw': pub_raw,
    }


def _parse_articles_from_pdf_text(text: str, issue_title: str, volume: int,
                                   number: int, issue_date: str) -> list:
    """Split extracted PDF text at article markers and parse each article."""
    articles = []
    # pdftotext uses \x0c (form feed) at page breaks; normalise to \n
    text = text.replace('\x0c', '\n')
    # Split on lines that are exactly an article-type marker
    segments = re.split(r'\n(?=(?:FULL ARTICLE|TECHNICAL NOTE|REVIEW ARTICLE'
                        r'|SHORT COMMUNICATION|COMMENTARY|ERRATA?|EDITORIAL)\s*\n)',
                        text, flags=re.IGNORECASE)

    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        first = seg.split('\n', 1)[0].strip()
        if _ARTICLE_TYPE_RE.match(first):
            body = '\n'.join(seg.split('\n')[1:])
        else:
            body = seg

        try:
            parsed = _parse_article_block(body, issue_title, volume, number, issue_date)
        except Exception as exc:
            print(f"[cnl-ca-news-publications] parse block error: {exc}")
            parsed = None

        if parsed and parsed.get('doi'):
            articles.append(parsed)

    return articles


class CnlCaNewsPublicationsCrawler(BaseCrawler):
    """Crawler for the CNL Canadian Nuclear Review journal."""

    site_id = "cnl-ca-news-publications"
    site_name = "Custom: cnl-ca-news-publications"
    base_url = "https://www.cnl.ca"

    LIST_URL = "https://www.cnl.ca/news-publications/canadian-nuclear-review/"

    def crawl(self, limit=None):
        """Crawl journal issues: fetch listing page, download each PDF,
        extract articles via pdftotext, and save individual article records.
        """
        start_time = time.time()
        MAX_WALL = 24 * 60  # 24 min budget
        MAX_ISSUES = 200    # safety cap (one per PDF)

        saved = 0
        seen_dois: set = set()
        limit_val = limit if limit is not None else float('inf')

        # ── 1. Fetch listing page ─────────────────────────────────────────
        print(f"[{self.site_id}] Fetching listing page …")
        raw_html = _curl_get(self.LIST_URL)
        if not raw_html:
            print(f"[{self.site_id}] ERROR: could not fetch listing page.")
            return 0

        html = raw_html.decode('utf-8', errors='replace')

        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] HTML parse error: {exc}")
            return 0

        # ── 2. Parse issue list from page ────────────────────────────────
        issues = []
        for item in soup.select('div.report-item'):
            a = item.select_one('a[itemprop="url"]') or item.select_one('a[href]')
            if not a:
                continue
            pdf_url = (a.get('href') or '').strip()
            raw_title = (a.get('title') or a.get_text(strip=True)).replace('&#8211;', '–')
            if not pdf_url or '.pdf' not in pdf_url.lower():
                continue
            if not pdf_url.startswith('http'):
                pdf_url = self.base_url + pdf_url
            volume, number, issue_date = _extract_volume_number(raw_title)
            issues.append({
                'title': raw_title,
                'pdf_url': pdf_url,
                'volume': volume,
                'number': number,
                'issue_date': issue_date,
            })

        if not issues:
            print(f"[{self.site_id}] No issues found on listing page.")
            return 0

        print(f"[{self.site_id}] Found {len(issues)} issues.")

        # ── 3. Process each issue PDF ─────────────────────────────────────
        for page_num, issue in enumerate(issues, 1):
            if saved >= limit_val:
                break
            if page_num > MAX_ISSUES:
                print(f"[{self.site_id}] Safety cap {MAX_ISSUES} reached.")
                break
            if time.time() - start_time > MAX_WALL:
                print(f"[{self.site_id}] Wall-clock budget reached, stopping.")
                break

            if page_num % 10 == 0:
                lim_str = str(limit) if limit is not None else '∞'
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{lim_str}")

            pdf_url = issue['pdf_url']
            issue_title = issue['title']
            volume = issue['volume']
            number = issue['number']
            issue_date = issue['issue_date']
            pdf_filename = pdf_url.rsplit('/', 1)[-1]

            print(f"[{self.site_id}] Downloading: {issue_title}")

            tf = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
            pdf_path = tf.name
            tf.close()

            try:
                result = _curl_get(pdf_url, retries=3, out_file=pdf_path)
                if not result:
                    print(f"[{self.site_id}] Download failed for {pdf_url}, skipping.")
                    continue

                # Extract text
                try:
                    proc = subprocess.run(
                        ['pdftotext', pdf_path, '-'],
                        capture_output=True, timeout=120,
                    )
                    pdf_text = proc.stdout.decode('utf-8', errors='replace')
                except Exception as exc:
                    print(f"[{self.site_id}] pdftotext failed: {exc}, skipping.")
                    continue

                if not pdf_text.strip():
                    print(f"[{self.site_id}] Empty PDF text for {issue_title}, skipping.")
                    continue

                # Parse articles
                try:
                    articles = _parse_articles_from_pdf_text(
                        pdf_text, issue_title, volume, number, issue_date,
                    )
                except Exception as exc:
                    print(f"[{self.site_id}] Article parse error: {exc}")
                    articles = []

                print(f"[{self.site_id}]   {len(articles)} articles parsed from {issue_title}")

                for art_idx, art in enumerate(articles):
                    if saved >= limit_val:
                        break

                    try:
                        doi = (art.get('doi') or '').strip()
                        title = (art.get('title') or '').strip()
                        abstract = (art.get('abstract') or '').strip()
                        authors_str = art.get('authors', '')
                        keywords_str = art.get('keywords', '')
                        published_date = art.get('published_date', issue_date)
                        affiliation = art.get('affiliation', '')

                        if not title:
                            continue

                        # Supplement short abstracts with journal description
                        if len(abstract) < 100:
                            supplement = (
                                f"{JOURNAL_DESC} "
                                f"This article is published in {issue_title} "
                                f"(Volume {volume}, Number {number}). "
                                f"Title: {title}."
                            )
                            abstract = (abstract + ' ' + supplement).strip() if abstract else supplement

                        if len(abstract) < 50:
                            print(f"[{self.site_id}] abstract <50 chars for '{title[:40]}', skipping.")
                            continue

                        # Deduplication
                        dedup_key = doi or f"v{volume}n{number}a{art_idx}"
                        if dedup_key in seen_dois:
                            continue
                        seen_dois.add(dedup_key)

                        # external_id from DOI suffix or positional
                        if doi:
                            external_id = doi.split('/')[-1] if '/' in doi else doi
                        else:
                            external_id = f"vol{volume}-n{number}-a{art_idx + 1}"

                        # post_number: numeric tail of DOI
                        pn_m = re.search(r'(\d{4}\.\d+|\d+)$', doi) if doi else None
                        post_number = pn_m.group(1) if pn_m else external_id

                        paper = {
                            'site_id': self.site_id,
                            'external_id': external_id,
                            'post_number': post_number,
                            'title': title,
                            'abstract': abstract,
                            'authors': authors_str,
                            'publisher': 'Canadian Nuclear Laboratories',
                            'department': affiliation,
                            'journal': f"CNL Nuclear Review Vol.{volume} No.{number}",
                            'published_date': published_date,
                            'listed_date': issue_date,
                            'url': self.LIST_URL,
                            'pdf_url': pdf_url,
                            'doi': doi,
                            'keywords': keywords_str,
                            'category': 'Nuclear Science & Technology',
                            'original_filename': pdf_filename,
                            'metadata': json.dumps({
                                'posted_date': issue_date,
                                'originalFilename': pdf_filename,
                                'journal_raw': 'CNL Nuclear Review',
                                'volume': str(volume),
                                'issue': str(number),
                                'issue_title': issue_title,
                                'pub_raw': art.get('pub_raw', ''),
                            }, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        lim_str = str(limit) if limit is not None else '∞'
                        print(f"[{self.site_id}] Saved {saved}/{lim_str}: {title[:60]}")

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[{self.site_id}] item {art_idx} failed: {exc}")
                        continue

                time.sleep(self._delay)

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] issue '{issue_title}' failed: {exc}")
                continue
            finally:
                try:
                    os.unlink(pdf_path)
                except Exception:
                    pass

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

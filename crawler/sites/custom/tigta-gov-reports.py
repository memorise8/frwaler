# -*- coding: utf-8 -*-
"""TIGTA (Treasury Inspector General for Tax Administration) reports crawler.

Target:  https://www.tigta.gov/reports/list
API:     https://www.tigta.gov/reports/all-reports.json  (all reports, one JSON)
Detail:  No HTML detail pages — each report links directly to a PDF.
Abstract: Extracted from PDF text using pdftotext; "What TIGTA Found" section
          on the HIGHLIGHTS cover page of each audit report.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from crawler.base_crawler import BaseCrawler


def _parse_date(date_str):
    """Parse 'May 28, 2026' → '2026-05-28'. Returns None if unparseable."""
    if not date_str:
        return None
    for fmt in ('%B %d, %Y', '%b %d, %Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime('%Y-%m-%d')
        except ValueError:
            pass
    return None


class TIGTAReportsCrawler(BaseCrawler):

    site_id = "tigta-gov-reports"
    site_name = "Custom: tigta-gov-reports"
    base_url = "https://www.tigta.gov"

    _JSON_URL = "https://www.tigta.gov/reports/all-reports.json"

    # Section headings that mark the "findings" content we want
    _FOUND_PATTERNS = [
        r'What\s+TIGTA\s+Found',
        r'What\s+We\s+Found',
        r'What\s+I\s+Found',
        r'WHAT\s+TIGTA\s+FOUND',
        r'WHAT\s+WE\s+FOUND',
    ]
    # Headings that mark the end of the findings section
    _END_PATTERNS = [
        r'What\s+TIGTA\s+Recommend',
        r'What\s+We\s+Recommend',
        r'What\s+I\s+Recommend',
        r'\bRecommendation',
    ]
    # Keywords that identify boilerplate paragraphs to skip in fallback mode
    _SKIP_KW = frozenset({
        'www.tigta.gov', 'tigta.gov', 'TIGTACommunications',
        'TREASURY INSPECTOR GENERAL\nFOR TAX ADMINISTRATION',
    })

    # ------------------------------------------------------------------ helpers

    def _curl_get(self, url, retries=3, timeout=60):
        """Fetch URL via curl; returns decoded text or None on failure."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: application/json,text/html,*/*;q=0.8",
            url,
        ]
        for attempt in range(retries):
            if attempt > 0:
                wait = [1, 3, 9][min(attempt - 1, 2)]
                print(f"[tigta-gov-reports] Retrying in {wait}s (attempt {attempt+1}/{retries}): {url}")
                time.sleep(wait)
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
                raw = result.stdout
                if raw and len(raw) > 100:
                    return raw.decode('utf-8', errors='replace')
                print(f"[tigta-gov-reports] Short/empty response ({len(raw)} bytes) from {url}")
            except Exception as exc:
                print(f"[tigta-gov-reports] curl error for {url}: {exc}")
        return None

    def _download_pdf(self, url, retries=3, timeout=120):
        """Download PDF to a temp file; returns path or None on failure."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
        ]
        for attempt in range(retries):
            if attempt > 0:
                wait = [1, 3, 9][min(attempt - 1, 2)]
                print(f"[tigta-gov-reports] PDF retry in {wait}s: {url}")
                time.sleep(wait)
            tmp_path = None
            try:
                fd, tmp_path = tempfile.mkstemp(suffix='.pdf', prefix='tigta_')
                os.close(fd)
                subprocess.run(
                    cmd + [url, "-o", tmp_path],
                    capture_output=True,
                    timeout=timeout + 5,
                )
                size = os.path.getsize(tmp_path)
                if size > 5000:
                    return tmp_path
                print(f"[tigta-gov-reports] PDF too small ({size} bytes): {url}")
                os.unlink(tmp_path)
            except Exception as exc:
                print(f"[tigta-gov-reports] PDF download error: {exc}")
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass
        return None

    def _pdf_to_text(self, pdf_path, pages=3):
        """Extract text from first N pages using pdftotext; returns str."""
        try:
            result = subprocess.run(
                ["pdftotext", "-f", "1", "-l", str(pages), pdf_path, "-"],
                capture_output=True,
                timeout=30,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode('utf-8', errors='replace')
        except Exception as exc:
            print(f"[tigta-gov-reports] pdftotext error: {exc}")
        return ""

    def _extract_abstract(self, text):
        """Extract abstract from pdftotext output.

        Primary:  "What TIGTA Found" section on the HIGHLIGHTS cover page.
        Fallback: "Executive Summary" / "Introduction" / first substantive paragraphs.
        Returns empty string if nothing usable is found.
        """
        if not text:
            return ""

        text = re.sub(r'\r\n|\r', '\n', text)

        # ---- Primary: audit HIGHLIGHTS page --------------------------------
        for fp in self._FOUND_PATTERNS:
            m = re.search(fp, text, re.IGNORECASE)
            if not m:
                continue

            after = text[m.end():]

            # Find the end of this section
            end_pos = len(after)
            for ep in self._END_PATTERNS:
                em = re.search(ep, after, re.IGNORECASE)
                if em and em.start() < end_pos:
                    end_pos = em.start()

            section = after[:end_pos].strip()

            # Split into paragraphs (double-newline separated in pdftotext output)
            paras = re.split(r'\n{2,}', section)
            kept = []
            for para in paras:
                stripped = para.strip()
                if not stripped:
                    continue
                lines = [ln for ln in stripped.split('\n') if ln.strip()]
                # Skip standalone single-line headings (short, no punctuation)
                if len(lines) == 1 and len(stripped) < 40 and not re.search(r'[.,:;]', stripped):
                    continue
                # Normalize to a single space-joined string
                kept.append(' '.join(stripped.split()))

            result = '\n\n'.join(kept).strip()
            if len(result) >= 100:
                return result[:3000]
            break  # found heading but content was too short; fall through

        # ---- Fallback A: Executive Summary / Introduction ------------------
        for fb in [r'Executive\s+Summary', r'EXECUTIVE\s+SUMMARY',
                   r'\bIntroduction\b', r'\bSUMMARY\b']:
            m = re.search(fb, text, re.IGNORECASE)
            if not m:
                continue
            after = text[m.end():]
            paras = [p.strip() for p in re.split(r'\n{2,}', after) if p.strip()]
            kept = []
            total = 0
            for para in paras:
                if len(para) < 40:
                    continue
                kept.append(' '.join(para.split()))
                total += len(para)
                if total >= 1200:
                    break
            result = '\n\n'.join(kept).strip()
            if len(result) >= 100:
                return result[:3000]

        # ---- Fallback B: first substantive paragraphs ----------------------
        paras = [p.strip() for p in re.split(r'\n{2,}', text) if p.strip()]
        kept = []
        total = 0
        for para in paras:
            if len(para) < 60:
                continue
            if any(kw in para for kw in self._SKIP_KW):
                continue
            kept.append(' '.join(para.split()))
            total += len(para)
            if total >= 900:
                break
        return ('\n\n'.join(kept))[:3000]

    # ------------------------------------------------------------------ crawl

    def crawl(self, limit=None):
        start_time = time.time()
        MAX_WALL_SECONDS = 25 * 60
        limit_str = str(limit) if limit is not None else 'inf'

        print(f"[tigta-gov-reports] Starting crawl (limit={limit_str})")

        # All reports are served as a single JSON endpoint — no server-side pagination
        json_text = self._curl_get(self._JSON_URL)
        if not json_text:
            print("[tigta-gov-reports] Failed to fetch reports JSON, aborting.")
            return 0

        try:
            data = json.loads(json_text)
            all_reports = data.get('reports', [])
        except Exception as exc:
            print(f"[tigta-gov-reports] JSON parse error: {exc}")
            return 0

        print(f"[tigta-gov-reports] Found {len(all_reports)} reports in JSON")

        saved = 0
        seen_urls = set()

        for i, report in enumerate(all_reports):
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time > MAX_WALL_SECONDS:
                print(f"[tigta-gov-reports] Wall-clock budget reached at item {i}, stopping.")
                break

            if i > 0 and i % 10 == 0:
                print(f"[tigta-gov-reports] page {i // 10}: saved {saved}/{limit_str}")

            link = (report.get('link') or '').strip()
            if not link:
                continue
            pdf_url = self.base_url + link if link.startswith('/') else link
            if pdf_url in seen_urls:
                continue
            seen_urls.add(pdf_url)

            title = (report.get('title') or '').strip()
            if not title:
                continue

            report_number = (report.get('report_number') or '').strip()
            report_type = (report.get('report_type') or '').strip()
            date_str = (report.get('date') or '').strip()
            pub_date = _parse_date(date_str)

            # external_id / post_number: prefer report_number; fall back to PDF stem
            if report_number:
                external_id = report_number
                post_number = report_number
            else:
                stem = os.path.splitext(os.path.basename(link))[0]
                external_id = stem
                post_number = None

            original_filename = os.path.basename(link)

            try:
                time.sleep(self._delay)

                tmp_pdf = None
                abstract = ""
                try:
                    tmp_pdf = self._download_pdf(pdf_url)
                    if tmp_pdf:
                        pdf_text = self._pdf_to_text(tmp_pdf, pages=3)
                        abstract = self._extract_abstract(pdf_text)
                finally:
                    if tmp_pdf:
                        try:
                            os.unlink(tmp_pdf)
                        except Exception:
                            pass

                if len(abstract) < 50:
                    print(
                        f"[tigta-gov-reports] Abstract too short ({len(abstract)} chars) "
                        f"for '{title[:50]}', skipping."
                    )
                    continue

                paper = {
                    'site_id': self.site_id,
                    'external_id': external_id,
                    'post_number': post_number,
                    'title': title,
                    'abstract': abstract,
                    'published_date': pub_date,
                    'posted_date': pub_date,
                    'url': pdf_url,
                    'pdf_url': pdf_url,
                    'original_filename': original_filename,
                    'category': report_type,
                    'publisher': 'Treasury Inspector General for Tax Administration',
                    'keywords': '',
                    'metadata': json.dumps({
                        'report_number': report_number,
                        'report_type': report_type,
                        'posted_date': date_str,
                    }, ensure_ascii=False),
                }

                self._save_paper(paper)
                saved += 1
                print(f"[tigta-gov-reports] Saved [{saved}]: {title[:65]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[tigta-gov-reports] item '{title[:40]}' failed: {exc}")
                continue

        print(f"[tigta-gov-reports] Done. Saved {saved} records.")
        return saved

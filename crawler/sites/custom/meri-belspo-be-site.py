# -*- coding: utf-8 -*-
"""Custom crawler for MERI/BELSPO Research Studies publications.

Data source: MERI_publications.csv at meri.belspo.be
Starting URL: https://meri.belspo.be/site/publications_en.stm?c=Research%20studies

The site serves all publication metadata from a single semicolon-delimited CSV.
No per-paper HTML detail pages exist; abstracts are extracted from PDFs via pdftotext.
"""

import csv
import io
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.parse

from crawler.base_crawler import BaseCrawler

_LOG       = "meri-belspo-be-site"
_CSV_URL   = "https://meri.belspo.be/site/docs/publications/MERI_publications.csv"
_PUB_URL   = "https://meri.belspo.be/site/publications_en.stm?c=Research%20studies"
_PDF_BASE  = "https://meri.belspo.be/site/docs/publications/"
_TARGET_CAT = "Research studies"
# Columns whose values carry forward to empty rows (mirrors CSVfile 'copy' option)
_CARRY_COLS = ["category", "sub_category", "description", "image"]


class MeriBelspoBeSiteCrawler(BaseCrawler):
    site_id   = "meri-belspo-be-site"
    site_name = "Custom: meri-belspo-be-site"
    base_url  = "https://meri.belspo.be"

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url, retries=3, binary=False):
        """Fetch URL via curl with TLS 1.3 workaround. Returns bytes/str or None."""
        delays = [1, 3, 9]
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    ["curl", "--tls-max", "1.3", "-sk", "--max-time", "90", url],
                    capture_output=True,
                    timeout=100,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout if binary else result.stdout.decode("utf-8", errors="replace")
                if attempt < retries - 1:
                    time.sleep(delays[attempt])
            except Exception as exc:
                print(f"[{_LOG}] curl error attempt {attempt + 1}/{retries}: {exc}")
                if attempt < retries - 1:
                    time.sleep(delays[attempt])
        return None

    # ------------------------------------------------------------------
    # PDF abstract extraction
    # ------------------------------------------------------------------

    def _pdf_abstract(self, pdf_url):
        """Download PDF, run pdftotext on pages 1-2, return best paragraph or None."""
        pdf_bytes = self._curl_get(pdf_url, retries=3, binary=True)
        if not pdf_bytes or len(pdf_bytes) < 512:
            return None
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
                fh.write(pdf_bytes)
                tmp_path = fh.name
            res = subprocess.run(
                ["pdftotext", "-f", "1", "-l", "2", tmp_path, "-"],
                capture_output=True,
                timeout=30,
            )
            if res.returncode != 0 or not res.stdout:
                return None
            text = res.stdout.decode("utf-8", errors="replace").strip()
            return self._best_paragraph(text) if text else None
        except Exception as exc:
            print(f"[{_LOG}] pdftotext failed for {pdf_url}: {exc}")
            return None
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    def _best_paragraph(self, text, min_len=80):
        """Return the longest substantial paragraph (≤2000 chars) from raw PDF text."""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        paragraphs = []
        buf = []
        for line in lines:
            if len(line) >= 25:
                buf.append(line)
            else:
                if buf:
                    paragraphs.append(" ".join(buf))
                buf = []
        if buf:
            paragraphs.append(" ".join(buf))
        best = max((p for p in paragraphs if len(p) >= min_len), key=len, default=None)
        if best:
            return best[:2000]
        combined = " ".join(lines)
        return combined[:2000] if len(combined) >= min_len else None

    # ------------------------------------------------------------------
    # Field helpers
    # ------------------------------------------------------------------

    def _win_path_to_url(self, win_path):
        """Convert Windows CSV path to (pdf_url, filename) or (None, None)."""
        if not win_path or not win_path.strip():
            return None, None
        filename = win_path.replace("\\", "/").split("/")[-1].strip()
        if not filename.lower().endswith(".pdf"):
            return None, None
        return _PDF_BASE + urllib.parse.quote(filename), filename

    def _extract_number(self, title1):
        """'No. 13' → '13'; fallback to slug or None."""
        if not title1:
            return None
        m = re.search(r"\d+", title1)
        if m:
            return m.group(0)
        slug = re.sub(r"\W+", "-", title1.strip()).strip("-")
        return slug or None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start    = time.time()
        max_secs = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
        limit_n  = float("inf") if limit is None else int(limit)

        # ── Step 1: fetch CSV ─────────────────────────────────────────
        print(f"[{_LOG}] Fetching CSV: {_CSV_URL}")
        raw = self._curl_get(_CSV_URL)
        if not raw:
            print(f"[{_LOG}] Failed to fetch CSV — aborting")
            return 0

        # Strip UTF-8 BOM
        raw = raw.lstrip("﻿")

        try:
            reader  = csv.DictReader(io.StringIO(raw), delimiter=";")
            all_rows = list(reader)
        except Exception as exc:
            print(f"[{_LOG}] CSV parse error: {exc}")
            return 0

        print(f"[{_LOG}] CSV loaded: {len(all_rows)} total rows")

        # ── Step 2: carry-forward (mirrors CSVfile 'copy' option) ─────
        carry = {c: "" for c in _CARRY_COLS}
        processed = []
        for row in all_rows:
            r = dict(row)
            for col in _CARRY_COLS:
                val = r.get(col, "").strip()
                if val:
                    carry[col] = val
                else:
                    r[col] = carry[col]
            processed.append(r)

        # ── Step 3: filter to "Research studies" category ─────────────
        rs_rows = [r for r in processed if r.get("category", "").strip() == _TARGET_CAT]
        print(f"[{_LOG}] '{_TARGET_CAT}' rows: {len(rs_rows)}")

        seen_urls: set = set()
        saved = 0

        for idx, row in enumerate(rs_rows):
            if saved >= limit_n:
                break
            if time.time() - start > max_secs:
                print(f"[{_LOG}] 25-min wall-clock budget reached at item {idx}, stopping")
                break

            if idx > 0 and idx % 10 == 0:
                print(f"[{_LOG}] page 1: saved {saved}/{limit_n} (item {idx})")

            try:
                title1      = row.get("title 1", "").strip()
                title2      = row.get("title 2", "").strip()
                authors_raw = row.get("authors", "").strip()
                year        = row.get("year", "").strip()
                description = row.get("description", "").strip()
                sub_cat     = row.get("sub_category", "").strip()

                # Pick best download (EN preferred, then FR, then NL)
                win_en   = row.get("download_EN", "").strip()
                win_fr   = row.get("download_FR", "").strip()
                win_nl   = row.get("download_NL", "").strip()
                win_path = win_en or win_fr or win_nl
                pdf_url, original_filename = self._win_path_to_url(win_path)

                # Deduplication
                dedup_key = pdf_url or f"{title1}||{title2}||{year}"
                if not dedup_key:
                    continue
                if dedup_key in seen_urls:
                    continue
                seen_urls.add(dedup_key)

                # Full title
                if title1 and title2:
                    full_title = f"{title1}: {title2}"
                else:
                    full_title = (title1 or title2 or "").strip()
                if not full_title:
                    print(f"[{_LOG}] item {idx}: no title, skipping")
                    continue

                post_number = self._extract_number(title1)
                external_id = f"RS-{post_number}" if post_number else f"rs-idx-{idx}"

                # ── Abstract: PDF → description → constructed ──────────
                abstract = None
                if pdf_url:
                    print(f"[{_LOG}] item {idx}: extracting abstract from {original_filename}")
                    abstract = self._pdf_abstract(pdf_url)
                    if abstract:
                        print(f"[{_LOG}] item {idx}: PDF abstract {len(abstract)} chars")

                if not abstract and description:
                    abstract = description

                if not abstract:
                    abstract = (
                        f"{full_title}. Published by BELSPO-MERI in {year}. "
                        f"Authors: {authors_raw}. "
                        f"Part of the MERI {_TARGET_CAT} series."
                    )

                if len(abstract) < 50:
                    print(f"[{_LOG}] item {idx} '{full_title[:40]}': abstract <50 chars, skipping")
                    continue

                # Date (only year in CSV)
                pub_date = f"{year}-01-01" if year and re.match(r"^\d{4}$", year) else None

                # Use PDF URL as item URL (no per-paper HTML page exists)
                item_url = pdf_url or _PUB_URL

                meta = {
                    "title_1":             title1,
                    "title_2":             title2,
                    "sub_category":        sub_cat,
                    "category_description": description,
                    "download_EN":         win_en,
                    "download_FR":         win_fr,
                    "download_NL":         win_nl,
                    "originalFilename":    original_filename,
                }

                paper = {
                    "site_id":           self.site_id,
                    "external_id":       external_id,
                    "post_number":       post_number,
                    "title":             full_title,
                    "abstract":          abstract,
                    "url":               item_url,
                    "pdf_url":           pdf_url,
                    "original_filename": original_filename,
                    "authors":           authors_raw,
                    "publisher":         "BELSPO - MERI",
                    "published_date":    pub_date,
                    "listed_date":       pub_date,
                    "keywords":          None,
                    "category":          _TARGET_CAT,
                    "doi":               None,
                    "metadata":          json.dumps(meta, ensure_ascii=False),
                }

                self._save_paper(paper)
                saved += 1
                print(f"[{_LOG}] saved {saved}: {full_title[:60]}")

                time.sleep(self._delay)

            except KeyboardInterrupt:
                print(f"[{_LOG}] KeyboardInterrupt at item {idx} — stopping")
                raise
            except Exception as exc:
                print(f"[{_LOG}] item {idx} failed: {exc}")
                continue

        print(f"[{_LOG}] Done: {saved} items saved")
        return saved

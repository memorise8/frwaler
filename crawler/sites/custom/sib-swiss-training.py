# -*- coding: utf-8 -*-
"""SIB Swiss Training Materials crawler.

Fetches all courses from https://www.sib.swiss/training/training-materials.
The listing page embeds all course data as JSON-LD schema.org/Course blocks,
so no separate detail-page fetching is required.
"""

import json
import os
import re
import subprocess
import sys
import time

# Absolute import so spec_from_file_location (no package context) works.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from crawler.base_crawler import BaseCrawler  # noqa: E402

# ---------------------------------------------------------------------------
# BeautifulSoup with fallback chain (html5lib → lxml → html.parser)
# ---------------------------------------------------------------------------
_BS_PARSERS = []
try:
    import bs4 as _bs4  # noqa: F401 — just check availability
    _BS_PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    pass

try:
    from bs4 import BeautifulSoup as _BeautifulSoup
except ImportError:
    _BeautifulSoup = None


def _make_soup(raw):
    if _BeautifulSoup is None:
        return None
    for parser in _BS_PARSERS:
        try:
            return _BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Network helper
# ---------------------------------------------------------------------------

def _curl_get(url, retries=3):
    """Fetch *url* with curl; returns decoded text or None on failure."""
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ["curl", "-skL", "--tls-max", "1.3", "--max-time", "30", url],
                capture_output=True,
                timeout=40,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
            if result.returncode != 0:
                print(
                    f"[sib-swiss-training] curl rc={result.returncode} "
                    f"attempt {attempt + 1}/{retries}: {url}"
                )
        except Exception as exc:
            print(f"[sib-swiss-training] curl error attempt {attempt + 1}/{retries}: {exc}")
        if attempt < retries - 1:
            wait = [1, 3, 9][min(attempt, 2)]
            time.sleep(wait)
    return None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _extract_courses(raw):
    """Return a list of JSON-LD Course dicts from *raw* HTML."""
    courses = []
    blocks = re.findall(
        r"<script\s[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        raw,
        re.DOTALL | re.IGNORECASE,
    )
    for b in blocks:
        try:
            d = json.loads(b)
            if d.get("@type") == "Course":
                courses.append(d)
        except Exception:
            continue
    return courses


def _extract_code(url):
    """Extract a course code from a URL like /training/course/20260504_MEMIC."""
    if not url:
        return None
    m = re.search(r"/training/course/([^/?#\s]+)$", url)
    return m.group(1) if m else None


def _course_to_record(course):
    """Convert a JSON-LD Course dict to a paper_dict suitable for _save_paper."""
    name = (course.get("name") or "").strip()
    description = (course.get("description") or "").strip()
    abstract = (course.get("abstract") or "").strip()

    # Prefer the schema.org `abstract` field; fall back to `description`.
    if len(abstract) < 50:
        abstract = description

    # Keywords
    kw_raw = course.get("keywords", "")
    if isinstance(kw_raw, list):
        kw_list = [str(k).strip() for k in kw_raw if str(k).strip()]
    elif kw_raw:
        kw_list = [k.strip() for k in str(kw_raw).split(",") if k.strip()]
    else:
        kw_list = []
    keywords = json.dumps(kw_list)

    # Category
    edu_level = course.get("educationalLevel", "")
    if isinstance(edu_level, list):
        category = ", ".join(edu_level)
    else:
        category = str(edu_level or "")

    # First hasCourseInstance carries the canonical URL, instructors, and date.
    instances = course.get("hasCourseInstance", [])
    url = ""
    external_id = None
    authors = []
    published_date = None

    if instances:
        inst = instances[0]
        url = inst.get("url") or inst.get("@id") or ""
        external_id = _extract_code(url)

        instructors = inst.get("instructor", [])
        if isinstance(instructors, list):
            authors = [i.get("name", "").strip() for i in instructors if i.get("name")]
        elif isinstance(instructors, dict):
            n = instructors.get("name", "").strip()
            if n:
                authors = [n]

        published_date = inst.get("startDate") or inst.get("endDate")

    # Fallback external_id derived from the course name.
    if not external_id:
        external_id = re.sub(r"[^\w]", "_", name)[:60]

    return {
        "external_id": external_id,
        "title": name,
        "abstract": abstract,
        "authors": json.dumps(authors),
        "keywords": keywords,
        "category": category,
        "published_date": published_date,
        "url": url,
        "pdf_url": None,
        "doi": None,
        "department": None,
        "metadata": json.dumps(
            {
                "instance_count": len(instances),
                "language": course.get("inLanguage", ""),
                "educationalLevel": category,
            }
        ),
    }


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class SibSwissTrainingCrawler(BaseCrawler):
    """Crawler for SIB Swiss training materials."""

    site_id = "sib-swiss-training"
    site_name = "Custom: sib-swiss-training"
    base_url = "https://www.sib.swiss"

    _LISTING_URL = "https://www.sib.swiss/training/training-materials"
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _WALL_CLOCK_S = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

    def crawl(self, limit=None):
        """Crawl training materials and persist to DB.

        Iterates listing pages until ``limit`` is reached, no new records
        appear, or safety caps are hit.
        """
        saved = 0
        seen_keys = set()       # URL-level dedup prevents infinite pager loops
        start_ts = time.time()
        limit_label = str(limit) if limit is not None else "∞"

        page = 0
        while page < self._MAX_PAGES:
            # Wall-clock budget
            elapsed = time.time() - start_ts
            if elapsed >= self._WALL_CLOCK_S:
                print(
                    f"[sib-swiss-training] wall-clock limit reached "
                    f"({elapsed:.0f}s), stopping cleanly"
                )
                break

            # Build page URL
            url = (
                self._LISTING_URL
                if page == 0
                else f"{self._LISTING_URL}?page={page}"
            )

            raw = _curl_get(url)
            if not raw:
                print(f"[sib-swiss-training] page {page}: fetch failed, stopping")
                break

            courses = _extract_courses(raw)
            if not courses:
                print(f"[sib-swiss-training] page {page}: 0 courses found, stopping")
                break

            new_this_page = 0
            for course in courses:
                if limit is not None and saved >= limit:
                    break

                # Dedup key: name + first instance URL
                insts = course.get("hasCourseInstance", [])
                inst_url = insts[0].get("url", "") if insts else ""
                key = course.get("name", "") + "|" + inst_url
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                new_this_page += 1

                try:
                    record = _course_to_record(course)

                    if len(record.get("abstract", "")) < 50:
                        print(
                            f"[sib-swiss-training] skip '{record['title'][:50]}': "
                            "abstract too short (<50 chars)"
                        )
                        continue

                    record["site_id"] = self.site_id
                    self._save_paper(record)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    title = course.get("name", "?")[:50]
                    print(f"[sib-swiss-training] item '{title}' failed: {exc}")
                    continue

            # Progress log every 10 pages
            if page % 10 == 0:
                print(
                    f"[sib-swiss-training] page {page}: "
                    f"saved {saved}/{limit_label}"
                )

            if new_this_page == 0:
                print(
                    f"[sib-swiss-training] page {page}: "
                    "no new records (pagination end or loop detected), stopping"
                )
                break

            if limit is not None and saved >= limit:
                break

            page += 1
            time.sleep(1.0)

        if page >= self._MAX_PAGES:
            print(
                f"[sib-swiss-training] safety cap of {self._MAX_PAGES} pages reached, stopping"
            )

        return saved

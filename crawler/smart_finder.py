# -*- coding: utf-8 -*-
"""Smart Document Finder - finds all downloadable documents from any URL."""
import os
import re
import time
import json
import hashlib
import requests
from urllib.parse import urlparse, urljoin, parse_qs, urlencode, urlunparse
from bs4 import BeautifulSoup
from dataclasses import dataclass, field
from typing import Optional


# Downloadable file extensions
FILE_EXTENSIONS = (
    '.pdf', '.xlsx', '.xls', '.csv', '.hwp', '.hwpx',
    '.docx', '.doc', '.pptx', '.ppt', '.zip', '.rar',
    '.json', '.xml', '.txt', '.rtf', '.odt', '.ods',
)

# Korean/English download-related link text patterns
DOWNLOAD_TEXT_PATTERNS = [
    '다운로드', '다운', '내려받기', '첨부파일', '첨부', '보고서', '원문',
    '전문보기', 'PDF', 'HWP', 'EXCEL', 'download', 'Download', 'DOWNLOAD',
    '파일받기', '자료받기', '문서', '보도자료',
]

# URL patterns that suggest downloads
DOWNLOAD_URL_PATTERNS = [
    r'download', r'attach', r'fileDown', r'file_down', r'getFile',
    r'cmm/fms/FileDown', r'downFile', r'pdfView', r'hwpView',
    r'fileSrc', r'fileStore', r'boardFile', r'atchFile',
    r'FileDown\.do', r'download\.do', r'atchFileId', r'fileDown\.do',
    r'boardFileDown', r'file/download', r'downFile\.do',
    r'fileDownload', r'getFileDown', r'atch_downld',
]

# Pagination parameter names
PAGE_PARAMS = [
    'page', 'pageIndex', 'pageNo', 'pg', 'p', 'offset',
    'pageNum', 'currentPage', 'nPage', 'pageno',
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


@dataclass
class FoundDocument:
    """A discovered document/file."""
    title: str
    file_url: str
    file_type: str  # pdf, hwp, xlsx, etc.
    source_page: str  # URL where this was found
    size: Optional[int] = None
    date: Optional[str] = None

    @property
    def id(self):
        return hashlib.sha256(self.file_url.encode()).hexdigest()[:16]


@dataclass
class FinderResult:
    """Result of a smart find operation."""
    url: str
    documents: list = field(default_factory=list)
    pages_scanned: int = 0
    detail_pages_visited: int = 0
    fetch_method: str = "requests"
    errors: list = field(default_factory=list)
    progress: str = ""


class SmartDocumentFinder:
    """Finds all downloadable documents from any URL without config files."""

    def __init__(self, delay=1.5, respect_robots=False, callback=None):
        """
        Args:
            delay: Seconds between requests
            respect_robots: Whether to check robots.txt
            callback: Optional function(progress_str) for real-time updates
        """
        self._delay = delay
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        })
        self._respect_robots = respect_robots
        self._fetch_method = None  # Auto-detect
        self._seen_urls = set()
        self._callback = callback
        self._browser = None
        self._playwright = None
        self._page = None  # Reuse same page for session persistence

    def _update_progress(self, msg):
        if self._callback:
            self._callback(msg)
        print(f"[SmartFinder] {msg}")

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    def _fetch(self, url, timeout=15, force_browser=False):
        """Fetch a page, auto-selecting the best method."""
        if force_browser or self._fetch_method == "browser":
            return self._fetch_browser(url, timeout)

        # Try requests first
        time.sleep(self._delay)
        try:
            resp = self._session.get(url, timeout=timeout, allow_redirects=True)
            if resp.status_code < 400 and len(resp.text) > 500:
                self._fetch_method = self._fetch_method or "requests"
                return resp.text, resp.url
        except Exception:
            pass

        # Try cloudscraper
        try:
            import cloudscraper
            scraper = cloudscraper.create_scraper()
            resp = scraper.get(url, timeout=timeout)
            if resp.status_code < 400 and len(resp.text) > 500:
                self._fetch_method = "cloudscraper"
                return resp.text, resp.url
        except Exception:
            pass

        # Fall back to browser
        return self._fetch_browser(url, timeout)

    def _fetch_browser(self, url, timeout=15):
        """Fetch using headless browser. Reuses same page for session persistence."""
        try:
            if not self._playwright:
                from playwright.sync_api import sync_playwright
                self._playwright = sync_playwright().start()
                self._browser = self._playwright.chromium.launch(headless=True)
                self._page = self._browser.new_page(user_agent=USER_AGENT)

            self._page.goto(url, timeout=timeout * 1000, wait_until="networkidle")
            html = self._page.content()
            final_url = self._page.url
            self._fetch_method = "browser"
            return html, final_url
        except Exception as e:
            # If page crashed, create a new one
            try:
                self._page = self._browser.new_page(user_agent=USER_AGENT)
            except Exception:
                pass
            return None, url

    def _close_browser(self):
        if self._page:
            try:
                self._page.close()
            except Exception:
                pass
            self._page = None
        if self._browser:
            try:
                self._browser.close()
                self._playwright.stop()
            except Exception:
                pass
            self._browser = None
            self._playwright = None

    # ------------------------------------------------------------------
    # File detection
    # ------------------------------------------------------------------

    def _is_file_url(self, href):
        """Check if URL points to a downloadable file."""
        if not href:
            return False, None

        href_lower = href.lower().split('?')[0].split('#')[0]

        # Check extension
        for ext in FILE_EXTENSIONS:
            if href_lower.endswith(ext):
                return True, ext[1:]  # Return type without dot

        # Check URL patterns
        for pattern in DOWNLOAD_URL_PATTERNS:
            if re.search(pattern, href, re.I):
                # Try to determine file type from URL
                for ext in FILE_EXTENSIONS:
                    if ext[1:] in href_lower:
                        return True, ext[1:]
                return True, "unknown"

        # Check query parameters for download hints
        from urllib.parse import urlparse as _urlparse
        query = _urlparse(href).query.lower()
        if query and any(p in query for p in ['atchfileid', 'fileid', 'filesn', 'file_sn', 'downtype', 'file_no']):
            return True, "unknown"

        return False, None

    def _is_download_link(self, tag):
        """Check if an <a> tag is a download link based on text/attributes."""
        # Check text content
        text = tag.get_text(strip=True).lower()
        for pattern in DOWNLOAD_TEXT_PATTERNS:
            if pattern.lower() in text:
                return True

        # Check title/alt attributes
        for attr in ['title', 'alt', 'aria-label']:
            val = (tag.get(attr, '') or '').lower()
            for pattern in DOWNLOAD_TEXT_PATTERNS:
                if pattern.lower() in val:
                    return True

        # Check class/id for download-related names
        classes = ' '.join(tag.get('class', []))
        tag_id = tag.get('id', '')
        for pattern in ['download', 'attach', 'file', 'btn_down']:
            if pattern in classes.lower() or pattern in tag_id.lower():
                return True

        return False

    def _extract_file_links(self, soup, page_url):
        """Extract all file download links from a page."""
        documents = []

        for a_tag in soup.select("a[href]"):
            href = a_tag.get("href", "").strip()
            if not href or href.startswith("javascript:") or href == "#":
                continue

            abs_url = urljoin(page_url, href)

            if abs_url in self._seen_urls:
                continue

            is_file, file_type = self._is_file_url(href)

            if is_file:
                # Confirmed file URL by extension or URL pattern
                self._seen_urls.add(abs_url)
                title = (
                    a_tag.get_text(strip=True)
                    or os.path.basename(urlparse(abs_url).path)
                    or "Untitled"
                )
                documents.append(FoundDocument(
                    title=title[:200],
                    file_url=abs_url,
                    file_type=file_type or "unknown",
                    source_page=page_url,
                ))
            else:
                # Check if link text contains a filename with extension
                link_text = a_tag.get_text(strip=True).lower()
                text_file_type = None
                for ext in FILE_EXTENSIONS:
                    if ext in link_text:
                        text_file_type = ext[1:]
                        break

                if text_file_type:
                    # Link text has file extension (e.g., "report.pdf", "pressoffice_20260402.hwp")
                    self._seen_urls.add(abs_url)
                    documents.append(FoundDocument(
                        title=a_tag.get_text(strip=True)[:200],
                        file_url=abs_url,
                        file_type=text_file_type,
                        source_page=page_url,
                    ))
                elif self._is_download_link(a_tag):
                    # Text suggests download - verify with HEAD request
                    actual_type = self._detect_file_type_head(abs_url)
                    if actual_type:
                        self._seen_urls.add(abs_url)
                        title = (
                            a_tag.get_text(strip=True)
                            or os.path.basename(urlparse(abs_url).path)
                            or "Untitled"
                        )
                        documents.append(FoundDocument(
                            title=title[:200],
                            file_url=abs_url,
                            file_type=actual_type,
                            source_page=page_url,
                        ))

        # Also check onclick handlers
        for tag in soup.select("[onclick]"):
            onclick = tag.get("onclick", "")
            # Extract URLs from onclick
            urls = re.findall(
                r"['\"]((https?://[^'\"]+|/[^'\"]+\.[a-zA-Z]{2,5}[^'\"]*?))['\"]",
                onclick,
            )
            for url_match in urls:
                url_str = url_match[0]
                abs_url = urljoin(page_url, url_str)
                is_file, file_type = self._is_file_url(abs_url)
                if is_file and abs_url not in self._seen_urls:
                    self._seen_urls.add(abs_url)
                    title = tag.get_text(strip=True) or "Untitled"
                    documents.append(FoundDocument(
                        title=title[:200],
                        file_url=abs_url,
                        file_type=file_type or "unknown",
                        source_page=page_url,
                    ))

        # Check onclick handlers for downloads (aggressive extraction)
        onclick_docs = self._extract_onclick_downloads(soup, page_url)
        documents.extend(onclick_docs)

        # Check iframes for embedded files (e.g., PDF.js viewer)
        for iframe in soup.select("iframe[src]"):
            src = iframe.get("src", "")
            if not src:
                continue
            # PDF.js viewer pattern: viewer.html?file=https%3A%2F%2F...
            if "viewer" in src and "file=" in src:
                from urllib.parse import unquote
                file_param = src.split("file=")[-1].split("&")[0]
                file_url = unquote(file_param)
                abs_url = urljoin(page_url, file_url)
                if abs_url not in self._seen_urls:
                    self._seen_urls.add(abs_url)
                    is_file, file_type = self._is_file_url(abs_url)
                    documents.append(FoundDocument(
                        title=soup.select_one("title").get_text(strip=True)[:200] if soup.select_one("title") else "Untitled",
                        file_url=abs_url,
                        file_type=file_type or "pdf",
                        source_page=page_url,
                    ))
            else:
                # Direct file in iframe
                abs_src = urljoin(page_url, src)
                is_file, file_type = self._is_file_url(abs_src)
                if is_file and abs_src not in self._seen_urls:
                    self._seen_urls.add(abs_src)
                    documents.append(FoundDocument(
                        title=soup.select_one("title").get_text(strip=True)[:200] if soup.select_one("title") else "Untitled",
                        file_url=abs_src,
                        file_type=file_type or "unknown",
                        source_page=page_url,
                    ))

        return documents

    def _extract_onclick_downloads(self, soup, page_url):
        """Extract download URLs from onclick handlers."""
        documents = []

        for tag in soup.select("[onclick]"):
            onclick = tag.get("onclick", "")
            text = tag.get_text(strip=True)

            # Pattern 1: location.href = '/download/...'
            loc_matches = re.findall(r"location\.href\s*=\s*['\"]([^'\"]+)['\"]", onclick)

            # Pattern 2: window.open('/download/...')
            open_matches = re.findall(r"window\.open\s*\(\s*['\"]([^'\"]+)['\"]", onclick)

            # Pattern 3: Common Korean gov download functions
            # fn_download('fileId', 'fileSn'), goFileDown('id'), etc.
            fn_matches = re.findall(
                r"(?:fn_download|downloadFile|fileDown|fn_fileDown|goDownload|goFileDown|fnDown|fn_egov_downFile|fncFileDown)\s*\(\s*['\"]([^'\"]*)['\"](?:\s*,\s*['\"]([^'\"]*)['\"])?",
                onclick
            )

            for match in loc_matches + open_matches:
                abs_url = urljoin(page_url, match)
                is_file, file_type = self._is_file_url(abs_url)
                if is_file or any(p in match.lower() for p in ['download', 'file', 'attach', 'down']):
                    if abs_url not in self._seen_urls:
                        self._seen_urls.add(abs_url)
                        documents.append(FoundDocument(
                            title=text[:200] or "Untitled",
                            file_url=abs_url,
                            file_type=file_type or "unknown",
                            source_page=page_url,
                        ))

        return documents

    def _intercept_downloads(self, page_url):
        """Try clicking download buttons and intercept file downloads via Playwright."""
        if not self._page:
            return []

        documents = []
        download_selectors = [
            "a[onclick*=download]", "a[onclick*=Download]",
            "a[onclick*=fileDown]", "a[onclick*=fn_down]",
            "button[onclick*=download]", "button[onclick*=fileDown]",
            "a[onclick*=goDown]", "a[onclick*=fnDown]",
        ]

        for selector in download_selectors:
            try:
                elements = self._page.query_selector_all(selector)
                for el in elements[:10]:
                    try:
                        with self._page.expect_download(timeout=5000) as download_info:
                            el.click()
                        download = download_info.value
                        fname = download.suggested_filename
                        ftype = fname.rsplit(".", 1)[-1].lower() if "." in fname else "unknown"
                        doc_url = download.url
                        download.cancel()  # Don't actually save the file

                        if doc_url not in self._seen_urls:
                            self._seen_urls.add(doc_url)
                            documents.append(FoundDocument(
                                title=fname,
                                file_url=doc_url,
                                file_type=ftype,
                                source_page=page_url,
                            ))
                    except Exception:
                        pass  # No download triggered
            except Exception:
                pass

        return documents

    def _detect_file_type_head(self, url):
        """Use HEAD request to detect file type from Content-Type/Disposition."""
        try:
            resp = self._session.head(url, timeout=5, allow_redirects=True)
            ct = resp.headers.get("content-type", "").lower()
            cd = resp.headers.get("content-disposition", "").lower()

            type_map = {
                "pdf": "pdf", "excel": "xlsx", "spreadsheet": "xlsx",
                "hwp": "hwp", "word": "docx", "powerpoint": "pptx",
                "csv": "csv", "zip": "zip", "json": "json", "xml": "xml",
            }
            for key, ftype in type_map.items():
                if key in ct or key in cd:
                    return ftype

            # Check filename in Content-Disposition
            if cd:
                for ext in FILE_EXTENSIONS:
                    if ext in cd:
                        return ext[1:]
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Pagination detection
    # ------------------------------------------------------------------

    def _detect_pagination(self, soup, current_url):
        """Detect pagination and return list of page URLs."""
        parsed = urlparse(current_url)
        params = parse_qs(parsed.query, keep_blank_values=True)

        # Method 1: Look for page parameter in current URL
        current_page_param = None
        current_page_val = None
        for param in PAGE_PARAMS:
            if param in params:
                current_page_param = param
                try:
                    current_page_val = int(params[param][0])
                except (ValueError, IndexError):
                    pass
                break

        # Method 2: Look for pagination links in HTML
        pagination_links = set()

        # Find links that look like page numbers
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            text = a.get_text(strip=True)

            # "다음", "Next", ">"
            if text in [
                "다음", "다음 페이지", "Next", "next", ">", "\u203a", "\u00bb",
                "다음페이지",
            ]:
                abs_url = urljoin(current_url, href)
                if abs_url != current_url and abs_url not in self._seen_urls:
                    pagination_links.add(abs_url)

            # Numbered page links (2, 3, 4...)
            if text.isdigit() and int(text) > 1:
                abs_url = urljoin(current_url, href)
                if abs_url != current_url:
                    pagination_links.add(abs_url)

        # Method 3: Generate page URLs from parameter pattern
        if current_page_param and current_page_val is not None:
            for page_num in range(current_page_val + 1, current_page_val + 50):
                new_params = dict(params)
                new_params[current_page_param] = [str(page_num)]
                query = urlencode({k: v[0] for k, v in new_params.items()})
                new_url = urlunparse(parsed._replace(query=query))
                pagination_links.add(new_url)

        return list(pagination_links)

    # ------------------------------------------------------------------
    # Detail page detection
    # ------------------------------------------------------------------

    def _find_detail_links(self, soup, page_url):
        """Find links to detail/article pages (not file links, not navigation)."""
        base_domain = urlparse(page_url).netloc
        current_path = urlparse(page_url).path

        # Strategy 1: Look for "view" URLs (most Korean gov boards use view.do, subview.do, etc.)
        view_links = []
        for a in soup.select("a[href]"):
            href = a.get("href", "").strip()
            if not href or href.startswith("#") or href.startswith("javascript:"):
                continue
            abs_url = urljoin(page_url, href)
            parsed = urlparse(abs_url)
            if parsed.netloc != base_domain:
                continue
            # "view" pattern in URL suggests article detail page
            path_lower = parsed.path.lower()
            if any(v in path_lower for v in ["view.do", "view.jsp", "subview.do", "detail", "/view/"]):
                text = a.get_text(strip=True)
                if len(text) > 5:
                    view_links.append({"url": abs_url, "text": text[:100]})

        if view_links:
            return [l for l in view_links if l["url"] not in self._seen_urls][:50]

        # Strategy 2: Find repeating link patterns, prefer longer text (articles vs nav)
        link_parents = {}
        for a in soup.select("a[href]"):
            href = a.get("href", "").strip()
            if not href or href.startswith("#") or href.startswith("javascript:"):
                continue
            abs_url = urljoin(page_url, href)
            parsed = urlparse(abs_url)
            if parsed.netloc != base_domain:
                continue
            is_file, _ = self._is_file_url(href)
            if is_file:
                continue
            # Skip links to same page (pagination, anchors)
            if parsed.path == current_path:
                continue

            parent = a.parent
            if parent:
                pid = id(parent.parent) if parent.parent else id(parent)
                if pid not in link_parents:
                    link_parents[pid] = []
                link_parents[pid].append({
                    "url": abs_url,
                    "text": a.get_text(strip=True)[:100],
                })

        # Score groups: prefer groups with longer average text (articles > nav)
        best_group = []
        best_score = 0
        for pid, links in link_parents.items():
            if len(links) < 3:
                continue
            avg_text_len = sum(len(l["text"]) for l in links) / len(links)
            score = len(links) * avg_text_len
            if score > best_score:
                best_score = score
                best_group = links

        return [l for l in best_group if len(l["text"]) > 5 and l["url"] not in self._seen_urls][:50]

    # ------------------------------------------------------------------
    # Recursive page exploration
    # ------------------------------------------------------------------

    def _explore_page(self, url, depth, result, max_pages=50):
        """Recursively explore a page for documents."""
        html, final_url = self._fetch(url, force_browser=True)
        if not html:
            return

        soup = BeautifulSoup(html, "html.parser")

        # Find files on this page
        docs = self._extract_file_links(soup, final_url)
        result.documents.extend(docs)

        # Try Playwright download interception if we have a page
        if self._page and depth <= 1:  # Only on detail pages
            intercepted = self._intercept_downloads(final_url)
            result.documents.extend(intercepted)

        result.pages_scanned += 1

        if depth <= 0:
            return

        # Find and follow detail links
        detail_links = self._find_detail_links(soup, final_url)
        if detail_links:
            self._update_progress(f"depth={depth}: {len(detail_links)}개 하위 링크 발견")
            for i, link in enumerate(detail_links):
                if link["url"] in self._seen_urls:
                    continue
                self._seen_urls.add(link["url"])

                self._explore_page(link["url"], depth - 1, result, max_pages)
                result.detail_pages_visited += 1

                if (i + 1) % 10 == 0:
                    self._update_progress(
                        f"depth={depth}: {i+1}/{len(detail_links)} 탐색, "
                        f"총 파일 {len(result.documents)}개"
                    )

    # ------------------------------------------------------------------
    # AI-assisted analysis (optional)
    # ------------------------------------------------------------------

    def _ai_analyze(self, html, url):
        """Use GPT to analyze page structure when pattern matching fails."""
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return None

        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key)

            # Clean HTML to reduce tokens
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup.select("script, style, noscript, svg, path"):
                tag.decompose()
            clean_text = soup.get_text(strip=True)[:3000]

            # Also get a sample of the HTML structure
            body = soup.select_one("body")
            structure = str(body)[:2000] if body else str(soup)[:2000]

            response = client.chat.completions.create(
                model="gpt-5.4-mini",
                messages=[{
                    "role": "system",
                    "content": (
                        "You analyze web pages to find downloadable documents. "
                        "Respond in JSON only."
                    ),
                }, {
                    "role": "user",
                    "content": (
                        f"Analyze this page and find document download links.\n"
                        f"URL: {url}\n\n"
                        f"Page text (truncated):\n{clean_text[:1500]}\n\n"
                        f"HTML structure (truncated):\n{structure[:1500]}\n\n"
                        f"Respond with JSON:\n"
                        f'{{\n'
                        f'    "has_documents": true/false,\n'
                        f'    "document_links_css": "CSS selector for document links",\n'
                        f'    "file_links_css": "CSS selector for direct file download links",\n'
                        f'    "pagination_css": "CSS selector for next page link if any",\n'
                        f'    "notes": "brief description"\n'
                        f'}}'
                    ),
                }],
                temperature=0,
                max_completion_tokens=300,
            )

            text = response.choices[0].message.content.strip()
            # Extract JSON from response
            if "```" in text:
                text = text.split("```")[1].strip()
                if text.startswith("json"):
                    text = text[4:].strip()
            return json.loads(text)
        except Exception as e:
            print(f"[SmartFinder] AI analysis error: {e}")
            return None

    # ------------------------------------------------------------------
    # Main find method
    # ------------------------------------------------------------------

    def find(self, url, max_pages=50, max_depth=3, use_ai=True) -> FinderResult:
        """Find all downloadable documents from a URL.

        Args:
            url: Starting URL
            max_pages: Maximum number of list pages to scan
            max_depth: How deep to follow detail links (0=list only, 1=list+detail)
            use_ai: Whether to use AI for analysis when pattern matching fails

        Returns:
            FinderResult with all found documents
        """
        result = FinderResult(url=url)
        self._seen_urls = set()

        try:
            self._update_progress(f"페이지 접근 중: {url[:60]}...")

            # Step 1: Fetch the initial page (always use browser first for accurate analysis)
            html, final_url = self._fetch(url, force_browser=True)
            if not html:
                result.errors.append(f"페이지 접근 실패: {url}")
                return result

            result.fetch_method = self._fetch_method or "requests"
            self._update_progress(f"접근 성공 ({result.fetch_method})")

            soup = BeautifulSoup(html, "html.parser")

            # Step 2: Find direct file links on this page
            docs = self._extract_file_links(soup, final_url)
            result.documents.extend(docs)
            result.pages_scanned = 1
            self._update_progress(f"페이지 1 스캔: 파일 {len(docs)}개 발견")

            # Step 3: Find detail page links and recursively explore them
            if max_depth >= 1:
                detail_links = self._find_detail_links(soup, final_url)
                if detail_links:
                    self._update_progress(
                        f"상세 페이지 {len(detail_links)}개 발견, 재귀 탐색 중 (max_depth={max_depth})..."
                    )
                    for i, link in enumerate(detail_links):
                        if link["url"] in self._seen_urls:
                            continue
                        self._seen_urls.add(link["url"])

                        self._explore_page(link["url"], max_depth - 1, result, max_pages)
                        result.detail_pages_visited += 1

                        if (i + 1) % 5 == 0:
                            self._update_progress(
                                f"상세 페이지 {i+1}/{len(detail_links)} 탐색, "
                                f"총 파일 {len(result.documents)}개"
                            )

            # Step 4: Pagination - follow to more list pages
            pagination_urls = self._detect_pagination(soup, final_url)
            if pagination_urls:
                self._update_progress("페이지네이션 감지, 추가 페이지 탐색...")
                pages_visited = 1

                for page_url in pagination_urls:
                    if pages_visited >= max_pages:
                        break
                    if page_url in self._seen_urls:
                        continue
                    self._seen_urls.add(page_url)

                    page_html, page_final_url = self._fetch(page_url)
                    if not page_html:
                        break  # Stop if we can't fetch

                    page_soup = BeautifulSoup(page_html, "html.parser")
                    page_docs = self._extract_file_links(page_soup, page_final_url)

                    # Also check detail pages on this page (recursive)
                    if max_depth >= 1:
                        page_details = self._find_detail_links(
                            page_soup, page_final_url
                        )
                        for link in page_details:
                            if link["url"] in self._seen_urls:
                                continue
                            self._seen_urls.add(link["url"])
                            self._explore_page(link["url"], max_depth - 1, result, max_pages)
                            result.detail_pages_visited += 1

                    if (
                        not page_docs
                        and not self._find_detail_links(page_soup, page_final_url)
                    ):
                        self._update_progress(
                            f"페이지 {pages_visited+1}: 더 이상 항목 없음, 중단"
                        )
                        break

                    result.documents.extend(page_docs)
                    pages_visited += 1
                    result.pages_scanned = pages_visited
                    self._update_progress(
                        f"페이지 {pages_visited}/{max_pages} 스캔, "
                        f"총 파일 {len(result.documents)}개"
                    )

            # Step 5: AI analysis if we found nothing
            if not result.documents and use_ai:
                self._update_progress("파일을 찾지 못함, AI 분석 시도 중...")
                ai_result = self._ai_analyze(html, url)
                if ai_result and ai_result.get("has_documents"):
                    # Try AI-suggested selectors
                    for sel_key in ["document_links_css", "file_links_css"]:
                        sel = ai_result.get(sel_key)
                        if sel:
                            try:
                                elements = soup.select(sel)
                                for el in elements:
                                    if el.name == "a":
                                        href = el.get("href", "")
                                        abs_url = urljoin(final_url, href)
                                        if abs_url not in self._seen_urls:
                                            self._seen_urls.add(abs_url)
                                            is_file, file_type = self._is_file_url(
                                                href
                                            )
                                            if not file_type:
                                                file_type = (
                                                    self._detect_file_type_head(
                                                        abs_url
                                                    )
                                                )
                                            result.documents.append(FoundDocument(
                                                title=(
                                                    el.get_text(strip=True)[:200]
                                                    or "Untitled"
                                                ),
                                                file_url=abs_url,
                                                file_type=file_type or "unknown",
                                                source_page=final_url,
                                            ))
                            except Exception:
                                pass

                    if result.documents:
                        self._update_progress(
                            f"AI 분석으로 {len(result.documents)}개 파일 발견"
                        )

                # Also try AJAX API detection
                if not result.documents:
                    try:
                        from .network_capture import find_data_api
                        self._update_progress("AJAX API 탐지 시도 중...")
                        api_result = find_data_api(url)
                        if api_result:
                            self._update_progress(
                                f"API 발견: {api_result['url'][:60]}"
                            )
                            # Parse API response for file links
                            try:
                                data = json.loads(api_result["sample"])
                                self._extract_files_from_json(data, url, result)
                            except Exception:
                                pass
                    except Exception as e:
                        result.errors.append(f"API 탐지 실패: {str(e)[:100]}")

            # Deduplicate
            seen = set()
            unique_docs = []
            for doc in result.documents:
                if doc.file_url not in seen:
                    seen.add(doc.file_url)
                    unique_docs.append(doc)
            result.documents = unique_docs

            result.progress = (
                f"완료: {len(result.documents)}개 파일 발견, "
                f"{result.pages_scanned}페이지 스캔, "
                f"{result.detail_pages_visited}개 상세 페이지 방문"
            )
            self._update_progress(result.progress)

        except Exception as e:
            result.errors.append(str(e)[:200])
        finally:
            self._close_browser()

        return result

    def _extract_files_from_json(self, data, base_url, result):
        """Extract file URLs from JSON API response."""
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = None
            for key in [
                "items", "data", "list", "result", "results", "rows",
                "resultList", "bbsList", "nttList", "boardList", "dataList",
            ]:
                if key in data and isinstance(data[key], list):
                    items = data[key]
                    break
            if not items:
                return
        else:
            return

        for item in items:
            if not isinstance(item, dict):
                continue
            # Look for file URLs in item
            for key, val in item.items():
                if isinstance(val, str) and val:
                    is_file, file_type = self._is_file_url(val)
                    if is_file:
                        abs_url = urljoin(base_url, val)
                        if abs_url not in self._seen_urls:
                            self._seen_urls.add(abs_url)
                            title = ""
                            for tk in [
                                "title", "nttSj", "bbsSj", "sj", "subject",
                                "boardTitle",
                            ]:
                                if tk in item and item[tk]:
                                    title = str(item[tk])[:200]
                                    break
                            result.documents.append(FoundDocument(
                                title=title or "Untitled",
                                file_url=abs_url,
                                file_type=file_type,
                                source_page=base_url,
                            ))

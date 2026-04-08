# Crawler-POC Architecture Deep Dive

**Date**: 2026-03-30  
**Project**: crawler-poc (multi-site document crawler + curation system)  
**Scope**: HTTP request handling, fetch methods, site configuration, CLI orchestration

---

## Executive Summary

The crawler-poc is a **config-driven multi-method web scraping system** with:
- **3 fetch methods**: requests (default), cloudscraper (WAF bypass), Playwright browser (SPA)
- **3 crawl types**: `html` (list→detail), `single-page` (all links on one page), `api` (REST endpoints)
- **30+ site configs** as JSON files with selector-based extraction
- **3 custom Python crawlers** (NTRS API, MOHW, FSC)
- **CLI with 6 commands**: crawl, test-config, list-sites, stats, download, convert

---

## Architecture Overview

```
crawler/
├── base_crawler.py          # Abstract base with HTTP session management
├── generic_crawler.py       # Config-driven crawler (reads JSON)
├── main.py                  # CLI entry point
├── db.py                    # SQLite database (papers, sites tables)
├── sites/
│   ├── __init__.py          # Crawler registry (auto-discovery)
│   ├── ntrs.py              # NASA NTRS API crawler (custom)
│   ├── mohw.py              # Korea MOHW crawler (custom)
│   ├── fsc.py               # Fertility clinic crawler (custom)
│   ├── configs/             # 30+ JSON site configs
│   │   ├── arxiv-ai.json
│   │   ├── ed-annual.json
│   │   ├── humoruniv.json
│   │   ├── inven-diablo2.json
│   │   └── ... (30+ more)
│   └── custom/              # Custom crawlers (extensible)
└── converter.py             # HWP/PDF → Markdown converter
```

---

## 1. HTTP Request Handling (base_crawler.py)

### Session Setup

```python
class BaseCrawler:
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ..."
    
    def __init__(self, db_conn, delay=1.0):
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": self.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,...",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        })
```

**Key Features**:
- **Persistent session** (`requests.Session()`) — reuses TCP connections
- **Chrome-like user agent** — mimics Windows Chrome/Chromium
- **Multi-language Accept-Language** — supports Korean + English
- **Compression support** — gzip, deflate, brotli

### Request Method with Retries

```python
def _request(self, url, params=None, method="GET", retries=3, **kwargs):
    """Make HTTP request with rate-limiting, retries, and error handling."""
    for attempt in range(retries):
        time.sleep(self._delay)  # Rate limit before each request
        try:
            response = self._session.request(
                method, url, params=params, timeout=30, **kwargs
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            print(f"[{self.site_id}] Request error (attempt {attempt + 1}/{retries})")
            if attempt < retries - 1:
                wait = (attempt + 1) * 3  # Exponential backoff: 3s, 6s, 9s
                print(f"[{self.site_id}] Retrying in {wait}s...")
                time.sleep(wait)
            else:
                return None
```

**Retry Strategy**:
- **3 retries by default** (configurable)
- **Exponential backoff**: 3s, 6s, 9s between attempts
- **30-second timeout** (hardcoded)
- **Rate limiting**: `self._delay` (1.0-2.5s) before each request

---

## 2. Fetch Methods (generic_crawler.py)

The GenericCrawler supports **3 fetch methods** via config option `fetch_method`:

### A. Requests (Default)

```python
response = self._request(url, params=params)
if response is None:
    return None
return BeautifulSoup(response.text, "html.parser")
```

**Use Cases**: Standard HTML sites, API responses  
**Performance**: Fastest  
**Limitations**: Can't handle JavaScript, fails on WAF-protected sites

---

### B. Cloudscraper (WAF Bypass)

```python
if self._fetch_method == "cloudscraper":
    import cloudscraper
    self._cloudscraper = cloudscraper.create_scraper()

# When fetching:
if self._cloudscraper and not is_file:
    try:
        _time.sleep(self._delay)
        response = self._cloudscraper.get(url, params=params, timeout=30)
        encoding = self._config.get("options", {}).get("encoding")
        if encoding:
            response.encoding = encoding
        return BeautifulSoup(response.text, "html.parser")
    except Exception as e:
        print(f"[{self.site_id}] Cloudscraper failed: {str(e)[:60]}")
        return None
```

**Use Cases**: Cloudflare-protected sites, WAF-enabled servers  
**Performance**: Slower than requests (simulates browser)  
**Limitations**: File downloads use requests instead

---

### C. Playwright Browser (SPA Support)

```python
def _get_browser_page(self, url, wait_seconds=3):
    from playwright.sync_api import sync_playwright
    
    if self._playwright is None:
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
    
    page = self._browser.new_page(user_agent="Mozilla/5.0 ...")
    try:
        page.goto(url, timeout=30000, wait_until="networkidle")
        page.wait_for_timeout(int(wait_seconds * 1000))
        html = page.content()
    finally:
        page.close()
    
    encoding = self._config.get("options", {}).get("encoding")
    if encoding:
        html = html.encode(encoding, errors="ignore").decode(encoding, errors="ignore")
    return BeautifulSoup(html, "html.parser")
```

**Use Cases**: Single-Page Apps (React, Vue, Angular), dynamic content  
**Performance**: Slowest (launches full browser per page)  
**Features**:
- Waits for `networkidle` (all network requests done)
- Configurable wait after page load (default 3s)
- Supports custom encoding
- Resource cleanup on close

---

### Fetch Method Selection Logic

```python
def _fetch_page(self, url, params=None):
    # Skip browser for file downloads
    is_file = any(ext in url.lower() for ext in ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.hwp', '.hwpx'])
    
    # Try cloudscraper first (if enabled and not a file)
    if self._cloudscraper and not is_file:
        # Try cloudscraper...
    
    # Try browser (if enabled and not a file)
    if self._use_browser and not is_file:
        try:
            return self._get_browser_page(url)
        except Exception:
            # Fall back to requests
            response = self._request(url, params=params)
            if response is None:
                return None
            return self._parse_response(response)
    else:
        # Direct requests fallback
        response = self._request(url, params=params)
        if response is None:
            return None
        return self._parse_response(response)
```

**Fallback Chain**:
1. Cloudscraper (if enabled) → on error → requests
2. Browser (if enabled) → on error → requests
3. Requests (always available)

---

## 3. Site Configuration (JSON Structure)

### Config Overview

All 30+ sites are JSON files in `crawler/sites/configs/*.json`. Three main patterns:

### Example 1: arxiv-ai.json (HTML with Pagination)

```json
{
  "site_id": "arxiv-ai",
  "site_name": "arXiv AI",
  "base_url": "https://arxiv.org",
  "crawl_type": "html",
  "list_page": {
    "url": "https://arxiv.org/list/cs.AI/recent",
    "params": {},
    "pagination": {
      "type": "query_param",
      "param": "skip",
      "start": 0,
      "step": 50
    },
    "selectors": {
      "item_container": "#articles dt",
      "item_link": "#articles dt a",
      "item_link_attr": "href",
      "title": "#articles dd .list-title",
      "date": "#articles dd .meta div.dateline",
      "category": null
    }
  },
  "detail_page": {
    "selectors": {
      "title": "h1.title",
      "abstract": "meta[name='citation_abstract']",
      "authors": ".authors",
      "date": ".dateline",
      "pdf_link": "meta[name='citation_pdf_url']",
      "keywords": null,
      "department": null,
      "doi": null
    }
  },
  "options": {
    "delay": 1.5,
    "encoding": null,
    "verify_ssl": true,
    "id_regex": null,
    "fetch_method": null
  }
}
```

---

### Example 2: ed-annual.json (Single-Page)

```json
{
  "site_id": "ed-annual",
  "site_name": "ED Annual Plans & Reports",
  "base_url": "https://www.ed.gov",
  "crawl_type": "single-page",
  "list_page": {
    "url": "https://www.ed.gov/about/ed-overview/annual-performance-reports/annual-plans-and-reports",
    "selectors": {
      "link": "a[href$='.pdf']",
      "link_attr": "href",
      "category_heading": "h2"
    }
  },
  "options": {
    "delay": 1.5,
    "encoding": null,
    "verify_ssl": true,
    "id_regex": null
  }
}
```

---

### Example 3: humoruniv.json (Custom Encoding + Regex)

```json
{
  "site_id": "humoruniv",
  "site_name": "웃긴대학",
  "base_url": "https://web.humoruniv.com",
  "crawl_type": "html",
  "options": {
    "delay": 1.5,
    "encoding": "euc-kr",
    "id_regex": "number=(\\d+)",
    "verify_ssl": true
  }
}
```

---

## 4. CLI Commands (main.py)

### Command 1: crawl
```bash
python -m crawler.main crawl arxiv-ai --limit 50
```
Crawls a site and saves papers to database.

### Command 2: test-config
```bash
python -m crawler.main test-config ed-annual --limit 3
```
Tests a config with small limit before full crawl.

### Command 3: list-sites
```bash
python -m crawler.main list-sites
```
Lists all registered sites + paper counts.

### Command 4: stats
```bash
python -m crawler.main stats
```
Shows per-site statistics + download status.

### Command 5: download
```bash
python -m crawler.main download arxiv-ai --limit 10
python -m crawler.main download --retry
```
Downloads PDF/HWP files via curl. Supports retry.

### Command 6: convert
```bash
python -m crawler.main convert arxiv-ai --limit 50
```
Converts downloaded HWP/PDF to Markdown.

---

## 5. Database Schema

### Sites Table
```sql
CREATE TABLE sites (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    base_url    TEXT NOT NULL,
    last_crawled TIMESTAMP
);
```

### Papers Table
```sql
CREATE TABLE papers (
    id              TEXT PRIMARY KEY,
    site_id         TEXT NOT NULL REFERENCES sites(id),
    external_id     TEXT,
    title           TEXT,
    authors         TEXT,       -- JSON array
    abstract        TEXT,
    category        TEXT,
    keywords        TEXT,       -- JSON array
    published_date  TEXT,
    url             TEXT,
    pdf_url         TEXT,
    doi             TEXT,
    department      TEXT,
    metadata        TEXT,       -- JSON object
    crawled_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    summary         TEXT,
    download_status TEXT,       -- "downloaded", "failed", null
    download_path   TEXT,
    
    UNIQUE(site_id, external_id)
);
```

---

## 6. Timeout & Retry Configuration

| Parameter | Value | Notes |
|-----------|-------|-------|
| HTTP timeout | 30s | Hardcoded in `_request()` |
| Retries | 3 | Default, configurable |
| Backoff | 3s, 6s, 9s | Exponential |
| Delay | 1.0-2.5s | Per-site config |
| Browser wait | 3s | After networkidle |

---

## 7. Key Design Patterns

1. **Composition**: BaseCrawler → GenericCrawler, custom crawlers
2. **Factory Pattern**: JSON configs create GenericCrawler subclasses
3. **Session Pooling**: Reused TCP connections
4. **Graceful Degradation**: Browser → Cloudscraper → Requests fallback
5. **Auto-Discovery**: JSON configs + Python crawlers auto-registered

---

## 8. 30+ Site Configs

Covers US government agencies (ed.gov, doi.gov, dol.gov, treasury.gov, va.gov, dhs.gov, justice.gov), academic sources (arXiv, NTRS), and international sites (Korean: humoruniv, inven).

---

## Quick Reference: File Locations

| File | Lines | Purpose |
|------|-------|---------|
| `base_crawler.py` | 98 | HTTP session, retries |
| `generic_crawler.py` | 501 | Config-driven crawling |
| `main.py` | 232 | CLI dispatcher |
| `db.py` | 80+ | SQLite ORM |
| `sites/__init__.py` | 62 | Crawler registry |
| `sites/ntrs.py` | 122 | NASA API crawler |
| `sites/configs/*.json` | — | 30+ site configs |


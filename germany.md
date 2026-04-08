# German Government/Research Sites Crawl Results

- **Date**: 2026-03-30
- **Total URLs tested**: 55
- **Accessible**: 55/55 (100%) — no geo-blocking
- **Crawler configs created**: 51 (excluding 4 Fraunhofer)
- **Working crawlers (test passed)**: 32
- **Failed (SPA/selector issues)**: 19
- **Needs custom crawler**: 4 (publica.fraunhofer.de)
- **Branch**: pro (includes --browser flag for SPA sites)

## Summary

| Category | Count | Details |
|----------|-------|---------|
| OK (requests) | 24 | Standard HTML sites, no browser needed |
| OK (browser) | 8 | SPA sites working with Playwright |
| FAIL (SPA no PDF) | 14 | publikationen-bund pages with no PDF on that ministry |
| FAIL (selector) | 5 | Need further selector analysis |
| Custom needed | 4 | Fraunhofer Angular DSpace |

## Working Crawlers — 32 sites

### Standard HTML (24 sites)

| Site ID | Name | Items Found |
|---------|------|-------------|
| bundesfinanz-brochures | Finance Ministry - Brochures | 10+ |
| bundesfinanz-monthly | Finance Ministry - Monthly Report | 10+ |
| bmwe-publications | Economics Ministry - Publications | 8+ |
| bmwe-medienraum | Economics Ministry - Media Room | 10+ |
| auswaertiges-amt-brochures | Foreign Office - Brochures (browser) | 19 PDFs |
| auswaertiges-amt-news | Foreign Office - News (browser) | 5+ |
| bmi-bund-news | Interior Ministry - News | 5+ |
| daten-berlin | Berlin Open Data - PDF datasets | 10+ paginated |
| bmv-publications | Transport Ministry - Publications | 10+ |
| bmv-mobility | Transport Ministry - Mobility Stats | 73 PDFs |
| bmg-pflege | Health Ministry - Care | 52+ |
| bmg-drogen | Health Ministry - Drugs & Addiction | 10+ |
| bmg-gesundheit | Health Ministry - Health | 10+ |
| bmg-praevention | Health Ministry - Prevention | 10+ |
| bmg-ministerium | Health Ministry - Ministry | 10+ |
| bmg-forschung | Health Ministry - Research | 10+ |
| bmg-presse | Health Ministry - Press Releases | 10+ |
| bmz-publications | Development Cooperation - Publications | 5+ |
| datenportal-bmbf | Education Data Portal | PDFs |
| bmwsb-meldungen | Housing Ministry - News | 10+ |
| bmbfsfj-presse | Family Ministry - Press Releases | 10+ |
| bmftr-publikationen | Research Ministry - Publications | 10+ |
| statistischebibliothek | Statistical Library | 1472 items |
| bmleh-advisory | Agriculture Ministry - Advisory Publications | 228 links |

### SPA Browser Success (8 sites)

These are publikationen-bundesregierung.de pages that successfully found PDF links via Playwright:

| Site ID | Ministry |
|---------|----------|
| publikationen-bund-aa | Foreign Office |
| publikationen-bund-bmbfsfj | Education/Family |
| publikationen-bund-bmg-d | Drug Policy Commissioner |
| publikationen-bund-bmg | Health |
| publikationen-bund-bmukn | Environment |
| publikationen-bund-bmz | Development |
| publikationen-bund-search | Search (all publishers) |
| publikationen-bund-ubskm | Child Abuse Commissioner |

## Failed — 19 sites

### publikationen-bund SPA — No PDF on Page (14 sites)

These ministry pages on publikationen-bundesregierung.de have no PDF links rendered by React. The pages either show ordering info only or the publications require physical ordering.

| Site ID | Ministry |
|---------|----------|
| publikationen-bund-bbmb | Disabilities Commissioner |
| publikationen-bund-bk | Federal Chancellery |
| publikationen-bund-bkm | Culture Commissioner |
| publikationen-bund-bmas | Labour Ministry |
| publikationen-bund-bmf | Finance |
| publikationen-bund-bmftr | Research |
| publikationen-bund-bmi | Interior |
| publikationen-bund-bmjv | Justice |
| publikationen-bund-bmleh | Agriculture |
| publikationen-bund-bmvg | Defence |
| publikationen-bund-bmv | Transport |
| publikationen-bund-bmwe | Economic Affairs |
| publikationen-bund-bmwsb | Housing |
| publikationen-bund-bpa | Press Office |
| publikationen-bund-ib | Migration Commissioner |

**To improve**: Intercept React API calls (`/pp-en/*!searchJson`) and build a custom Python crawler.

### Other Failed (5 sites)

| Site ID | Issue |
|---------|-------|
| bmbfsfj-publikationen | Complex search URL with encoded state params |
| bmleh-goodpractices | German form-based search page |
| bmleh-presse | German press release search page |
| bmukn-presse | JS-rendered press releases |

## Needs Custom Crawler — 4 sites

### publica.fraunhofer.de (Angular DSpace)

| Site ID | Type Filter |
|---------|-------------|
| fraunhofer-journal | Open Access Journal Articles |
| fraunhofer-conference | Conference Papers |
| fraunhofer-thesis | Doctoral Theses |
| fraunhofer-report | Reports |

Angular SPA that doesn't render search results even with Playwright + 8s wait. Requires DSpace REST API direct calls.

## Pro Branch Features

### --browser flag (new)

```bash
# SPA site with browser rendering
python -m crawler.main auto-add "URL" --browser --verbose

# Batch with browser
python -m crawler.main batch-add urls.txt --browser
```

### Improved SPA detection

Agent now checks for `<div id="root">`, `<div id="app">`, `<app-root>`, `__NEXT_DATA__`, `ng-version` in HTML.

## How to Use

```bash
# Switch to pro branch
git checkout pro

# Activate venv
source .venv/bin/activate

# List all German sites
python -m crawler.main list-sites | grep -E "bmg|bmv|bmwe|bundesfinanz|daten-berlin|publikationen"

# Crawl a site
python -m crawler.main crawl bmg-pflege --limit 20

# Download PDFs
python -m crawler.main download bmg-pflege --limit 10

# Auto-add new SPA site
python -m crawler.main auto-add "URL" --browser --verbose

# Stats
python -m crawler.main stats
```

## Test Reports

- `reports/de_test_20260330_*.json` — Initial accessibility test (55/55 OK)

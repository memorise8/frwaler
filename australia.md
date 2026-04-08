# Australian Government Sites Crawling Test Results

## Overall Summary

- **Total URLs tested:** 65 Australian government/research sites
- **Success:** 32 (49%)
- **Failed:** 33 (51%) — geo-blocked, requires Australian VPN/proxy
- **Test date:** 2026-03-30
- **Test location:** Non-Australian IP
- **Timeout:** 60 seconds per method
- **Methods used:** requests → cloudscraper → Playwright browser

---

## Successfully Accessible Sites (32)

### Originally Successful (29 sites)
These work with standard requests from the initial 15s test:

| Name | URL | Size |
|------|-----|------|
| transparency.gov.au | https://www.transparency.gov.au/publications | 692 bytes |
| data.gov.au | https://data.gov.au/data/dataset/?res_format=PDF | 1.2 MB |
| dataverse.ada.edu.au | https://dataverse.ada.edu.au/dataverse/ada?q=&types=dataverses%3Adatasets%3Afiles&sort=dateSort&order=desc&page=1 | — |
| pmc.gov.au | https://www.pmc.gov.au/resources?f%5B0%5D=db_r_publication_category%3A334 | 962 bytes |
| treasury.gov.au | https://treasury.gov.au/publication | 49 KB |
| abs.gov.au | https://dataexplorer.abs.gov.au/?tm=%20&pg=0&fc=Economy&snb=1213&isAvailabilityDisabled=false | 74 KB |
| accc.gov.au | https://www.accc.gov.au/about-us/publications | 160 KB |
| apra.gov.au/stats | https://www.apra.gov.au/statistics | 129 KB |
| apra.gov.au/pubs | https://www.apra.gov.au/news-and-publications/39 | 149 KB |
| anu.edu.au | https://openresearch-repository.anu.edu.au/search?spc.page=1&spc.sf=dc.date.accessioned&spc.sd=DESC&view=list | 724 bytes |
| ga.gov.au/geonetwork | https://ecat.ga.gov.au/geonetwork/srv/eng/catalog.search#/search?isTemplate=n&sortBy=relevance&from=1&to=30&any=PDF | 9 KB |
| ga.gov.au/1940s | https://www.ga.gov.au/data-pubs/library/legacy-publications/records/1940s | 123 KB |
| ga.gov.au/1950s | https://www.ga.gov.au/data-pubs/library/legacy-publications/records/1950s | 283 KB |
| ga.gov.au/1960s | https://www.ga.gov.au/data-pubs/library/legacy-publications/records/1960s | 421 KB |
| ga.gov.au/1970s | https://www.ga.gov.au/data-pubs/library/legacy-publications/records/1970s | 323 KB |
| ga.gov.au/1980s | https://www.ga.gov.au/data-pubs/library/legacy-publications/records/1980s | 123 KB |
| ga.gov.au/1990s | https://www.ga.gov.au/data-pubs/library/legacy-publications/records/1990s | 201 KB |
| ga.gov.au/2000s | https://www.ga.gov.au/data-pubs/library/legacy-publications/records/2000s | 91 KB |
| ga.gov.au/2010s | https://www.ga.gov.au/data-pubs/library/legacy-publications/records/2010s | 120 KB |
| ga.gov.au/2020s | https://www.ga.gov.au/data-pubs/library/legacy-publications/records/digitised-records-2020s | 89 KB |
| alrc.gov.au | https://www.alrc.gov.au/publications/final-report/ | 101 KB |
| homeaffairs.gov.au/search | https://www.homeaffairs.gov.au/sitesearch?k=pdf | 1.2 MB, has PDF links |
| treasury.gov.au/search | https://treasury.gov.au/search?search_keyword=pdf | 41 KB, has PDF links |
| abs.gov.au/search | https://search.abs.gov.au/s/search.html?form=simple&collection=abs-search&query=pdf | 179 KB, has PDF links |
| accc.gov.au/search | https://www.accc.gov.au/search?query=pdf | 156 KB |
| apra.gov.au/search | https://www.apra.gov.au/search?query=pdf | 107 KB |
| ansto.gov.au/search | https://www.ansto.gov.au/search?query=pdf | 80 KB, has PDF links |
| ga.gov.au/search | https://www.ga.gov.au/search?from=0&query=pdf&index=geoscience_site_crawl | 16 KB, has PDF links |
| alrc.gov.au/search | https://www.alrc.gov.au/?s=pdf&type=all | 148 KB, has PDF links |

### Newly Confirmed Accessible (3 sites)
Confirmed accessible from 60s test with multiple methods:

| Name | URL | Method | Notes |
|------|-----|--------|-------|
| afp.gov.au | https://www.afp.gov.au/news-centre | requests | Initially showed captcha at 15s, succeeded at 60s |
| afp.gov.au/search | https://afp.gov.au/search?keys=pdf&content_type_id=All | requests | 60 KB |
| pmc.gov.au/search | https://www.pmc.gov.au/search?term=pdf | browser (Playwright) | Empty with requests, needs JS rendering |

---

## Failed Sites (33)

### Geo-blocked (timeout + ERR_HTTP2_PROTOCOL_ERROR)
All three methods fail (requests, cloudscraper, Playwright browser).

| Domain | Publication URLs | Search URLs |
|--------|-----------------|-------------|
| ag.gov.au | integrity, international-relations, families-and-marriage, rights-and-protections, legal-system, crime, national-security (7) | /search?query=pdf |
| agriculture.gov.au | /about/publications/list | /search?search_api_fulltext=pdf |
| defence.gov.au | annual-reports, defence-census, discipline-reports, export-permit-statistics (4) | /search?keywords=pdf |
| finance.gov.au | annual-report, reports (2) | /search?search=pdf |
| dfat.gov.au | /about-us/publications?page=1 | /search?keys=pdf |
| health.gov.au | /resources/publications | /node/44804?query=pdf |
| acma.gov.au | media-releases, publications (2) | /search?search_api_fulltext=pdf |
| nhmrc.gov.au | publications, resources (2) | /search?search=pdf |
| industry.gov.au | — | /search?search=pdf |
| dss.gov.au | — | /search?search=pdf |
| dva.gov.au | — | /search/node?keys=pdf |
| aims.gov.au | — | /search?keys=pdf |

### WAF-Blocked (403 Forbidden)

| Domain | URLs | Notes |
|--------|------|-------|
| aihw.gov.au | downloadable-resources, latest-reports, corporate-publications/reports, media-releases, search (5) | 403 on requests/cloudscraper, browser timeout |

### Empty Response

| Domain | URL | Notes |
|--------|-----|-------|
| ansto.gov.au/repo | https://apo.ansto.gov.au/search?... | 200 OK but 287 bytes empty — likely API-gated DSpace repo |

---

## How to Access Geo-blocked Sites

### Option 1: VPN (Simplest)
Connect to an Australian VPN server (NordVPN, ExpressVPN, etc.) then run the crawler normally.

### Option 2: Proxy (Crawler-only)
Set proxy in site config JSON:

```json
"options": {
    "proxy": "socks5://au-proxy-host:port",
    "timeout": 60
}
```

Only crawler traffic goes through Australia. Proxy support was added to the codebase on 2026-03-30.

### Option 3: Re-test with Proxy
```bash
PYTHONUNBUFFERED=1 .venv/bin/python scripts/test_au_sites.py 60
```

---

## Technical Details

- **Error pattern for geo-blocked:** requests/cloudscraper timeout at 60s, Playwright ERR_HTTP2_PROTOCOL_ERROR
- **cloudscraper limitations:** Does NOT bypass Akamai Bot Manager (used by many .gov.au sites)
- **Full JSON report:** `reports/au_test_20260330_205653.json`


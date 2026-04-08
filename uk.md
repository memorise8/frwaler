# UK Government/Research Sites Crawl Results

- **Date**: 2026-03-31
- **Total URLs tested**: 59
- **Accessible**: 58/59 (98%) — data.london.gov.uk only failure
- **Crawler configs created**: 57
- **Working crawlers (test passed)**: 55
- **Failed**: 2 (ArcGIS Hub SPA)
- **Not accessible**: 1 (data.london.gov.uk)
- **Branch**: pro

## Summary

| Category | Count | Details |
|----------|-------|---------|
| OK (requests) | 50 | Standard HTML sites |
| OK (cloudscraper) | 5 | parliament.uk WAF bypass |
| FAIL (ArcGIS SPA) | 2 | geoportal-stats, naturalengland |
| FAIL (inaccessible) | 1 | data.london.gov.uk |
| Not tested | 1 | adruk/datacatalogue (SPA) |

## Working Crawlers — 55 sites

### GOV.UK (4)
| Site ID | Description |
|----------|-------------|
| gov-uk-research | Research and statistics (research) |
| gov-uk-statistics | Research and statistics (published stats) |
| data-gov-uk | Data.gov.uk PDF datasets |
| gov-uk-official-docs | Official documents |

### Office for National Statistics (2)
| Site ID | Description |
|----------|-------------|
| ons-publications | ONS publications search |
| ons-data | ONS datasets and time series |

### Scotland (4)
| Site ID | Description |
|----------|-------------|
| gov-scot-stats | Scottish Government statistics |
| gov-scot-pubs | Scottish Government publications |
| publichealthscotland | Public Health Scotland publications |
| nrscotland | National Records of Scotland |

### Wales (2)
| Site ID | Description |
|----------|-------------|
| gov-wales-stats | Welsh Government statistics |
| gov-wales-pubs | Welsh Government publications |

### Northern Ireland (12)
| Site ID | Description |
|----------|-------------|
| nisra | NI Statistics and Research Agency |
| education-ni-news | Dept of Education NI - News |
| education-ni-pubs | Dept of Education NI - Publications |
| economy-ni-pubs | Dept for the Economy NI - Publications |
| economy-ni-news | Dept for the Economy NI - News |
| finance-ni-news | Dept of Finance NI - News |
| finance-ni-pubs | Dept of Finance NI - Publications |
| infrastructure-ni-news | Dept for Infrastructure NI - News |
| infrastructure-ni-pubs | Dept for Infrastructure NI - Publications |
| health-ni-news | Dept of Health NI - News |
| health-ni-pubs | Dept of Health NI - Publications |
| justice-ni-news | Dept of Justice NI - News |
| justice-ni-pubs | Dept of Justice NI - Publications |

Note: All NI sites share the same Drupal CMS pattern.

### Parliament (5) — cloudscraper required
| Site ID | Description |
|----------|-------------|
| parliament-commons | House of Commons Journal |
| parliament-oscepa | OSCE PA Publications |
| committees-parliament | Committee Publications |
| post-parliament | POST Research |
| lordslibrary | Lords Library Research |

### Research Institutes (16)
| Site ID | Description |
|----------|-------------|
| babraham-report | Babraham Institute - Annual Report |
| babraham-news | Babraham Institute - News |
| jic-pubs | John Innes Centre - Publications |
| jic-news | John Innes Centre - News |
| jic-annual | John Innes Centre - Annual Reports |
| earlham-pubs | Earlham Institute - Publications |
| earlham-news | Earlham Institute - News |
| lboro-repository | Loughborough University Repository |
| mrc-lmb-pubs | MRC Lab of Molecular Biology - Publications |
| noc-brochures | National Oceanography Centre - Brochures |
| noc-pubs | National Oceanography Centre - Publications |
| noc-news | National Oceanography Centre - News |
| eprints-soton | University of Southampton ePrints |
| nora-nerc | NERC Open Research Archive |
| rfi-reports | Rosalind Franklin Institute - Reports |
| rfi-news | Rosalind Franklin Institute - News |

### Other Data/Research (10)
| Site ID | Description |
|----------|-------------|
| ukdataservice | UK Data Service - Latest Collections |
| adruk-pubs | ADR UK - Publications |
| adruk-annual | ADR UK - Annual Reports |
| adruk-impact | ADR UK - Impact Case Studies |
| osr-pubs | Office for Statistics Regulation - Publications |
| osr-news | Office for Statistics Regulation - News |
| opendatacommunities | Open Data Communities |
| skillsdev-scot-pubs | Skills Development Scotland - Publications |
| skillsdev-scot-news | Skills Development Scotland - News |

## Failed — 3 sites

| Site ID | URL | Issue |
|----------|-----|-------|
| geoportal-stats | geoportal.statistics.gov.uk | ArcGIS Hub SPA — needs API |
| naturalengland | naturalengland-defra.opendata.arcgis.com | ArcGIS Hub SPA — needs API |
| data-london | data.london.gov.uk | Connection failed |

## Not Tested
| URL | Reason |
|-----|--------|
| datacatalogue.adruk.org | SPA application portal |

## How to Use

```bash
# List UK sites
python -m crawler.main list-sites | grep -E "gov-uk|ons-|gov-scot|gov-wales|nisra|-ni-|parliament|noc-|jic-|adruk"

# Crawl a site
python -m crawler.main crawl gov-uk-research --limit 20

# Parliament sites need cloudscraper
python -m crawler.main crawl parliament-commons --limit 10

# Download PDFs
python -m crawler.main download gov-uk-research --limit 10

# Stats
python -m crawler.main stats
```

## Test Report
- `reports/uk_test_20260331_*.json` — Accessibility test (58/59 OK)

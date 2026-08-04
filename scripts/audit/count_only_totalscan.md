# Count-Only Capacity Summary

Generated from `count_only_totalscan.csv` — 93 previously-unmeasured sites (all `sites` rows minus those with a numeric source_total already in coverage_report.csv/custom_crawler_totals(.SNAPSHOT).csv, minus HAL-global-scope hal_solr sites >1.5M).

## Results

- Targets: **93**
- Completed (exact count, crawl finished before the external cap): **35**
- Capped (external timeout hit — count is a LOWER BOUND): **58**
- Errors: **0**
- No crawler registered: **0**

### By method

| method | sites |
|---|---:|
| count_crawl | 93 |

## Capacity

- This pass's counted total (sum of `counted`, exact + lower-bound rows): **21,183**
  - NOTE: rows with `completed=False` (capped, 58 sites) are LOWER BOUNDS, so this sum understates true capacity for those sites.
- Already-measured baseline (union of coverage_report.csv + custom_crawler_totals(.SNAPSHOT).csv, 203 sites): **19,633,502**
- **Combined capacity estimate: 19,654,685**

## Top 30 newly-counted sites

| site_id | collected | counted | completed | method |
|---|---:|---:|---|---|
| federalregister-gov-documents | 5,957 | 5,548 | False | count_crawl |
| data-busan-go-kr-bdip | 2,022 | 1,856 | False | count_crawl |
| enscp-hal-science-search | 3,517 | 1,507 | False | count_crawl |
| ens-hal-science-search | 18,340 | 1,473 | False | count_crawl |
| doaj-org-search | 1,417 | 923 | True | count_crawl |
| inserm-hal-science-search | 883 | 904 | True | count_crawl |
| ineris-hal-science-search | 2,234 | 656 | False | count_crawl |
| isae-ensma-hal-science-search | 14 | 591 | False | count_crawl |
| polytechnique-hal-science-search | 3,103 | 529 | False | count_crawl |
| cigionline-org-publications | 500 | 527 | False | count_crawl |
| amu-hal-science-search | 3,048 | 510 | False | count_crawl |
| cea-hal-science-cnrgh | 759 | 472 | False | count_crawl |
| anr-hal-science-search | 363 | 454 | False | count_crawl |
| cnam-hal-science-search | 975 | 403 | False | count_crawl |
| repository-cern-search | 500 | 401 | False | count_crawl |
| hal-science-ign-ensg | 3,288 | 386 | False | count_crawl |
| enc-hal-science-search | 509 | 383 | False | count_crawl |
| ens-lyon-hal-science-search | 1,343 | 362 | True | count_crawl |
| ird-hal-science-search | 1,144 | 292 | False | count_crawl |
| ehess-hal-science-search | 429 | 270 | True | count_crawl |
| cnam-hal-science-ceet | 279 | 268 | True | count_crawl |
| ec-lyon-hal-science-search | 222 | 224 | True | count_crawl |
| avoindata-suomi-fi-data | 154 | 153 | True | count_crawl |
| homeaffairs-gov-au-sitesearch | 1,308 | 139 | False | count_crawl |
| icp-hal-science-search | 220 | 132 | True | count_crawl |
| icn-hal-science-search | 133 | 97 | True | count_crawl |
| book-ioj-go-kr-library | 500 | 96 | False | count_crawl |
| data-taltech-ee-search | 92 | 92 | True | count_crawl |
| issibern-ch-results | 495 | 86 | True | count_crawl |
| nfra-gov-cn-cn | 726 | 71 | True | count_crawl |

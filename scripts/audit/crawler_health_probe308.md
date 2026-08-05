# Count-Only Capacity Summary

Generated from `crawler_health_probe308.csv` — 308 previously-unmeasured sites (all `sites` rows minus those with a numeric source_total already in coverage_report.csv/custom_crawler_totals(.SNAPSHOT).csv, minus HAL-global-scope hal_solr sites >1.5M).

## Results

- Targets: **308**
- Completed (exact count, crawl finished before the external cap): **291**
- Capped (external timeout hit — count is a LOWER BOUND): **17**
- Errors: **0**
- No crawler registered: **0**

### By method

| method | sites |
|---|---:|
| count_crawl | 308 |

## Capacity

- This pass's counted total (sum of `counted`, exact + lower-bound rows): **651**
  - NOTE: rows with `completed=False` (capped, 17 sites) are LOWER BOUNDS, so this sum understates true capacity for those sites.
- Already-measured baseline (union of coverage_report.csv + custom_crawler_totals(.SNAPSHOT).csv, 203 sites): **19,633,502**
- **Combined capacity estimate: 19,634,153**

## Top 30 newly-counted sites

| site_id | collected | counted | completed | method |
|---|---:|---:|---|---|
| anr-hal-science-search | 363 | 3 | True | count_crawl |
| accc-gov-au-about-us | 502 | 3 | True | count_crawl |
| amu-hal-science-search | 3,048 | 3 | True | count_crawl |
| artsetmetiers-hal-science-search | 86 | 3 | True | count_crawl |
| asiapacific-ca-media | 50 | 3 | True | count_crawl |
| avoindata-suomi-fi-data | 154 | 3 | True | count_crawl |
| adaptcentre-ie-research | 206 | 3 | True | count_crawl |
| arrow-tudublin-ie-creaart | 229 | 3 | True | count_crawl |
| archives-nationales-culture-gouv-fr-presse | 15 | 3 | True | count_crawl |
| arcom-fr-presse | 290 | 3 | True | count_crawl |
| book-ioj-go-kr-library | 500 | 3 | True | count_crawl |
| bjerknes-uib-no-en | 214 | 3 | True | count_crawl |
| bm-dk-soeg | 88 | 3 | True | count_crawl |
| bmbfsfj-bund-de-bmbfsfj | 502 | 3 | True | count_crawl |
| cac-gov-cn-hdfw | 89 | 3 | True | count_crawl |
| broadbentinstitute-ca-research | 81 | 3 | True | count_crawl |
| canada-ca-en | 948 | 3 | True | count_crawl |
| bra-se-english | 306 | 3 | True | count_crawl |
| canurb-org-publications | 92 | 3 | True | count_crawl |
| cancer-fr-catalogue-des-public | 12 | 3 | True | count_crawl |
| cea-hal-science-cnrgh | 759 | 3 | True | count_crawl |
| cancer-fr-presse | 110 | 3 | True | count_crawl |
| cds-cern-ch-collection | 505 | 3 | True | count_crawl |
| cedelft-eu-reports | 500 | 3 | True | count_crawl |
| chinatax-gov-cn-chinatax | 6 | 3 | True | count_crawl |
| cigionline-org-publications | 500 | 3 | True | count_crawl |
| cerema-fr-fr | 231 | 3 | True | count_crawl |
| climateinstitute-ca-newsroom | 110 | 3 | True | count_crawl |
| cnam-hal-science-ceet | 279 | 3 | True | count_crawl |
| cnam-hal-science-search | 975 | 3 | True | count_crawl |

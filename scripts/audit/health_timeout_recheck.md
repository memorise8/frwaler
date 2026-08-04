# Count-Only Capacity Summary

Generated from `health_timeout_recheck.csv` — 45 previously-unmeasured sites (all `sites` rows minus those with a numeric source_total already in coverage_report.csv/custom_crawler_totals(.SNAPSHOT).csv, minus HAL-global-scope hal_solr sites >1.5M).

## Results

- Targets: **45**
- Completed (exact count, crawl finished before the external cap): **17**
- Capped (external timeout hit — count is a LOWER BOUND): **28**
- Errors: **0**
- No crawler registered: **0**

### By method

| method | sites |
|---|---:|
| count_crawl | 45 |

## Capacity

- This pass's counted total (sum of `counted`, exact + lower-bound rows): **40**
  - NOTE: rows with `completed=False` (capped, 28 sites) are LOWER BOUNDS, so this sum understates true capacity for those sites.
- Already-measured baseline (union of coverage_report.csv + custom_crawler_totals(.SNAPSHOT).csv, 203 sites): **19,633,502**
- **Combined capacity estimate: 19,633,542**

## Top 30 newly-counted sites

| site_id | collected | counted | completed | method |
|---|---:|---:|---|---|
| gov-il-collectors | 708 | 10 | False | count_crawl |
| data-mfe-govt-nz-documents | 21 | 3 | True | count_crawl |
| data-mfe-govt-nz-tables | 435 | 3 | True | count_crawl |
| data-nasa-gov-dataset | 4,474 | 3 | True | count_crawl |
| bmf-gv-at-ministerium | 36 | 3 | True | count_crawl |
| dhs-gov-congressional-approp | 638 | 3 | True | count_crawl |
| dhs-gov-publication | 29 | 3 | True | count_crawl |
| nhc-gov-cn-wjw | 71 | 3 | True | count_crawl |
| openscience-si-naprednoiskanjeaspx | 30 | 3 | True | count_crawl |
| dhs-gov-news-releases | 79 | 2 | False | count_crawl |
| dspace-ut-ee-search | 125 | 2 | False | count_crawl |
| bfr-bund-de-en | 23 | 1 | True | count_crawl |
| genome-gov-about-nhgri | 42 | 1 | False | count_crawl |
| astron-nl-about | 57 | 0 | True | count_crawl |
| academie-sciences-fr-espace-presse | 68 | 0 | False | count_crawl |
| agri-ee-ministeerium-uudised | 122 | 0 | False | count_crawl |
| anses-fr-fr | 6,091 | 0 | False | count_crawl |
| astro-oma-be-en | 22 | 0 | False | count_crawl |
| bmv-de-en | 11 | 0 | True | count_crawl |
| dapa-go-kr-dapa | 970 | 0 | True | count_crawl |
| cells-es-en | 11 | 0 | True | count_crawl |
| english-mee-gov-cn-resources | 20 | 0 | True | count_crawl |
| ehesp-fr-espace-presse | 127 | 0 | False | count_crawl |
| en-ndrc-gov-cn-policies | 52 | 0 | False | count_crawl |
| forskningsradet-no-om-forskningsradet | 356 | 0 | False | count_crawl |
| garda-ie-en | 94 | 0 | False | count_crawl |
| hea-ie-resources | 221 | 0 | True | count_crawl |
| genomecanada-ca-about | 50 | 0 | False | count_crawl |
| ineris-fr-fr | 141 | 0 | False | count_crawl |
| mnd-go-kr-user | 304 | 0 | True | count_crawl |

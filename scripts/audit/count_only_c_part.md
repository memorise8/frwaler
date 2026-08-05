# Count-Only Capacity Summary

Generated from `count_only_c_part.csv` — 15 previously-unmeasured sites (all `sites` rows minus those with a numeric source_total already in coverage_report.csv/custom_crawler_totals(.SNAPSHOT).csv, minus HAL-global-scope hal_solr sites >1.5M).

## Results

- Targets: **15**
- Completed (exact count, crawl finished before the external cap): **15**
- Capped (external timeout hit — count is a LOWER BOUND): **0**
- Errors: **0**
- No crawler registered: **0**

### By method

| method | sites |
|---|---:|
| count_crawl | 15 |

## Capacity

- This pass's counted total (sum of `counted`, exact + lower-bound rows): **80,960**
  - NOTE: rows with `completed=False` (capped, 0 sites) are LOWER BOUNDS, so this sum understates true capacity for those sites.
- Already-measured baseline (union of coverage_report.csv + custom_crawler_totals(.SNAPSHOT).csv, 203 sites): **19,633,502**
- **Combined capacity estimate: 19,714,462**

## Top 30 newly-counted sites

| site_id | collected | counted | completed | method |
|---|---:|---:|---|---|
| flore-unifi-it | 14,038 | 43,600 | True | count_crawl |
| iris-unitn-it | 10,521 | 20,626 | True | count_crawl |
| earth-prints-org | 796 | 3,864 | True | count_crawl |
| health-gov-au-publications | 749 | 3,574 | True | count_crawl |
| acma-gov-au-publications | 316 | 2,275 | True | count_crawl |
| openaccess-inaf-it | 513 | 1,888 | True | count_crawl |
| data-e-gov-go-jp-data | 1,392 | 1,380 | True | count_crawl |
| ag-gov-au-publications | 607 | 960 | True | count_crawl |
| health-govt-nz-publications | 306 | 818 | True | count_crawl |
| treasury-govt-nz-publications | 245 | 817 | True | count_crawl |
| government-se-publications | 465 | 521 | True | count_crawl |
| dfat-gov-au-publications | 324 | 324 | True | count_crawl |
| nhmrc-gov-au-publications | 232 | 233 | True | count_crawl |
| defence-gov-au-publications | 80 | 80 | True | count_crawl |
| justice-govt-nz-publications | 0 | 0 | True | count_crawl |

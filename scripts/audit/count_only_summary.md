# Count-Only Capacity Summary

Generated from `repaired_remeasure.csv` — 13 previously-unmeasured sites (all `sites` rows minus those with a numeric source_total already in coverage_report.csv/custom_crawler_totals(.SNAPSHOT).csv, minus HAL-global-scope hal_solr sites >1.5M).

## Results

- Targets: **13**
- Completed (exact count, crawl finished before the external cap): **5**
- Capped (external timeout hit — count is a LOWER BOUND): **8**
- Errors: **0**
- No crawler registered: **0**

### By method

| method | sites |
|---|---:|
| count_crawl | 13 |

## Capacity

- This pass's counted total (sum of `counted`, exact + lower-bound rows): **2,108**
  - NOTE: rows with `completed=False` (capped, 8 sites) are LOWER BOUNDS, so this sum understates true capacity for those sites.
- Already-measured baseline (union of coverage_report.csv + custom_crawler_totals(.SNAPSHOT).csv, 203 sites): **19,633,502**
- **Combined capacity estimate: 19,635,610**

## Top 30 newly-counted sites

| site_id | collected | counted | completed | method |
|---|---:|---:|---|---|
| kihasa-re-kr-publish | 673 | 403 | False | count_crawl |
| agriculture-gouv-fr-recherche-developpem | 472 | 400 | True | count_crawl |
| transportation-gov-newsroom | 205 | 247 | False | count_crawl |
| cso-ie-en | 388 | 234 | False | count_crawl |
| ons-gov-uk-search | 516 | 232 | False | count_crawl |
| dhs-gov-news-releases | 79 | 228 | False | count_crawl |
| agri-ee-ministeerium-uudised | 122 | 122 | True | count_crawl |
| nia-nih-gov-news | 95 | 115 | False | count_crawl |
| bam-de-navigation | 308 | 36 | False | count_crawl |
| nupi-no-en | 28 | 29 | True | count_crawl |
| safefood-net-news | 31 | 29 | True | count_crawl |
| emsl-pnnl-gov-science | 6 | 28 | True | count_crawl |
| bmz-de-de | 6 | 5 | False | count_crawl |

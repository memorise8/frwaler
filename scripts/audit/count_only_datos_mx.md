# Count-Only Capacity Summary

Generated from `count_only_datos_mx.csv` — 8 previously-unmeasured sites (all `sites` rows minus those with a numeric source_total already in coverage_report.csv/custom_crawler_totals(.SNAPSHOT).csv, minus HAL-global-scope hal_solr sites >1.5M).

## Results

- Targets: **8**
- Completed (exact count, crawl finished before the external cap): **8**
- Capped (external timeout hit — count is a LOWER BOUND): **0**
- Errors: **0**
- No crawler registered: **0**

### By method

| method | sites |
|---|---:|
| count_crawl | 8 |

## Capacity

- This pass's counted total (sum of `counted`, exact + lower-bound rows): **379**
  - NOTE: rows with `completed=False` (capped, 0 sites) are LOWER BOUNDS, so this sum understates true capacity for those sites.
- Already-measured baseline (union of coverage_report.csv + custom_crawler_totals(.SNAPSHOT).csv, 203 sites): **19,633,502**
- **Combined capacity estimate: 19,633,881**

## Top 30 newly-counted sites

| site_id | collected | counted | completed | method |
|---|---:|---:|---|---|
| datos-gob-mx-cultura | 114 | 114 | True | count_crawl |
| datos-gob-mx-presupuesto | 108 | 108 | True | count_crawl |
| datos-gob-mx-agricultura | 85 | 85 | True | count_crawl |
| datos-gob-mx-seguridad | 27 | 27 | True | count_crawl |
| datos-gob-mx-secretaria-trabajo | 16 | 16 | True | count_crawl |
| datos-gob-mx-territorio | 16 | 16 | True | count_crawl |
| datos-gob-mx-secretaria-salud | 9 | 9 | True | count_crawl |
| datos-gob-mx-turismo | 4 | 4 | True | count_crawl |

# Count-Only Capacity Summary

Generated from `count_only_capped_1h.csv` — 0 previously-unmeasured sites (all `sites` rows minus those with a numeric source_total already in coverage_report.csv/custom_crawler_totals(.SNAPSHOT).csv, minus HAL-global-scope hal_solr sites >1.5M).

## Results

- Targets: **0**
- Completed (exact count, crawl finished before the external cap): **0**
- Capped (external timeout hit — count is a LOWER BOUND): **0**
- Errors: **0**
- No crawler registered: **0**

### By method

| method | sites |
|---|---:|

## Capacity

- This pass's counted total (sum of `counted`, exact + lower-bound rows): **0**
  - NOTE: rows with `completed=False` (capped, 0 sites) are LOWER BOUNDS, so this sum understates true capacity for those sites.
- Already-measured baseline (union of coverage_report.csv + custom_crawler_totals(.SNAPSHOT).csv, 203 sites): **19,633,502**
- **Combined capacity estimate: 19,633,502**

## Top 30 newly-counted sites

| site_id | collected | counted | completed | method |
|---|---:|---:|---|---|

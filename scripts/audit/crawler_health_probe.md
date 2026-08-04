# Count-Only Capacity Summary

Generated from `crawler_health_probe.csv` — 804 previously-unmeasured sites (all `sites` rows minus those with a numeric source_total already in coverage_report.csv/custom_crawler_totals(.SNAPSHOT).csv, minus HAL-global-scope hal_solr sites >1.5M).

## Results

- Targets: **804**
- Completed (exact count, crawl finished before the external cap): **733**
- Capped (external timeout hit — count is a LOWER BOUND): **45**
- Errors: **0**
- No crawler registered: **26**

### By method

| method | sites |
|---|---:|
| count_crawl | 778 |
| no_crawler | 26 |

## Capacity

- This pass's counted total (sum of `counted`, exact + lower-bound rows): **2,001**
  - NOTE: rows with `completed=False` (capped, 45 sites) are LOWER BOUNDS, so this sum understates true capacity for those sites.
- Already-measured baseline (union of coverage_report.csv + custom_crawler_totals(.SNAPSHOT).csv, 203 sites): **19,633,502**
- **Combined capacity estimate: 19,635,503**

## Top 30 newly-counted sites

| site_id | collected | counted | completed | method |
|---|---:|---:|---|---|
| afp-gov-au-news-centre | 432 | 3 | True | count_crawl |
| acma-gov-au-publications | 316 | 3 | True | count_crawl |
| afd-fr-fr | 797 | 3 | True | count_crawl |
| adaptcentre-ie-research | 206 | 3 | True | count_crawl |
| adruk-org-news-publications | 210 | 3 | True | count_crawl |
| ag-gov-au-publications | 607 | 3 | True | count_crawl |
| acpr-banque-france-fr-fr | 225 | 3 | True | count_crawl |
| ameslab-gov-news | 394 | 3 | True | count_crawl |
| alrc-gov-au-publications | 116 | 3 | True | count_crawl |
| agriculture-gouv-fr-recherche-developpem | 472 | 3 | True | count_crawl |
| accc-gov-au-about-us | 502 | 3 | True | count_crawl |
| africamuseum-be-en | 52 | 3 | True | count_crawl |
| amolf-nl-publications | 512 | 3 | True | count_crawl |
| amu-hal-science-search | 3,048 | 3 | True | count_crawl |
| amf-france-org-fr | 157 | 3 | True | count_crawl |
| accc-gov-au-search | 454 | 3 | True | count_crawl |
| app-mps-gov-cn-gdnps | 501 | 3 | True | count_crawl |
| ami-swiss-en | 558 | 3 | True | count_crawl |
| arn-se-om-arn | 80 | 3 | True | count_crawl |
| ansto-gov-au-search | 30 | 3 | True | count_crawl |
| artsetmetiers-fr-fr | 222 | 3 | True | count_crawl |
| artsetmetiers-hal-science-search | 86 | 3 | True | count_crawl |
| arcnl-nl-publications | 11 | 3 | True | count_crawl |
| asiapacific-ca-media | 50 | 3 | True | count_crawl |
| archives-nationales-culture-gouv-fr-presse | 15 | 3 | True | count_crawl |
| audencia-com-lecole | 246 | 3 | True | count_crawl |
| arcom-fr-presse | 290 | 3 | True | count_crawl |
| arrow-tudublin-ie-creaart | 229 | 3 | True | count_crawl |
| auri-re-kr-boardes | 260 | 3 | True | count_crawl |
| auswaertiges-amt-de-en | 526 | 3 | True | count_crawl |

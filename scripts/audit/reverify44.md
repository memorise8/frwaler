# Count-Only Capacity Summary

Generated from `reverify44.csv` — 44 previously-unmeasured sites (all `sites` rows minus those with a numeric source_total already in coverage_report.csv/custom_crawler_totals(.SNAPSHOT).csv, minus HAL-global-scope hal_solr sites >1.5M).

## Results

- Targets: **44**
- Completed (exact count, crawl finished before the external cap): **44**
- Capped (external timeout hit — count is a LOWER BOUND): **0**
- Errors: **0**
- No crawler registered: **0**

### By method

| method | sites |
|---|---:|
| count_crawl | 44 |

## Capacity

- This pass's counted total (sum of `counted`, exact + lower-bound rows): **81**
  - NOTE: rows with `completed=False` (capped, 0 sites) are LOWER BOUNDS, so this sum understates true capacity for those sites.
- Already-measured baseline (union of coverage_report.csv + custom_crawler_totals(.SNAPSHOT).csv, 203 sites): **19,633,502**
- **Combined capacity estimate: 19,633,583**

## Top 30 newly-counted sites

| site_id | collected | counted | completed | method |
|---|---:|---:|---|---|
| bj-admin-ch-bj | 41 | 3 | True | count_crawl |
| apra-gov-au-statistics | 75 | 3 | True | count_crawl |
| bundesfinanzministerium-de-web | 112 | 3 | True | count_crawl |
| data-biodiversity-be-dataset | 524 | 3 | True | count_crawl |
| education-govt-nz-our-work | 34 | 3 | True | count_crawl |
| en-moj-gov-cn-lawsandregulationsht | 15 | 3 | True | count_crawl |
| eprints-imtlucca-it-cgi | 1,461 | 3 | True | count_crawl |
| gov-gr-search | 489 | 3 | True | count_crawl |
| gov-ie-en | 780 | 3 | True | count_crawl |
| arcep-fr-actualites | 34 | 3 | True | count_crawl |
| inrae-fr-actualites | 830 | 3 | True | count_crawl |
| ifr-pan-edu-pl-dzialalnosc-naukowa | 232 | 3 | True | count_crawl |
| eprints-soton-ac-uk-view | 259 | 3 | True | count_crawl |
| issnationallab-org-about | 82 | 3 | True | count_crawl |
| kostat-go-kr-boardes | 100 | 3 | True | count_crawl |
| minedu-gov-gr-grafeio-typoy-kai-di | 404 | 3 | True | count_crawl |
| inria-fr-fr | 114 | 3 | True | count_crawl |
| nina-no-english | 36 | 3 | True | count_crawl |
| nofima-com-publication | 200 | 3 | True | count_crawl |
| nzpri-aut-ac-nz-document-library | 7 | 3 | True | count_crawl |
| phfscience-nz-news-publications | 32 | 3 | True | count_crawl |
| publicsafety-gc-ca-cnt | 424 | 3 | True | count_crawl |
| rijksoverheid-nl-documenten | 274 | 3 | True | count_crawl |
| science-astron-nl-science-astron | 117 | 3 | True | count_crawl |
| unidata-gv-at-pages | 404 | 3 | True | count_crawl |
| mpi-govt-nz-about-mpi | 14 | 3 | True | count_crawl |
| mbie-govt-nz-building-and-energy | 80 | 3 | True | count_crawl |
| doc-cerema-fr-default | 697 | 0 | True | count_crawl |
| en-iwhr-cn-iwhr-english-new | 458 | 0 | True | count_crawl |
| data-gov-be-nl | 132 | 0 | True | count_crawl |

# Count-Only Capacity Summary

Generated from `lowerbound_deep.csv` — 313 previously-unmeasured sites (all `sites` rows minus those with a numeric source_total already in coverage_report.csv/custom_crawler_totals(.SNAPSHOT).csv, minus HAL-global-scope hal_solr sites >1.5M).

## Results

- Targets: **313**
- Completed (exact count, crawl finished before the external cap): **119**
- Capped (external timeout hit — count is a LOWER BOUND): **176**
- Errors: **0**
- No crawler registered: **18**

### By method

| method | sites |
|---|---:|
| count_crawl | 295 |
| no_crawler | 18 |

## Capacity

- This pass's counted total (sum of `counted`, exact + lower-bound rows): **116,574**
  - NOTE: rows with `completed=False` (capped, 176 sites) are LOWER BOUNDS, so this sum understates true capacity for those sites.
- Already-measured baseline (union of coverage_report.csv + custom_crawler_totals(.SNAPSHOT).csv, 203 sites): **19,633,502**
- **Combined capacity estimate: 19,750,076**

## Top 30 newly-counted sites

| site_id | collected | counted | completed | method |
|---|---:|---:|---|---|
| prism-go-kr-homepage | 2,900 | 20,621 | False | count_crawl |
| sonar-ch-global | 5,462 | 5,364 | True | count_crawl |
| llnl-gov-news | 3,537 | 3,958 | False | count_crawl |
| jsearch-mwr-gov-cn-irs-c-web | 2,499 | 2,575 | False | count_crawl |
| par-nsf-gov-search | 2,481 | 2,360 | False | count_crawl |
| homeaffairs-gov-au-sitesearch | 1,308 | 1,912 | False | count_crawl |
| presse-economie-gouv-fr | 1,206 | 1,881 | False | count_crawl |
| ypes-gr-category | 987 | 1,785 | False | count_crawl |
| oig-treasury-gov-other-reports-testim | 1,353 | 1,354 | True | count_crawl |
| sejong-go-kr-bbs | 500 | 1,241 | False | count_crawl |
| gnews-gg-go-kr-briefing | 500 | 1,216 | False | count_crawl |
| scourt-go-kr-portal | 982 | 1,177 | False | count_crawl |
| minrel-gob-cl-minrel | 4,547 | 1,154 | False | count_crawl |
| justice-gov-news | 741 | 1,035 | False | count_crawl |
| policyalternatives-ca-news-research | 1,543 | 1,026 | False | count_crawl |
| book-ioj-go-kr-library | 500 | 998 | True | count_crawl |
| bok-or-kr-portal | 430 | 995 | False | count_crawl |
| caissedesdepots-fr-communiques-de-press | 1,201 | 951 | False | count_crawl |
| stepi-re-kr-site | 570 | 941 | False | count_crawl |
| moef-go-kr-nw | 683 | 935 | False | count_crawl |
| mpva-go-kr-mpva | 590 | 902 | False | count_crawl |
| kmcc-go-kr-userdo | 666 | 881 | False | count_crawl |
| gri-re-kr-web | 516 | 845 | False | count_crawl |
| mss-go-kr-site | 500 | 832 | False | count_crawl |
| civilprotection-gov-gr-deltia-tupou | 500 | 825 | False | count_crawl |
| rda-go-kr-board | 1,020 | 792 | False | count_crawl |
| ir-cwi-nl | 592 | 786 | False | count_crawl |
| ftc-go-kr-www | 694 | 781 | False | count_crawl |
| repository-naturalis-nl | 9,681 | 777 | False | count_crawl |
| ir-arcnl-nl | 452 | 770 | False | count_crawl |

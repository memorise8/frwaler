# 크롤러 케파 최종 집계 (capacity_final)

- 총 사이트: **804**
- 총 수집완료(collected): **525,570**
- exact 사이트 max_to_collect 합계: **6,018,334** (428개)
- lower_bound 사이트 max_to_collect 합계: **90,922** (231개)
- unmeasured 사이트: **145개**

## 헤드라인 케파

- **CONFIRMED capacity (exact only): 6,018,334**
- **MINIMUM capacity (exact + lower_bound): 6,109,256**

## 30,000,000 추정치 대비

- CONFIRMED 6,018,334 = 30M의 **20.1%**
- MINIMUM 6,109,256 = 30M의 **20.4%**
- (전체 서베이가 아직 진행 중이므로 수치는 부분값이며, unmeasured 사이트에 숨은 케파가 남아 있음)

## 스토리지 추정

- 사용된 GLOBAL fallback pdf_rate: **0.5770** (수집 문서 중 PDF 보유 비율)
- 사용된 GLOBAL fallback 평균 PDF 크기: **5.63 MB**
- 시나리오별 예상 PDF 개수 / 예상 스토리지:
  - **CONFIRMED (exact)**: 3,516,508 PDF, **17.58 TB** (18,002 GB)
  - **MINIMUM (exact+lower_bound)**: 3,561,796 PDF, **17.70 TB** (18,127 GB)
- 참고: 텍스트 전용(비-PDF) 문서는 스토리지 기여가 ~0 이므로, 스토리지는 전체 문서 수가 아니라 PDF 개수에 비례함.

### 예상 스토리지 상위 15개 사이트

| site_id | max_to_collect | proj_pdf | proj_GB |
|---|--:|--:|--:|
| ntrs-nasa-gov-search | 646,398 | 634,546 | 4,895.3 |
| ostrnrcan-dostrncan-canada-ca-search | 198,333 | 193,825 | 2,241.7 |
| etera-ee-browse | 210,234 | 177,536 | 1,903.8 |
| research-collection-ethz-ch-search | 303,530 | 302,869 | 1,402.7 |
| sonar-ch-global | 309,727 | 308,139 | 1,253.1 |
| inserm-hal-science-search | 234,422 | 162,476 | 611.2 |
| pergamos-lib-uoa-gr-search | 250,523 | 179,876 | 550.7 |
| anr-hal-science-search | 140,008 | 140,008 | 534.5 |
| olympias-lib-uoi-gr-jspui | 50,014 | 49,264 | 484.0 |
| openresearch-repository-anu-edu-au-search | 393,484 | 58,796 | 420.8 |
| nora-nerc-ac-uk-view | 57,936 | 52,104 | 389.2 |
| ga-gov-au-data-pubs | 31,979 | 31,751 | 326.6 |
| inria-hal-science-search | 65,674 | 64,461 | 294.4 |
| repositorio-uchile-cl-discover | 102,175 | 101,562 | 270.4 |
| data-gov-au-data | 140,429 | 124,420 | 254.1 |

## max_to_collect 상위 25개 사이트

| site_id | max_to_collect | collected | source | 구분 |
|---|--:|--:|---|---|
| e-stat-go-jp-stat-search | 1,709,118 | 1,018 | big_api | exact |
| ntrs-nasa-gov-search | 646,398 | 6,490 | big_api | exact |
| openresearch-repository-anu-edu-au-search | 393,484 | 261 | api_dspace_rest7 | exact |
| sonar-ch-global | 309,727 | 5,462 | big_api | exact |
| research-collection-ethz-ch-search | 303,530 | 459 | api_dspace_rest7 | exact |
| pergamos-lib-uoa-gr-search | 250,523 | 500 | api_oai_resumption | exact |
| inserm-hal-science-search | 234,422 | 883 | hal_api | exact |
| etera-ee-browse | 210,234 | 100,403 | big_api | exact |
| ostrnrcan-dostrncan-canada-ca-search | 198,333 | 220 | custom_api | exact |
| data-gov-au-data | 140,429 | 500 | api_ckan | exact |
| anr-hal-science-search | 140,008 | 363 | hal_api | exact |
| data-gv-at-datasets | 116,818 | 400 | custom_api | exact |
| dspace-ut-ee-search | 114,527 | 125 | api_dspace_rest7 | exact |
| repositorio-uchile-cl-discover | 102,175 | 500 | api_oai_resumption | exact |
| inria-hal-science-search | 65,674 | 1,083 | hal_api | exact |
| data-gov-uk-search | 58,487 | 488 | api_ckan | exact |
| nora-nerc-ac-uk-view | 57,936 | 596 | api_oai_resumption | exact |
| ir-lib-uth-gr-xmlui | 55,971 | 843 | api_oai_resumption | exact |
| gov-scot-publications | 50,542 | 1,024 | big_api | exact |
| olympias-lib-uoi-gr-jspui | 50,014 | 400 | api_oai_resumption | exact |
| energy-gov-search | 49,332 | 191 | custom_api | exact |
| lenus-ie-browse | 47,138 | 168 | api_dspace_rest7 | exact |
| gov-uk-search | 42,435 | 862 | big_api | exact |
| data-nasa-gov-dataset | 35,964 | 4,474 | api_ckan | exact |
| doaj-org-search | 35,582 | 1,417 | api_oai_resumption | exact |

## unmeasured 사이트 (145개) — 숨은 케파 후보

수집완료(collected) 내림차순 상위 20개:

| site_id | collected | health |
|---|--:|---|
| flore-unifi-it | 14,038 |  |
| iris-unitn-it | 10,521 |  |
| pubs-drdc-rddc-gc-ca-basis | 4,040 | fetch_fail |
| llnl-gov-news | 3,537 | zero_parsed |
| minfin-gov-gr-grafeio-typou | 1,797 | fetch_fail |
| eprints-imtlucca-it-cgi | 1,461 | fetch_fail |
| homeaffairs-gov-au-sitesearch | 1,308 | fetch_fail |
| earth-prints-org | 796 |  |
| moj-go-kr-moj | 793 | zero_parsed |
| gob-mx-semar | 783 | no_crawler |
| gob-mx-semarnat | 774 | no_crawler |
| gob-mx-sep | 774 | no_crawler |
| gob-mx-sre | 774 | no_crawler |
| gob-mx-sspc | 773 | no_crawler |
| gob-mx-buengobierno | 771 | no_crawler |
| gob-mx-salud | 765 | no_crawler |
| gob-mx-sectur | 765 | no_crawler |
| gob-mx-se | 762 | no_crawler |
| gob-mx-shcp | 762 | no_crawler |
| gob-mx-defensa | 756 | no_crawler |

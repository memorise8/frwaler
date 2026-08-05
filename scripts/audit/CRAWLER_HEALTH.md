# 크롤러 전체 건강검진 (2026-08-05 18:38)

- 대상: 사이트 804개 (크롤러 보유 778, 미보유 26)
- 방식: sweep 실증(470) + 실전 probe(308, limit3/300s) — count-only 무저장

| health | 수 | 의미 |
|---|---:|---|
| ok | 691 | 정상 작동 |
| broken | 44 | 고장 의심(셀렉터/파싱) |
| no_crawler | 26 | 크롤러 미제작 |
| pdf_artifact | 16 | PDF의존 — 측정한계로 판정보류 |
| slow_unknown | 15 | 5분내 무저장(느림/고장) |
| blocked | 8 | 사이트가 차단 중 |
| net_fail | 4 | 접속 실패 |

## 수리 우선순위 (전량 다운로드 전)

### broken (44)
- apra-gov-au-statistics
- arcep-fr-actualites
- bj-admin-ch-bj
- bundesfinanzministerium-de-web
- dapa-go-kr-dapa
- data-biodiversity-be-dataset
- data-gov-be-nl
- doc-cerema-fr-default
- education-govt-nz-our-work
- en-iwhr-cn-iwhr-english-new
- en-moj-gov-cn-lawsandregulationsht
- eprints-imtlucca-it-cgi
- eprints-soton-ac-uk-view
- gov-gr-search
- gov-ie-en
- gsi-ie-en-ie
- ifr-pan-edu-pl-dzialalnosc-naukowa
- info-daegu-go-kr-newshome
- inrae-fr-actualites
- inria-fr-fr
- issnationallab-org-about
- iwhr-com-zgskywwnew
- jeonnam-go-kr-m7116
- kostat-go-kr-boardes
- mbie-govt-nz-building-and-energy
- minedu-gov-gr-grafeio-typoy-kai-di
- minfin-gov-gr-grafeio-typou
- minscfa-gov-gr-grafeio-typou
- mpi-govt-nz-about-mpi
- nidcd-nih-gov-news
- nih-gov-news-events
- nih-gov-press-room
- nina-no-english
- nofima-com-publication
- nzpri-aut-ac-nz-document-library
- om-mp-be-nl
- opendatacommunities-org-data
- phfscience-nz-news-publications
- publicsafety-gc-ca-cnt
- regjeringen-no-en
- rijksoverheid-nl-documenten
- science-astron-nl-science-astron
- unidata-gv-at-pages
- ypergasias-gov-gr-category

### blocked (8)
- education-govt-nz-search
- justice-govt-nz-publications
- mpi-govt-nz-resources-and-forms
- nkvts-no-english
- phfscience-nz-digital-library
- pmc-gov-au-resources
- stats-govt-nz-publications
- transparency-gov-au-publications

### net_fail (4)
- chineseafs-org-ckynewsmgr
- mnd-go-kr-user
- olympias-lib-uoi-gr-jspui
- repositorio-uchile-cl-discover

### slow_unknown (15)
- academie-sciences-fr-espace-presse
- anses-fr-fr
- ehesp-fr-espace-presse
- en-ndrc-gov-cn-policies
- forskningsradet-no-om-forskningsradet
- garda-ie-en
- ineris-fr-fr
- insee-fr-fr
- met-no-publikasjoner
- nec-go-kr-site
- provenienzforschung-gv-at-empfehlungen-des-bei
- socialstyrelsen-se-en
- stat-inst-se-om-sis
- tigta-gov-reports
- transformation-gouv-fr-espace-presse

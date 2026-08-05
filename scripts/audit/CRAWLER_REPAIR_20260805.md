# broken 크롤러 44개 수리 결과 (2026-08-05)

건강검진 broken 44개를 6개 병렬 에이전트로 수리 → count-only probe(무저장, `.env` 소싱)로 독립 재검증.

## 결과: **수리 확정 27 / 미해결 17**

### ✅ 수리 확정 27개 (재검증 probe counted≥1)
플랫폼 이전(SPA/JSON API/OAI 전환), WAF 우회(curl_cffi TLS위장·Playwright 상주), `.env` 오탐 포함.

- apra-gov-au-statistics
- arcep-fr-actualites
- bj-admin-ch-bj
- bundesfinanzministerium-de-web
- data-biodiversity-be-dataset
- education-govt-nz-our-work
- en-moj-gov-cn-lawsandregulationsht
- eprints-imtlucca-it-cgi
- eprints-soton-ac-uk-view
- gov-gr-search
- gov-ie-en
- ifr-pan-edu-pl-dzialalnosc-naukowa
- inrae-fr-actualites
- inria-fr-fr
- issnationallab-org-about
- kostat-go-kr-boardes
- mbie-govt-nz-building-and-energy
- minedu-gov-gr-grafeio-typoy-kai-di
- mpi-govt-nz-about-mpi
- nina-no-english
- nofima-com-publication
- nzpri-aut-ac-nz-document-library
- phfscience-nz-news-publications
- publicsafety-gc-ca-cnt
- rijksoverheid-nl-documenten
- science-astron-nl-science-astron
- unidata-gv-at-pages

### ✗ 미해결 17개 — 전부 인프라 차단/폐쇄 (크롤러 품질 아님)

| site_id | 사유 |
|---|---|
| dapa-go-kr-dapa | IP/ASN 차단(TCP drop) |
| data-gov-be-nl | CAPTCHA 격상(느린페이스면 가능성) |
| doc-cerema-fr-default | IP/ASN 차단(서브도메인 전체 403) |
| en-iwhr-cn-iwhr-english-new | WAF JS챌린지(Tengine 412) |
| gsi-ie-en-ie | 사이트 자체 임시폐쇄(~8월말) |
| info-daegu-go-kr-newshome | IP/ASN 차단(TCP drop) |
| iwhr-com-zgskywwnew | WAF JS챌린지(Tengine 412) |
| jeonnam-go-kr-m7116 | IP/ASN 차단(TCP drop) |
| minfin-gov-gr-grafeio-typou | WAF+IP평판 403(SiteGround) |
| minscfa-gov-gr-grafeio-typou | WAF+IP평판 403(SiteGround) |
| nidcd-nih-gov-news | IP 밴(명시적, ~9/1) |
| nih-gov-news-events | IP 밴(CF, nih.gov) |
| nih-gov-press-room | IP 밴(CF, nih.gov) |
| om-mp-be-nl | CAPTCHA 격상(코드수정됨,검증대기) |
| opendatacommunities-org-data | IP/지역 차단(Azure WAF) |
| regjeringen-no-en | IP/ASN 차단(CF, 브라우저도 불가) |
| ypergasias-gov-gr-category | WAF+IP평판 403(SiteGround) |

### 미해결 후속 트랙
- **IP/ASN 차단·IP밴(9)**: 다른 egress IP/프록시 필요 (dapa·jeonnam·daegu·regjeringen·opendatacommunities·cerema·NIH 3)
- **WAF JS챌린지(5)**: 헤드리스 브라우저로도 불가 (iwhr 2, 그리스 SiteGround 3)
- **CAPTCHA 격상(2)**: 우리 반복테스트로 격상됨 — 실제 수집 느린페이스면 통과 가능성, 단독 재검증 대상 (data-gov-be·om-mp-be)
- **임시폐쇄(1)**: gsi-ie — 8월말 복귀 후 재시도

# HTML 크롤러 "고장 146개" 수리 — 최종 요약 (2026-08-04)

## 배경
HTML 529개(`html_list.csv`) 중 "완료로 찍혔지만 진짜 다 수집됐나?" 검증에서 셀렉터 고장 의심 146개(`html_repair_FINAL.csv`)를 수리 대상으로 잡음. 실제 수리에 착수하자 **"counted=0=고장" 전제가 대부분 오탐**임이 드러남.

## 핵심 발견: count-only 측정도구의 3대 결함 (crawler는 멀쩡한데 0으로 오보)
1. **PDF-abstract 의존 크롤러** — abstract를 pdftotext로 만드는데, 하네스 PDF-guard(무저장 위해 .pdf curl을 가짜 빈응답)가 abstract 추출을 막아 counted=0. (bfr-bund·bmf-gv-at·english-mee·genome-gov·insee-fr·baw-de 등 8개). 판별: `grep pdftotext|_extract_pdf|pdf_text`.
2. **cap-무력화 정규식 오매치** — `neutralize_caps`가 `DEADLINE_MARGIN_SECONDS`의 "MARGIN"류를 잡아 값을 크게 → 예산 음수 → 0.0초 즉시종료. (environment-govt-nz).
3. **느린 사이트 + 짧은 타임아웃** — 120s컷에 안 끝나 가짜 0. 300~600s 재측정 필요. (health-ni→100, babraham→57 등).
   + 부수: 에이전트 자기 curl이 유발한 429 자책골(economy-ni), SUSPECT 휴리스틱(counted==collected 등) 대량 오탐.

## 146개 최종 트리아지
| 분류 | 개수 | 처리 |
|---|---:|---|
| 멀쩡(오탐) — count>5 또는 검사통과 | ~107 | 조치불필요 |
| PDF-artifact (측정불가·멀쩡) | 8 | 하네스 한계, crawler 정상 |
| **진짜 고장 → 수리완료** | **12** | 아래 표 |
| 미해결(abstract셀렉터, 별건) | 1 | bmz-de-de |
| SKIP — IP-WAF(프록시필요) | 4 | baw-de·data-biodiversity-be·opendatacommunities·minscfa-gov-gr |
| SKIP — SPA/JS쉘 | 8 | 정적크롤 불가 |
| SKIP — 죽음/백엔드폐기 | ~6 | dapa·khs·nps·unidata 등 |

## 수리 완료 12개 (before → after, 300s 재측정 `repaired_remeasure.csv`)
| 크롤러 | before | after | 원인/수정 |
|---|---:|---:|---|
| kihasa-re-kr-publish | 0 | 403+ | 셀렉터 |
| agriculture-gouv-fr | 0 | 400 | HTTP429를 "0건"으로 오처리 → status체크+백오프 |
| transportation-gov-newsroom | 0 | 247+ | Akamai차단 → **curl_cffi(chrome124) 임퍼스네이션** |
| cso-ie-en | 0 | 234+ | 속성 `name`→`id` 한 글자 |
| ons-gov-uk-search | 235 | 232 | JSON API, 실은 정상(오탐) — 쿼리 정리만 |
| dhs-gov-news-releases | 6 | 228+ | Akamai → curl_cffi 빠른경로 추가 |
| agri-ee-ministeerium-uudised | 0 | 122 | Cloudflare → curl_cffi |
| nia-nih-gov-news | 51 | 115+ | WAF HTTP-200 소프트차단 감지 + 백오프 |
| bam-de-navigation | 0 | 36+ | TYPO3 전면개편(리스트+상세+cHash 페이저) 전면 재작성 |
| safefood-net-news | 0 | 29 | Cloudflare → curl_cffi |
| nupi-no-en | 0 | 29 | 셀렉터 |
| emsl-pnnl-gov-science | 6 | 28 | 레거시 항목 abstract `<p>` 없음 → 메타데이터 폴백 abstract |

## 재사용 자산
- **curl_cffi Chrome TLS 임퍼스네이션(chrome124)** 이 Cloudflare/Akamai "Just a moment"/"Access Denied" 우회에 효과적(4/4 성공). chrome120은 flaky. → 다른 WAF 크롤러에 적용 가능.
- 파일럿 8대 교훈: scratchpad `repair_playbook.md`.

## 남은 과제
1. **bmz-de-de** abstract 셀렉터 수리(WAF 아님, out-of-scope였음).
2. **IP-WAF 4개** — 프록시/다른 egress IP 확보 시 curl_cffi로 재시도.
3. PDF-artifact 8개는 **count-only로는 영원히 0** — 실측하려면 하네스에 PDF-guard 우회한 "링크만 카운트" 모드 필요(또는 실크롤).
4. 케파 영향: 이 HTML 수리분은 총 ~2천건 수준(대부분 하한, 실제 더 큼)이라 케파 헤드라인 1,980만엔 거의 영향 없음. **의미는 "크롤러가 실제 수집시 제대로 동작"하게 된 것.**

## 산출물
`html_repair_FINAL.csv`(146분류) · `truth_sweep_146.csv`(90s 실측) · `repaired_remeasure.csv`(수리분 300s 재측정) · `probe_suspects.csv` · `html_only_TRUTH.csv`(529 정직판정) · `repair_batches/`

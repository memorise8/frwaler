# 크롤러 함대 품질 검수 — 납품 평가 (2026-08-03)

코드리뷰(실제 소스 ~50개 정독 + 자동스캔 교차검증) 결과. 대상 799개 커스텀 크롤러.

## 헤드라인 판정: REQUEST CHANGES (조건부 납품)
- 기본 동작은 하지만 깨끗하지 않음. 헬스 통과 84%, **"정확·완전한 콘텐츠 수집"은 ~70~75%**.
- ⚠️ **자동스캔 flag는 노이즈** — 클라이언트에 그대로 주면 안 됨: "PDF접미사 217개"는 ~90% 오탐, 정작 가장 치명적 데이터손실은 스캔이 대부분 놓침.

## 헬스 (n=799)
| 상태 | 수 | % |
|---|---:|---:|
| ok | 673 | 84.2 |
| 고장(0수집) | 73 | 9.1 |
| timeout | 28 | 3.5 |
| 미확인 | 24 | 3.0 |
- 구조적 사실: **769/799가 정적 fetch, JS렌더 가능 30개뿐** → SPA 전환 사이트는 조용히 0수집.

## 검증된 결함 (실제 코드 확인)

### 1. 데이터손실 — PDF만 받고 CSV/XLSX 본체 버림 (HIGH, 확신 高)
확정 5개: `e-stat-go-jp-stat-search`(:280-299), `apra-gov-au-statistics`(:230-247, 최악-통계포털인데 엑셀 버림), `data-gov-au-data`(:74 fq=res_format:PDF), `datos-gob-cl-dataset`(:251-291), `avoindata-suomi-fi-data`(:121-156).
- 완화형 5개(메타에 전 URL 보존→재수집 가능): catalogue-data-govt-nz, data-gouv-fr, bdap-opendata-it(csv_download_url까지), data-gv-at, opendata-dk.
- **근거 스키마 한계**: pdf_url 슬롯 1개뿐(base_crawler.py:117,124) → 다중배포 데이터셋이 파일 1개만 운반.
- **외삽: ~40개 데이터포털 중 10~15% 데이터손실 예상** — CKAN/통계 크롤러 표적 스윕 필요(정규식이 다 놓침).

### 2. 초록<N자 skip이 유효 레코드 버림 (MEDIUM~HIGH, 확신 中)
- 파일중심 사이트(통계/보도/PDF리포지토리)에서 유해: `andra-fr`(:438, 스캔PDF는 초록<50→통째버림), `apra-gov-au-statistics`(:225), `artsetmetiers-fr`(:261, N=100).
- 권고: 파일중심 크롤러는 초록길이 대신 **title AND pdf_url**로 게이트. 392개 flag 중 파일중심 35~45%가 위험.

### 3. 뉴스/보도 스코프 오염 (MEDIUM, 확신 中)
- 203개 flag 중 **~60~70% 진짜 스코프이탈**(보도자료 blurb), ~15~20% 정당, ~15~20% 혼합.
- 이탈 예: benthamscience, bmvg-de(국방부 보도), busan-go-kr(시 보도자료).

### 4. MAX_PAGES=200 캡 (382개) — 대형 리포지토리 수집 상한 (LOW~MED)
- 부정확이 아니라 **과소수집**. 사용자 지적대로 전량수집엔 상향 필요. env화(LIBERTREE_MAX_PAGES) 롤아웃 준비됨.

## 73개 고장 원인 (20개 표본)
| 버킷 | 비율 | 수정성 |
|---|---|---|
| 셀렉터 노후/사이트 개편 | ~35% | **수정가능** (셀렉터/URL 갱신) |
| 죽음/403/WAF봇차단 | ~35% | 혼합 (헤더수정~구조적) |
| SPA/JS전환 | ~10% | 구조적 (정적크롤 불가) |
| API키 회전/만료 | ~15% | 수정가능 (키 재추출) |
| 페이지네이션 파라미터 변경 | ~5% | 수정가능 |
- 수정가능 예: cso-ie-en, data-gov-be-nl, bam-de-navigation, kostat-go-kr(→mods.go.kr 도메인이전), arcep-fr(cHash).
- 구조적: repositorio-uchile(DSpace7 Angular전환), opendatacommunities(SvelteKit 내부스크랩).
- ⚠️ crawler/.env는 키 채워져 있음(1334B) — 문제는 사이트측 공개키 회전. ./.env(루트)가 0B라 오해유발.

## 우선 수정목록 (임팩트순)
1. **데이터손실 5개 수정** + CKAN/통계 ~35개 스윕 (소스쿼리 PDF필터 제거, 전 배포URL 보존)
2. **저비용 고장 복구** (73개 중 절반: 셀렉터/URL/도메인/키 갱신)
3. **403/WAF 7개 라이브 curl 프로브**로 헤더수정 vs 하드WAF 분류 (읽기전용이라 미실행)
4. SPA 2개 JS렌더 전환 or 구조적 손실 인정
5. 파일중심 크롤러 초록게이트→title+pdf_url
6. 뉴스 203개 스코프 분류 (60~70% 제거가능)
7. **crawler_audit.csv flag 그대로 납품 금지** — 검증된 수치로 재발행

## 긍정
- 기반 프레임워크 견고: retry/backoff, 파서폴백체인, 벽시계예산, URL dedup 루프가드 일관 적용.
- 다수 크롤러는 이미 올바름(메타에 전 리소스URL 보존, 랜딩페이지 정상 추적).
- OAI-PMH/JSON API 코호트가 가장 견고 — 취약 HTML스크랩에 확대 권장.

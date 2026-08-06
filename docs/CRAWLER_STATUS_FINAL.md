# 크롤러 최종 건강 상태 + 납품 준비 (2026-08-06)

> 전 과정 **무저장(임시 DB·blob)·운영 DB 무손상**. 이 문서는 compact 후 재개 진입점.
> 원본 데이터: `scripts/audit/crawler_status_final.csv` (804곳 전체, 되는곳/안되는곳+이유).

---

## 0. 한눈에

| | 수 | 비고 |
|---|--:|---|
| **된다 (작동)** | **772 / 804 (96%)** | 오늘 실제 미니크롤로 문서 저장 성공 = 실측 검증 |
| **안된다** | **32** | 코드문제는 4곳뿐, 나머지는 인프라/차단 |

**된다 772 = 정밀검증 32 + 신규제작 26 + ok재검증 714** (718 재검증 중 4곳만 탈락).

---

## 1. 검증 방법 (신뢰 근거)

- **실제 미니크롤**(`scripts/audit/delivery_health.py`): 각 사이트를 임시 DB·임시 blob에 3건 수집 시도 → 1건이라도 저장되면 **된다**. count-only 아님, 클라이언트와 동일 경로. `crawler/.env` 로드(키 필요 사이트 정상화).
- **718 ok 전수 재검증**: `run_verify_ok.py` (16 병렬 청크) → 709 즉시 HEALTHY + 재확인 5곳 회복 = 714.
- **60 not-ok 정밀 패스**: 32 HEALTHY(오탐이었음) / 4 수리 / 18 IP차단 / 6 폐쇄.
- 판정 근거는 각 CSV의 `out_tail`(크롤러 실제 출력) + 도달성 확인(HTTP status).

---

## 2. 안된다 32곳 — 분류 + 이유

### 🔧 진짜 코드 수리 = 4곳 (사이트는 정상 200 응답인데 파싱 0)
| 사이트 | 증상 | 수리 |
|---|---|---|
| `eprr-lanl-gov` | 실제 33KB 페이지, "no items" | 셀렉터 |
| `nidcd-nih-gov-news` | 실제 40KB, 0건 | 셀렉터 |
| `stats-govt-nz-publications` | CommonCrawl 방식 낡음(0 records) | 라이브 파싱 재작성 |
| `andra-fr-publications` | 200이나 263B JS셸 | Playwright 재작성 |

### 🌐 우리 IP 차단 / WAF = 18곳 (코드문제 아님 — **클라이언트 egress에선 될 가능성**)
403/202-WAF/봇차단. 클라이언트 서버(다른 IP)에서 첫 수집 때 재확인 필요, 안 되면 프록시:
NIH 3(news-events·press-room·nidcd-order), 그리스gov 3(minfin·minscfa·ypergasias), 중국iwhr 2(en-iwhr-cn·iwhr-com), 한국gov 2(dapa·info-daegu), justice-govt-nz, bordbia-ie, ethniccommunities-govt-nz, opendatacommunities-org, regjeringen-no, om-mp-be(CAPTCHA), pmc-gov-au, gsi-ie(8월말 복귀 예정).

### 🔌 폐쇄 / 네트워크 불통 = 6곳
chineseafs-org, doc-cerema-fr, jeonnam-go-kr, mnd-go-kr, mpi-govt-nz, olympias-lib-uoi-gr.

### 🔍 점검 필요 = 4곳 (과거 데이터 있음, 일시/셀렉터 의심)
cso-ie-en(연도쿼리 0), fibl-org-en(0 records), sm-ee-otsing(에스토니아 검색API 빈응답), nec-go-kr-site(느림/필터).

---

## 3. 남은 작업 (compact 후 재개)

1. **진짜 수리 4곳** — 셀렉터 2 + 재작성 2 (executor로, 8/5 수리 패턴 재사용). 유일한 코드 작업.
2. **점검 4곳** — cso-ie 연도로직 등 짧은 확인.
3. **IP차단 18곳** — 코드 아님. 클라이언트 첫 수집 시 확인 / 안 되면 프록시. **납품문서에 "client-side 확인 필요" 목록으로 명시.**
4. **폐쇄 6곳** — 제외 or 재확인(gsi-ie는 일시).
5. **FE 시스템 빌드** (별도 트랙, 결정 완료 — `docs/` FE 설계 참조): Postgres + BE(FastAPI) + FE(libertree-app 확장) Docker, 빈 시작, PDF 풀뎁스, 최신화(능동확인+증분), 역할분리.

---

## 4. 핵심 파일 맵

| 파일 | 내용 |
|---|---|
| `scripts/audit/crawler_status_final.csv` | **804곳 되는곳/안되는곳+이유** (site_id·name·status·category·reason·collected) |
| `scripts/audit/working_sites.csv` | 되는 776곳 + 근거 + 수집수 |
| `scripts/audit/final_verdict.csv` | 804곳 된다/안됨:카테고리 |
| `scripts/audit/delivery_health.py` | 실제 미니크롤 검증기(임시DB/blob, .env로드) |
| `scripts/audit/run_verify_ok.py` | 718 병렬 재검증 런처 |
| `scripts/audit/ok_verify.csv` / `delivery_health.csv` / `recheck9.csv` | 재검증 원자료(out_tail 근거 포함) |

---

## 5. 최신화(freshness) — 확인된 사실 (FE 설계용)

- **PDF 다운로드 작동**: 실측 5/6(1건은 원본 404). `pdf_downloader.download_pdf_for`.
- **증분 = insert 계층 dedup 내장**((site_id,post_number,meta_url)) → 재크롤하면 새 문서만 추가. 1,242개 전부 적용.
- **능동 최신화 확인**은 "리스트 1페이지 최신항목 vs DB 대조" = 초 단위, LLM 없음. base_crawler에 **공통 조기종료** 넣으면 전 사이트 앞부분만 순회.
- 전체 코퍼스 신선도: 90일보다 오래된 사이트 0, 문서 77%가 30일 이내(2026-08-06 기준).

# FINO DART 공시 수집기 (정기공시+외부감사관련) 설계 스펙 (2026-07-09)

## 목표

DART 공식 OpenAPI(opendart.fss.or.kr)로 **상장사 전체(유가·코스닥·코넥스)** 의 **정기공시(A)** 와 **외부감사관련(F)** 공시를 **최근 12개월치** 수집한다. 각 공시의 **목록 메타데이터 + 원문 문서(zip)** 를 받고, 정기공시에는 **재무제표(전체계정)** 를 추가 수집한다. 20,000건/일 API 한도 하에서 3~4일에 걸쳐 **중단·재개 가능**하게 동작한다.

## 소스 (실측 검증 완료, 2026-07-09, `.env`의 `DART_API_KEY`로 status 000 확인)

| API | 용도 | 핵심 파라미터 | 반환 |
|---|---|---|---|
| `corpCode.xml` | 기업 고유번호 마스터 | crtfc_key | zip → CORPCODE.xml (corp_code 8자리, corp_name, stock_code 6자리(상장사만), modify_date) |
| `list.json` | 공시 목록 | pblntf_ty(A/F), corp_cls(Y/K/N), bgn_de, end_de, page_no, page_count(≤100) | list[]: rcept_no, corp_code, corp_name, stock_code, corp_cls, report_nm, rcept_dt, flr_nm, rm |
| `document.xml` | 공시 원문 | rcept_no | **zip 파일**(원문 XML/문서) |
| `fnlttSinglAcntAll.json` | 재무제표(전체계정) | corp_code, bsns_year, reprt_code, fs_div(CFS/OFS) | list[]: 계정과목·당기/전기 금액 등 |

실측 제약:
- `list.json`은 **corp_code 없이 전체 조회 시 기간 범위 ≤ 3개월**(초과 시 status 100). → 목록은 기업별 순회가 아니라 **3개월 창 × 4 = 1년, pblntf_ty × corp_cls 조합으로 페이징**(효율적).
- **일 20,000건 한도**(자정 리셋). list+document+financials 합산 약 5~6만 호출 → **3~4일 소요**. 재개 필수.
- 3개월 실측(4~7월): A 유가 969 + 코스닥 2,038. F는 3~4월 감사보고서 시즌 집중. 연 목록 ~2~2.5만 건 추정, 원문 zip 총 ~50-60GB(디스크 4TB 여유).

## 상장사 정의

corpCode.xml에서 **stock_code가 있는 기업**(=상장사). corp_cls는 목록 API에서 Y(유가)·K(코스닥)·N(코넥스). 이 3종을 상장사 전체로 본다.

## 재무제표 매핑 (정기공시 A만)

report_nm에서 보고서 종류·사업연도 파싱 → reprt_code:
- 사업보고서 → `11011`, 반기보고서 → `11012`, 1분기보고서 → `11013`, 3분기보고서 → `11014`
- bsns_year = report_nm의 `(YYYY.MM)`에서 연도. fs_div는 CFS(연결)·OFS(별도) 각각 시도(없으면 status 013 skip).
- 외부감사관련(F)은 재무제표 API 대상 아님 → 원문만.

## 아키텍처 — 신규 모듈 `crawler/fino_dart/`

fino_std/fino_ops 패턴 미러링. DB `data/fino_dart.db`, 원문 `data/fino_dart_docs/`.

```
crawler/fino_dart/
  models.py     Corp / Filing / FinancialRec 데이터클래스
  sources.py    엔드포인트 상수, PBLNTF_TYPES=(A,F), CORP_CLS=(Y,K,N), reprt_code 매핑, deep-link
  client.py     httpx + 일일 쿼터 추적/차단 + 재시도 (DailyQuota)
  fetch.py      fetch_corpcode / fetch_list_page / fetch_document / fetch_financials
  parsers.py    parse_corpcode_zip, parse_list_rows, report_to_reprt(보고서명→reprt_code·연도)
  db.py         corps/filings/documents/financials/api_quota 스키마 + upsert + resume 쿼리
  collect.py    4단계 파이프라인 CLI(corpcode→list→docs→financials), 재개·쿼터·증분
```

- 크롤러 코드는 자체 완결(기존 크롤러 무수정). 완료 후 fino_ops에 `dart` 코퍼스로 편입.

## 데이터 모델 — data/fino_dart.db

```sql
corps(corp_code TEXT PK, corp_name TEXT, stock_code TEXT, modify_date TEXT, collected_at TEXT)

filings(rcp_no TEXT PK, corp_code TEXT, corp_name TEXT, stock_code TEXT, corp_cls TEXT,
        report_nm TEXT, pblntf_ty TEXT, rcept_dt TEXT, flr_nm TEXT, rm TEXT, collected_at TEXT)

documents(rcp_no TEXT PK REFERENCES filings, local_path TEXT, bytes INTEGER,
          status TEXT,  -- ok | empty | error
          fetched_at TEXT)

financials(id INTEGER PK, rcp_no TEXT REFERENCES filings, bsns_year TEXT, reprt_code TEXT,
           fs_div TEXT, fs_json TEXT, status TEXT, fetched_at TEXT,
           UNIQUE(rcp_no, fs_div))

api_quota(day TEXT PK, count INTEGER NOT NULL DEFAULT 0)   -- 일일 호출 카운터(자정 리셋)
```

- deep-link: `https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp_no}`.

## 수집 파이프라인 (4단계, 전부 재개 가능)

1. **corpcode**: corps 테이블이 비었거나 N일 이상 오래되면 corpCode.xml 1회 → 상장사(stock_code 있음)만 upsert. (호출 1회)
2. **list**: (pblntf_ty A,F) × (corp_cls Y,K,N) × (최근 12개월을 3개월 창 4개) 페이징 → filings upsert(rcp_no PK 멱등). (수백 페이지 호출)
3. **docs**: `documents.status='ok'` 없는 filing만 → document.xml zip 저장 → documents upsert. (filing 수만큼)
4. **financials**: pblntf_ty='A'이고 보고서 종류가 재무제표 대상인 filing 중, 해당 fs_div 레코드 없는 것만 → fnlttSinglAcntAll(CFS,OFS) → financials upsert. (정기 정형보고서 수만큼)

각 단계는 독립 실행 가능(`--phase`), `--phase all`은 순서대로.

## 레이트리밋·재개 (핵심 안전장치)

- **DailyQuota**: 매 API 성공 호출 전 `api_quota[오늘]` 확인, `>= LIMIT`(기본 19,500, 20,000에서 여유)이면 `QuotaExhausted` 발생 → 파이프라인이 현재 진행분까지 커밋하고 정상 종료(“일일 한도 도달 — 자정 이후 재실행하면 이어서”). 성공 시 카운터 +1.
- **재개**: 모든 단계가 “이미 있는 것 skip” 기반 → 며칠에 걸쳐 재실행하면 남은 것만. 재실행=이어받기+증분(새 공시 편입).
- **매너**: 요청 지연 기본 0.3s, 재시도 3회(backoff). status 013(데이터 없음)은 정상 skip, status 020(사용한도 초과)은 즉시 중단.

## 오류 처리

- document.xml이 빈 zip/실패 → documents.status='empty'/'error' 기록 후 계속(다음 재실행에서 재시도 가능하도록 error만 재시도 대상).
- 재무제표 status 013(해당 없음) → financials.status='none'로 기록해 재조회 방지.
- API 키 부재/무효(status 010/011) → 즉시 명확한 에러로 중단.

## 검증·테스트

- 유닛(pytest, 실 API 없음): parsers(list 행·corpCode zip·보고서명→reprt_code), db(스키마·upsert 멱등·resume skip 쿼리), DailyQuota(한도 도달 시 QuotaExhausted·카운터 증가), client(MockTransport 재시도).
- 라이브 스모크(최종 태스크, 소량): 최근 1개월·corp_cls=Y·소수 기업으로 list→docs→financials 1건씩 실제 수집해 파일·재무제표 저장 확인. **전체 수집은 별도 실행**(3~4일, 백그라운드/재개).

## fino_ops 편입

7번째 코퍼스 `dart` 등록: count=filings 행수, freshness=max(collected_at), argv=`collect --phase all`. refresh 1회는 일일 쿼터까지 진행 후 정상 종료(new_count=이번에 추가된 filings). 대시보드/CLI에서 진행상황 확인.

## 다운스트림

fino_std 등과 동일: filings/financials export(NDJSON, rcp_no·deep-link) + 원문 zip 경로 → temis/FINO 인덱스·RAG.

## Open Questions

- [ ] 코넥스(N) 포함 여부 최종 확정(기본 포함) — 규모 대비 가치 낮으면 Y,K로 축소 옵션.
- [ ] 원문 zip을 압축 해제·본문 추출까지 할지(1단계는 zip 저장까지, 텍스트 추출은 후속).
- [ ] 전체 수집(3~4일) 실행 주체: 이 세션에서 백그라운드 착수 vs 사용자 수동 — 구현 완료 후 결정.

# FINO 회계 기준서(K-IFRS/GAAP) 크롤러 — 설계 스펙 (2026-07-02)

## 목표

KASB 공식 **회계기준열람서비스(db.kasb.or.kr)** 의 JSON API로 K-IFRS·일반기업회계기준(GAAP) **기준서 전문을 문단 단위**로 수집한다. FINO 기존 인덱스 fino-kifrs-20260625 / fino-gaap-20260625 에 대응하는 원본이며, 문단별 citation deep-link를 포함한다.

## 소스 (실측 확인 완료, 2026-07-02)

React SPA 뒤의 비공개 JSON API. 인증 없음, LLM/PDF 파싱 불필요.

| API | 반환 | 용도 |
|---|---|---|
| `GET /api/title/{stdNum}` | `titles[]` — 섹션 목차 (type: big/mid/small/little/fifth, documentId, ref) | 크롤 백본. **본문+적용사례+결론도출근거+소수의견 전부 포함** |
| `GET /api/content/{stdNum}/{documentId}` | `clauses[]` — title/paragraph 혼합 스트림. paragraph에 `number`, `content`(HTML) | 섹션 본문 |

핵심 실측 사실:
- `api/content`는 **`api/title`의 documentId만** 받는다 (`api/standard-indexes`의 ID는 500).
- **상위(big) 섹션 content는 하위 문단을 전부 포함**한다 (1001의 '재무제표' big = 문단 9~46 53개). → **big 섹션만 순회하면 무중복 전체 수집**.
- 미지원 기준서는 `{"message":"Something went wrong!"}`(HTTP 500) 또는 `{"titles":[]}` 반환 → graceful skip.
- deep-link: `https://db.kasb.or.kr/s/{stdNum}/{paraNum}` (뷰어 라우트, 문단 단위).

## 수집 범위 (KASB판 SPA 번들 typeStds/stdMap에서 추출)

| 타입 | stdNum | 수 |
|---|---|---|
| `kifrs` | 1000(개념체계) + 1001~1117 현행 42종 + 1118(조기적용) | 43 |
| `kifrs_interp` | 해석서 2010~2123 | 19 |
| `kifrs_etc` | 1191·1192(번역서), 10121(적용의견서) | 3 |
| `gaap` | 99(재무회계개념체계), 1~33(장), 60(시행일), 91(보험업준칙), 93(영문양식) | 37 |

- 구기준(1011·1017·1018 등 KASB판 제외 항목)과 주석 처리된 92는 시드에서 제외.
- 91·93·1191·1192는 API 미지원(별도 HTML/PDF 문서), 1118·10121은 현재 빈 목차 → 시드에 두되 **수집 시 [skip] 로그 후 통과** (향후 채워지면 자동 수집).
- 실수집 기대치: **문서 ~96개** (102 시드 − 6 skip).
- 기타기준서·ESG·내부회계·감사기준서는 범위 외 (사용자 합의: K-IFRS+GAAP만).

## 아키텍처 — 신규 모듈 `crawler/fino_std/` (A안, 승인됨)

fino_law의 documents/articles 패턴을 미러링한 독립 모듈. DB는 `data/fino_std.db`.

```
crawler/fino_std/
  models.py    StdTarget / Section / ParagraphRecord
  sources.py   STD_SEEDS (104종 정적 시드) + citation URL 헬퍼
  fetch.py     httpx GET + 재시도/지연 (fetch_titles, fetch_content)
  parsers.py   big 섹션 필터, clauses → 문단 레코드 (HTML→텍스트, 섹션경로 스택)
  db.py        documents / paragraphs 스키마, upsert + replace
  collect.py   CLI: --types kifrs,kifrs_interp,kifrs_etc,gaap / --std N / --delay-seconds
  export_markdown.py, export_ndjson.py
```

- 선택 이유: 소스 성격(JSON API)이 유일해 "소스별 크롤러 분리" 아키텍처 원칙에 부합. 기존 DB 무접촉. B안(fino_law 동거)은 세법 DB 오염, C안(fino_acct)은 문서형 스키마와 불일치로 기각.

## 데이터 모델

```sql
documents(id, std_num INTEGER UNIQUE, std_type TEXT, title TEXT,
          source_url TEXT, collected_at)
paragraphs(id, document_id → documents, para_num TEXT, section_path TEXT,
           body_html TEXT, body_text TEXT, source_url TEXT, seq INTEGER,
           UNIQUE(document_id, seq))
```

- **재수집 = 문서 단위 교체**(DELETE 후 INSERT, 단일 트랜잭션): 상류 개정 시 잔재 없음. 재실행=최신화.
- `section_path`: content 스트림의 title clause를 level 스택으로 유지해 "재무제표 > 일반사항 > 계속기업" 형태로 생성.
- `para_num`: 본문 "1"·"한10.1"·"82A", 결론도출근거 "BC1", 실무지침 "IG7" 등 문자열. 번호 없는 문단은 빈 문자열 허용(순서는 seq).
- `source_url`(문단) = `https://db.kasb.or.kr/s/{std_num}/{para_num}`.

## 수집 흐름

```
for seed in STD_SEEDS(선택 타입):
    titles = fetch_titles(std_num)          # None/빈 titles → [skip] 로그
    bigs = pick_big_sections(titles)         # type=='big'만
    paras = []
    for big in bigs:
        content = fetch_content(std_num, big.documentId)
        paras += parse_content(content)      # title 스택 + paragraph 추출
    upsert_document + replace_paragraphs     # 트랜잭션
```

- 매너: 요청당 지연 0.4s 기본, 3회 재시도(backoff), UA 명시. 예상 호출 ~1,000회(문서 104 + big 섹션 ~800) ≈ 10분 내외.

## 검증·테스트

- 유닛: 실 API 응답 픽스처(tests/fixtures/fino_std/)로 parsers(빅 필터·문단 추출·섹션경로·HTML strip), db(upsert/replace 멱등), collect(fetch 모킹 통합).
- 수집 후: 타입별 문서 수(kifrs 44−1, interp 20, gaap 37−2 등), 문단 수>0 비율, 빈 body 0건, deep-link 표본 대조(`api/paragraphs/content/{std}/{para}`와 본문 일치), `/data_raid/share/회계_KIFRS·GAAP기준서` MD와 기준서 제목 단위 대사.
- LLM 사용 없음. data/*.db는 .gitignore 유지.

## 다운스트림

fino_law과 동일: export MD/NDJSON(문단별 source_url 포함) → FINO 인덱스 재생성 → RAG citation.

## Open Questions (구현 중 확정)

- [ ] BC/IG 문단 deep-link가 뷰어에서 실제 스크롤되는지 표본 확인 (안 되면 섹션 첫 문단 번호로 폴백).
- [ ] 1118(재무제표 표시와 공시)·10121(적용의견서) titles가 채워지는 시점 — 재실행 시 자동 편입 확인.

# NTS 해석사례 정비내역 적재 + 검색 제외 목록 설계 스펙 (2026-07-13)

## 목표

NTS **해석사례 정비내역**(`nts_new.xlsx`, 996건)을 `fino_nts.db`에 적재하고, 정비로 **삭제된 해석사례**를 RAG 검색에서 **아예 제외**할 수 있도록 **제외 목록(external_id)** 을 생성한다. 동시에 "언제·왜·무엇이 삭제됐는지" **삭제 이력**을 별도 테이블로 보존한다.

**핵심 원칙**: 삭제 해석은 답변에 노출하지 않고 검색에서 뺀다(사용자 확정). 기존 `papers`·`doc_index`는 **무수정**(비파괴), 새 테이블만 추가한다.

## 스코프 (이번 스펙 = 데이터층만)

3계층 중 1계층만 구현. 나머지는 후속:
- **이번 스펙 (frwaler / fino_nts.db)**: 정비내역 적재 + 삭제사례 매칭 + 제외목록·이력 export.
- **후속 (ES 인덱스, 별도 리포)**: `nts_index`/`fino-elastic-upload`의 색인 빌드가 제외목록(external_id)을 필터링해 실제 검색에서 제거. **답변 로직 수정 불필요**(제외되면 애초에 검색 안 됨).

대상 DB: `data/fino_nts.db`(현재 fino_ops·export가 쓰는 활성 NTS DB). 원본 `papers.db`(20GB 아카이브)는 이번 범위 밖.

## 소스 데이터 (실측 확인, 2026-07-13)

`nts_new.xlsx` Sheet1, 996 데이터행. 컬럼:
- `번호`(seq), `요약정보`(세목: 부가/양도/법인/상증/종소/조특/국기/국조 등), `사유`(해석 요약), `유지사례`(대체 현행사례, `|` 다중), `삭제사례`(폐기된 예규·판례번호, `|` 다중), `정비사유`, `등록일자`(예: `2026.06.26.`).
- 사례 셀 형식: `번호(YYYY.MM.DD.)` — 번호와 날짜가 괄호로 결합, `|`로 다중.

## 연결 체인 (실증됨)

```
xlsx 삭제사례번호  →  doc_index.doc_number (정확일치)  →  doc_index.doc_id  ( = papers.external_id )  →  RAG 대상 문서
   재산세과-271           서면-2022-법규재산-2905           010000000000555222  = papers.external_id       해석 원문
```

- `doc_index.doc_id` = `papers.external_id`(18자리) 확인됨(서면-2022-법규재산-2905 → doc_id 010000000000555222 → papers 1건).
- **매칭 커버리지**(실측): 삭제사례 번호 2,228개 중 정확일치 612개(27%), 유지사례 1,256개 중 1,076개(86%). 삭제 27%는 정상 — 폐기된 옛 사례는 현행 taxlaw 목록에 없어 애초 미크롤. DB에 없는 것은 제외할 필요도 없음.

## 아키텍처 — 신규 모듈 `crawler/nts_revisions/`

```
crawler/nts_revisions/
  models.py    Revision / RevisionCase / ExcludedDoc 데이터클래스
  parse.py     nts_new.xlsx → Revision 리스트 (사례 셀 → (번호,날짜) 분리)
  match.py     삭제사례번호 → doc_index 정확일치 → doc_id(=external_id), 연도 disambiguation
  db.py        nts_revisions / nts_revision_cases / nts_excluded_docs 스키마 + 멱등 upsert
  load.py      로더(파싱→매칭→적재) + export(제외목록·이력 ndjson) + CLI
```

기존 fino_std/fino_dart 모듈 패턴 준용. `papers`·`doc_index` 테이블은 읽기만(무수정).

## 데이터 모델 — fino_nts.db에 테이블 3종 추가

```sql
nts_revisions (
    id INTEGER PRIMARY KEY, seq INTEGER, tax_category TEXT, summary TEXT,
    reason TEXT, revision_reason TEXT, registered_at TEXT,
    source_file TEXT, loaded_at TEXT
);  -- 996건, 정비내역 원장(=삭제 이력 상위)

nts_revision_cases (
    id INTEGER PRIMARY KEY, revision_id INTEGER REFERENCES nts_revisions(id),
    role TEXT,           -- 'keep' | 'delete'
    case_number TEXT, case_date TEXT,
    matched_doc_id TEXT  -- papers.external_id (매칭 시) / NULL(미매칭)
);  -- 유지/삭제 사례 정규화(| 분리)

nts_excluded_docs (
    external_id TEXT PRIMARY KEY,   -- papers.external_id = 검색 제외 대상
    revision_id INTEGER REFERENCES nts_revisions(id),
    doc_number TEXT, title TEXT,    -- papers/doc_index에서 스냅샷(문서가 빠져도 이력 보존)
    revision_reason TEXT, registered_at TEXT, excluded_at TEXT
);  -- ★ 검색 제외 목록 + 삭제 이력
```

- 세 테이블 모두 신규. `papers`·`doc_index` 스키마·데이터 변경 없음.

## 처리 흐름

1. **parse**: xlsx → Revision(seq, 세목, 요약, 사유, keep_cases[], delete_cases[], 정비사유, 등록일자). 사례 셀은 `|` 분리 후 `번호(날짜)`에서 번호·날짜 추출.
2. **match**: 각 delete_case 번호를 `doc_index.doc_number`와 정확일치 조회. 후보 1건이면 그 doc_id 채택. 다건이면 case_date 연도와 `doc_index.published_date` 연도 일치로 좁힘. 미매칭이면 matched_doc_id=NULL.
3. **load(멱등)**: 같은 source_file의 기존 정비분 삭제 후 재적재. nts_revisions/nts_revision_cases 채우고, 매칭된 delete_case의 external_id를 nts_excluded_docs에 등재(title/doc_number는 doc_index/papers에서 스냅샷).
4. **export**:
   - `data/export/nts_excluded_ids.ndjson` — `{external_id, doc_number, revision_reason, registered_at}` 줄단위. **후속 ES 색인의 제외 필터 입력.**
   - `data/export/nts_deletion_history.ndjson` — nts_revisions 조인 이력(세목·요약·삭제사례·대체사례·사유·일자) 감사용.
   - **삭제 문서를 RAG 검색 소스로 내보내지 않음**(요구대로).

## 오류·엣지 처리

- 사례 셀에 예규번호가 아닌 법령 조문(`소득세법 시행령 제155조의3(...)`)이 섞임 → 조문 패턴은 doc_index 매칭에서 자연히 미매칭(NULL)로 처리, 별도 필터 불필요.
- 한 번호가 doc_index에 여러 건 → 연도 disambiguation, 그래도 다건이면 **전부 제외 목록에 등재**(보수적: 폐기 해석 누출 방지 우선).
- 재실행 멱등: source_file 기준 교체. xlsx가 갱신 배포되면 재적재로 최신화.
- 기존 테이블 무수정 → 백업 불필요, 재크롤 무영향.

## 검증·테스트

- 유닛(pytest, 실 xlsx/DB 없음): parse(사례 `|`·`번호(날짜)` 분리, 조문 혼입), match(정확일치·연도 disambiguation·다건 보수 등재, 임시 doc_index 픽스처), db(3테이블 멱등 upsert·source_file 교체), export(제외목록·이력 ndjson 형식).
- 라이브 검증(최종 태스크): 실 `nts_new.xlsx` + `fino_nts.db`로 적재 → 제외목록 건수(매칭 삭제사례 수)·이력 996건·커버리지(유지86%/삭제27%) 재현, 표본 external_id가 papers에 실재 확인.

## 후속 (문서화만, 이번 범위 밖)

- ES 색인(`nts_index`/`fino-elastic-upload`): `nts_excluded_ids.ndjson`를 색인 빌드/재색인에서 필터링해 해당 external_id 문서를 제거(또는 `excluded=true` 마킹 후 쿼리에서 배제).
- 삭제 이력 조회 UI/CLI(선택): ops가 "무엇이 왜 삭제됐나" 조회.

## Open Questions

- [ ] fino_ops에 별도 코퍼스로 노출할지 — 정비는 크롤이 아닌 로더라 기본 미편입(필요 시 후속).
- [ ] 제외 실현(ES 필터) 시점 — 후속 스펙에서 nts_index 재색인 절차와 함께 확정.

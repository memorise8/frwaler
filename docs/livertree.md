# livertree — 글로벌 시퀀스 PK + 12자리 폴더 레이아웃

## 개요

`livertree` 브랜치는 사이트 무관 단일 정수 시퀀스를 모든 문서의 고유번호로
사용한다. 이 번호 (`1` ~ `999,999,999,999`, 즉 1조) 가 곧
- DB의 PK,
- 디스크의 파일명,
- 디렉터리 분산의 키
역할을 모두 겸한다.

## 폴더 구조

12자리 zero-padded 시퀀스 ID `N` 을 다음과 같이 분산 저장한다.

```
data / N[0:4] / N[4:8] / N.pdf   (원본 PDF)
data / N[0:4] / N[4:8] / N.txt   (PDF 에서 추출한 텍스트)
```

| 시퀀스 | 파일명 | 경로 |
|---|---|---|
| 0 | `000000000000` | `data/0000/0000/000000000000.pdf` |
| 9999 | `000000009999` | `data/0000/0000/000000009999.pdf` |
| 10000 | `000000010000` | `data/0000/0001/000000010000.pdf` |
| 100000000 | `000100000000` | `data/0001/0000/000100000000.pdf` |
| 999999999999 | `999999999999` | `data/9999/9999/999999999999.pdf` |

각 leaf 디렉터리는 최대 10,000개 파일만 보유하며, 1조 한도 안에서 항상
파일시스템 친화적인 분산이 보장된다.

데이터 루트 (`data/`) 는 환경변수 `LIVERTREE_DATA_ROOT` 로 재정의 가능
(`crawler/storage.py` 참조).

## 메타스키마 (`documents` 테이블)

| 컬럼 | 타입 | 의미 |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | 데이터번호 (시퀀스) |
| `crawled_at` | TIMESTAMP | 수집일 (DB INSERT 시각) |
| `site_id` | TEXT FK→sites.id | 수집 사이트 식별자 |
| `external_id` | TEXT | 글번호 (사이트 내 고유, resume용) |
| `meta_url` | TEXT | 메타정보 URL (게시글 페이지) |
| `title` | TEXT | 제목 |
| `published_date` | TEXT | 작성일/출판일 |
| `posted_date` | TEXT | list 게시일 |
| `authors` | TEXT | 저자 (`; ` 로 구분) |
| `publisher` | TEXT | 출판사/기관 (`; ` 로 구분) |
| `journal` | TEXT | 출판된 저널 |
| `pdf_url` | TEXT | 첨부파일 URL |
| `keywords` | TEXT | 키워드 (`, ` 로 구분) |
| `abstract` | TEXT | 초록 |
| `original_filename` | TEXT | 원본 PDF 파일명 |
| `pdf_path` | TEXT | `data/AAAA/BBBB/N.{pdf,hwp,hwpx,bin}` — 다운로드 성공 시 채워짐 |
| `txt_path` | TEXT | `data/AAAA/BBBB/N.txt` — 변환 성공 시 채워짐 (NULL = 미변환) |
| `download_status` | TEXT | `pending` / `downloaded` / `failed` |
| `summary` | TEXT | LLM 요약 (선택) |
| `metadata` | TEXT | JSON: 사이트별 raw 필드 (`documentTypeName`, `documentNumber`, `category`, `doi` 등) |

UNIQUE 제약: `(site_id, external_id)` — 같은 사이트의 같은 글이 두 번
입력되지 않는다.

인덱스: `idx_documents_site`, `idx_documents_site_extid`,
`idx_documents_crawled_at`, `idx_documents_pubdate`.

## 다음번 수집 끝 위치 측정

`(site_id)` 기준 `MAX(external_id)` 를 조회하면 해당 사이트의 가장
최근 수집된 글번호를 얻을 수 있다. 글번호가 없는 사이트는
`meta_url` 을 fallback 으로 사용한다 (URL 구조 변경에 취약하므로
가능하면 글번호 사용 권장). 기존 `doc_index` 테이블의 incremental /
gap-fill 메커니즘은 그대로 동작한다.

## 다운로드: 확장자 결정

`crawler/main.py:cmd_download` 는 매 다운로드마다 다음 순서로 확장자를 결정한다.

1. `pdf_url` 에 `.pdf` / `.hwp` / `.hwpx` 가 명시되어 있으면 그대로 사용
2. 명시 확장자가 없으면:
   - 임시 파일 `data/AAAA/BBBB/N.tmp` 로 다운로드
   - 처음 8 bytes 검사 (`crawler/storage.detect_extension`)
     - `%PDF-` → `.pdf`
     - OLE 시그니처 (`D0 CF 11 E0 ...`) → `.hwp`
     - ZIP 시그니처 (`PK\x03\x04`) → `.hwpx`
     - 매치 없음 → `.bin` (변환 단계가 HWP fallback 시도)
   - `os.replace(.tmp, .{ext})` 로 정식 경로 이동

이 흐름 덕에 `boardDownload.es` / `displayFile.do` 같은 확장자 없는 엔드포인트도
실제 콘텐츠에 맞춰 정확한 확장자로 저장된다.

## 코드 구조

| 파일 | 역할 |
|---|---|
| `crawler/storage.py` | 시퀀스 ID ↔ 경로 변환 유틸 |
| `crawler/db.py` | `documents` 스키마 + `upsert_document` / `update_document_paths` 등 |
| `crawler/base_crawler.py` | `_save_paper` 가 어댑터를 거쳐 `_save_document` 로 라우팅 |
| `crawler/livertree_adapter.py` | 기존 `paper_dict` → 신 `document_dict` 변환 |
| `crawler/main.py` | `cmd_download`, `cmd_convert` 가 신규 폴더 레이아웃에 저장 |
| `crawler/converter.py` | PDF → `.txt` 추출 (markdown 출력은 사용 안 함) |
| `tests/test_storage.py` | 경로 변환 단위 테스트 |
| `scripts/migrate_livertree.py` | 백업 + idempotent CREATE 보조 |

## 적용 방법

### 신규 환경
```bash
# DB 가 없으면 init_db 가 자동 생성
python -m scripts.migrate_livertree
```

### 기존 fino-crawler DB 위에 적용
```bash
# 자동 백업 후 documents 테이블/인덱스만 추가 (papers/doc_index 는 보존)
python -m scripts.migrate_livertree --db data/papers.db
```

기존 `papers` 테이블은 호환성을 위해 그대로 유지되며 livertree 코드는
더 이상 거기에 INSERT 하지 않는다 (모든 새 수집은 `documents` 로 간다).

읽기 측 (`finolaw/src/lib/db.ts`, `crawler.main.cmd_stats`) 도 모두
`documents` 기반으로 마이그레이션됐다. UUID PK 기반 legacy `papers`
행은 `getPaper(id)` 가 비숫자 ID 일 때만 fallback 으로 조회한다.

> **FTS5 (전문검색)** 는 일시적으로 비활성화되어 있다. `papers_fts` 가
> `papers` 행만 인덱싱하므로 livertree 데이터는 LIKE 경로로만 검색된다.
> `documents_fts` 재구축은 후속 PR 에서 진행한다.

> **기존 `papers.db` 위에 적용** 시 `migrate_livertree.py` 는 빈 documents
> 테이블만 만들고 papers 행을 자동 백필하지 않는다. UI 가 documents 만
> 읽으므로 업그레이드 환경에서는 (a) papers → documents 백필 마이그레이션을
> 별도로 작성하거나 (b) 사이트별로 재크롤이 필요하다. 그린필드 환경
> (data 디렉터리/DB 가 비어있는 상태) 에서는 그대로 진행하면 된다.

## 검증

```bash
# 1. 단위 테스트
python -m unittest tests.test_storage -v

# 2. 첫 수집
python -m crawler.main crawl ntrs --limit 2

# 3. 다운로드 + 텍스트 추출
python -m crawler.main download ntrs --limit 2
python -m crawler.main convert ntrs --limit 2
ls data/0000/0000/

# 4. 메타 검증
sqlite3 data/papers.db <<SQL
SELECT id, site_id, external_id, title,
       pdf_path, txt_path, original_filename,
       authors, keywords
FROM documents ORDER BY id;
SQL
```

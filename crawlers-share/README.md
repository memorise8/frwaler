# libertree 크롤러 모음 (독립 실행 패키지)

전 세계 정부·연구기관·통계·공공데이터 사이트 **1,200여 곳**에서 문서(간행물·데이터셋·보도자료·논문 등)의 메타데이터와 PDF를 수집하는 크롤러 모음입니다. 별도 서버·서비스 없이 이 폴더만으로 실행되며, 결과는 로컬 SQLite 파일에 적재됩니다.

> 이 패키지는 **수집(crawl)** 전용입니다. 내부 운영에서 쓰던 LLM 분석·요약·자동 크롤러 생성기, 원본 데이터베이스, API 키는 포함되지 않습니다.

---

## 1. 설치

```bash
# 파이썬 3.10+ 권장
python -m venv .venv && source .venv/bin/activate    # (선택) 가상환경
pip install -r requirements.txt

# 봇 방어(JS 챌린지) 사이트용 브라우저 — 최초 1회
python -m playwright install chromium
```

## 2. 실행

```bash
python run.py --list                    # 등록된 모든 site_id 출력
python run.py --list | wc -l            # 크롤러 개수 확인

python run.py <site_id> --limit 20      # 20건만 (동작 확인용)
python run.py <site_id>                 # 전량 수집
python run.py <site_id> --db out.db     # 저장 경로 지정
```

예:

```bash
python run.py datos-gob-mx-turismo --limit 5      # CKAN(API) 사이트
python run.py gob-mx-agricultura --limit 5        # 봇 방어(Akamai) 사이트
```

결과는 SQLite `documents` 테이블에 저장됩니다. 스키마는 최초 실행 시 자동 생성됩니다.

```bash
sqlite3 libertree.db "SELECT count(*) FROM documents;"
sqlite3 libertree.db "SELECT title, url, published_date FROM documents LIMIT 5;"
```

## 3. 수집 상한 (환경변수)

크롤러는 무한정 도는 대신 아래 상한 안에서 멈춥니다. 전량 수집 시 넉넉히 올리세요.

| 환경변수 | 의미 | 기본값 |
|---|---|---|
| `LIBERTREE_MAX_PAGES` | 페이지 상한 | 200 |
| `LIBERTREE_MAX_WALL_S` | 사이트당 최대 실행 시간(초) | 1500 (25분) |

```bash
LIBERTREE_MAX_PAGES=100000 LIBERTREE_MAX_WALL_S=36000 python run.py <site_id>
```

## 4. API 키가 필요한 일부 사이트

대부분의 크롤러는 키가 필요 없습니다. 일부 사이트(뉴질랜드 교육부, INRAE 등)는 공개 검색 API 키가 필요합니다. `.env.example` 을 `.env` 로 복사하고 해당 키를 채우면 `run.py` 가 자동으로 읽습니다.

```bash
cp .env.example .env
# .env 를 열어 필요한 *_KEY 값을 채웁니다
```

## 5. 크롤러 유형

| 유형 | 방식 | 예 |
|---|---|---|
| 구조화 API | CKAN / DSpace / OAI-PMH / HAL 등 JSON·XML API. total 즉시 반환, 빠름 | `datos-gob-mx-*` (CKAN) |
| HTML 페이징 | 목록 페이지를 넘기며 상세 페이지 파싱 | 대부분의 사이트 |
| 봇 방어 우회 | Cloudflare/Akamai 등 JS 챌린지를 headless Chromium(Playwright)으로 1회 통과 후 curl_cffi 로 고속 페이징 | `gob-mx-*` (Akamai) |

봇 방어 사이트는 `python -m playwright install chromium` 이 필요하며, 챌린지 통과에 사이트당 수십 초가 걸릴 수 있습니다.

## 6. 폴더 구성

```
crawlers-share/
├── run.py                    # 실행 러너 (이 파일부터 사용)
├── list_sites.py             # 카탈로그(site_id·이름·URL) TSV 출력
├── requirements.txt
├── .env.example              # 키가 필요한 사이트용 환경변수 예시
├── README.md
└── crawler/
    ├── base_crawler.py       # 모든 크롤러의 베이스 클래스 (_save_paper 등)
    ├── stealth_fetcher.py    # curl_cffi → playwright → requests 폴백 페처
    ├── playwright_fetcher.py # 브라우저 페치 헬퍼
    ├── db_libertree.py       # SQLite 스키마 / 문서 적재
    ├── generic_crawler.py    # JSON 설정 기반 범용 크롤러
    └── sites/
        ├── __init__.py       # 크롤러 자동 등록 레지스트리
        ├── custom/*.py       # 사이트별 커스텀 크롤러
        └── configs/*.json    # 설정 기반 크롤러 정의
```

## 7. 참고

- 전 과정은 대상 사이트의 공개 페이지/공개 API만 사용합니다. 각 사이트의 이용약관·로봇 정책·요청 속도를 준수하세요(`--delay` 로 간격 조절).
- 새 사이트 추가는 `crawler/sites/custom/` 에 `BaseCrawler` 하위 클래스(`site_id`·`crawl()` 보유) 파일을 두거나, `crawler/sites/configs/` 에 JSON 설정을 추가하면 자동 등록됩니다.

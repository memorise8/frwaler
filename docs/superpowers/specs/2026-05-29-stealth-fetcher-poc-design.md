# Stealth Fetcher PoC — Design Spec

**작성일**: 2026-05-29
**작성 세션**: brainstorming via superpowers
**상태**: 승인 — 즉시 구현 진입

---

## 1. 목적

기존 libertree 자동 수집 체인 종료 후 **246 차단 사이트** 회복을 위한 PoC.

서버 IP(LG U+ ISP, 평판 양호)는 정상이지만 요청 패턴이 봇으로 식별돼 Cloudflare/봇 차단 시스템에 막힘. 진짜 Chrome 브라우저 트래픽처럼 보이게 만드는 **stealth fetcher** 모듈을 PoC로 검증해 production 가치 평가.

PoC 성공 시 추가 ~60-80 hosts 회복 예상 (총 도달 463 → ~530, 64-65%).

---

## 2. 컨텍스트 (필수 사전 지식)

### 2.1 현재 시스템
- 수집 자동 체인 종료 (5/27)
- 적재 사이트 532, documents 353,040
- 차단 카테고리 분포: cloudflare_challenge 194, cloudflare_403 44, still_403 7, still_blocked 246 등
- 서버 IP: `112.217.198.42` (LG DACOM Corporation / AS3786, 한국 청주)
- 봇 검출 시 막히는 사유: 우리 요청의 TLS fingerprint / User-Agent / 헤더 패턴이 봇으로 인식됨

### 2.2 코드베이스 핵심 파일
| 경로 | 역할 |
|---|---|
| `crawler/base_crawler.py` | 모든 크롤러의 base class. `_session` (requests.Session) 보유 |
| `crawler/playwright_fetcher.py` | 기존 playwright 기반 fetcher (SPA 사이트용) |
| `crawler/promote_all.py` | 일괄 promote 스크립트 |
| `data/audit/blocked.csv` | 차단된 사이트 274건 (host, render_class, block_reason) |
| `data/audit/coverage_report.csv` | 1,994 entries 전체 분류 (마스터) |

### 2.3 Python 환경
- Python 3.12 / `.venv/` 가상환경
- 기존 의존성: requests, beautifulsoup4, playwright (이미 설치)

---

## 3. 설계 결정 (Brainstorming 결과)

| # | 결정 사항 | 선택 |
|---|---|---|
| 1 | 결과물 형태 | spec 문서 + 현 세션 즉시 구현 시작 |
| 2 | 작업 범위 | **PoC 먼저** — sample 15개 (10-20개 범위) |
| 3 | 기술 깊이 | **Comprehensive** — playwright stealth + TLS fingerprint (curl_cffi) + Cookie/Referer |
| 4 | 코드 위치 | **`crawler/stealth_fetcher.py`** (신규 모듈, production 통합 고려) |

---

## 4. 아키텍처

### 4.1 모듈 구조

```
crawler/stealth_fetcher.py
├── class StealthSession
│   ├── __init__(timeout, max_retries)
│   ├── fetch_html(url) → tuple(str|None, str reason)
│   └── fetch_pdf(url) → tuple(bytes|None, str reason)
│
├── 내부 fallback 계층 (fetch_html 안에서)
│   1. _curl_cffi_get(url)         — Chrome impersonation, 빠름
│   2. _playwright_stealth(url)    — 풀 브라우저, JS 챌린지 통과
│   3. _requests_fallback(url)     — 마지막 시도, 일반 requests
│
└── classify_response(html, status) → "ok" | "cloudflare_challenge" | "tls_error" | ...
```

### 4.2 5-Layer 자연화 전략

| Layer | 도구 | 효과 |
|---|---|---|
| 1. TLS fingerprint | curl_cffi (Chrome impersonation) | TLS 핸드셰이크가 진짜 Chrome처럼 보임 |
| 2. JS 환경 | playwright + playwright-stealth | navigator.webdriver 숨김, viewport 조정 |
| 3. User-Agent | Chrome / Firefox 6종 회전 | request fingerprint 다양화 |
| 4. 헤더 순서/구성 | curl_cffi가 자동 처리 | Accept, Accept-Language, Sec-Fetch-* 등 |
| 5. Cookie / Referer | 도메인별 자연스럽게 설정 | 첫 GET → JS 챌린지 통과 → 그 후 페이지 |

---

## 5. 데이터 흐름

```
input: data/audit/blocked.csv → sample 15 sites
            ↓
StealthSession.fetch_html(url)
            ↓
classify_response → ok / partial / cloudflare_challenge / timeout / error
            ↓
output: data/audit/stealth_poc_results.json
        {
          "fetched_at": "2026-05-29T...",
          "samples": 15,
          "ok": 8,
          "cloudflare_challenge": 2,
          "timeout": 1,
          "error": 4,
          "per_site": [...]
        }
        + 콘솔 요약 리포트
```

---

## 6. PoC 실행 절차

### 6.1 Sample 선정 (15개)
`data/audit/blocked.csv` 에서 카테고리별 균등 분배:
- cloudflare_challenge: 8 사이트
- still_blocked: 4 사이트
- still_403 / cloudflare_403: 3 사이트

선정 기준: 한국·주요국 정부/연구기관 우선 (가치 높은 사이트로 검증).

### 6.2 실행 스크립트
`scripts/poc_stealth_recovery.py`
- blocked.csv 로딩 + sample 추출
- StealthSession 가동, 사이트별 fetch
- 결과 JSON 저장 + 콘솔 표 출력

### 6.3 결과 분석
- `ok` 비율 ≥ 50% → PoC 성공, production 통합 계획 작성
- < 50% → 디자인 재검토 (TLS lib 교체, captcha API 통합 등)

---

## 7. 검증 메트릭

| 메트릭 | 임계값 | 실패 시 액션 |
|---|---|---|
| HTML 정상 수신율 | ≥ 50% (8/15) | TLS lib 교체 또는 fallback 순서 변경 |
| 사이트당 평균 응답 시간 | < 15초 | timeout 튜닝 |
| graceful fallback 동작 | 100% | 에러 케이스 보강 |
| Cloudflare challenge 페이지 인식 | "Just a moment" 등 키워드 검출 | 분류 정확도 ≥ 90% |

---

## 8. 에러 처리

### 8.1 Fallback 계층
```
fetch_html(url):
  try curl_cffi (timeout 15s)
    → 200 + 합리적 길이 → 성공 반환
    → 외 다른 상태 → 다음 단계
  try playwright_stealth (timeout 45s)
    → "Just a moment" 검출 → cloudflare_challenge
    → 정상 HTML → 성공
  try requests fallback (timeout 10s)
    → 모두 실패 → reason="all_layers_failed" 반환
```

### 8.2 분류 규칙
- HTTP 200 + len(html) > 1000 + "<title>" 포함 + 챌린지 키워드 미포함 → `ok`
- HTTP 200 + "Just a moment" / "Checking your browser" 포함 → `cloudflare_challenge`
- HTTP 403 → `403_forbidden`
- HTTP 404 / 410 → `dead`
- TLS 핸드셰이크 실패 → `tls_error`
- 30초 이상 응답 없음 → `timeout`
- 기타 → `unknown`

---

## 9. 테스트 구조

`tests/test_stealth_fetcher.py`:

```python
def test_stealth_session_initializes():
    """필수 의존성(curl_cffi, playwright-stealth) 설치 확인"""

def test_classify_response_normal_html():
    """정상 HTML → 'ok' 분류"""

def test_classify_response_cloudflare_challenge():
    """'Just a moment' HTML → 'cloudflare_challenge' 분류"""

def test_fetch_html_returns_tuple():
    """fetch_html 반환 시그니처 검증"""
```

네트워크 테스트는 별도 marker (`@pytest.mark.network`)로 격리 — CI에서 skip.

---

## 10. 통합 경로 (PoC 성공 후)

```
[PoC ≥ 50% 성공]
        ↓
1. base_crawler.py에 fetcher 옵션 추가:
   - 기본 `requests` 유지
   - `stealth=True` 시 StealthSession 사용
2. crawler/sites/configs/*.json 에 `"stealth": true` 플래그 추가
   - 차단 사이트만 선택적으로 stealth 사용
3. promote_all.py 에서 stealth 사이트만 별도 batch 실행
4. 결과를 documents 테이블에 적재
```

PoC 실패 시:
- residential proxy 도입 검토 (별도 spec)
- 또는 사이트별 개별 분석 후 수동 회복

---

## 11. 의존성 추가

`requirements.txt` 에 추가:
```
curl_cffi>=0.7.0           # Chrome TLS fingerprint
playwright-stealth>=1.0.6  # Python stealth plugin
```

playwright 자체 + Chromium 바이너리는 이미 설치됨 (기존 SPA 크롤링용).

---

## 12. 구현 단계 (예상 3-4시간)

| Step | 작업 | 시간 |
|---|---|---|
| 1 | 의존성 설치 + import 검증 | 15분 |
| 2 | `crawler/stealth_fetcher.py` 작성 | 1.5시간 |
| 3 | `tests/test_stealth_fetcher.py` 작성 | 30분 |
| 4 | `scripts/poc_stealth_recovery.py` 작성 | 30분 |
| 5 | PoC 실행 + 결과 분석 | 30분 |
| 6 | 결과 요약 보고 + 다음 단계 결정 | 15분 |

---

## 13. 산출 파일

| 파일 | 역할 |
|---|---|
| `crawler/stealth_fetcher.py` | 핵심 모듈 |
| `tests/test_stealth_fetcher.py` | 단위 테스트 |
| `scripts/poc_stealth_recovery.py` | PoC 실행 스크립트 |
| `data/audit/stealth_poc_results.json` | PoC 결과 (JSON) |
| `data/audit/logs/stealth_poc_<ts>.log` | 실행 로그 |
| `requirements.txt` | 의존성 갱신 |

---

## 14. 주의 사항

- DB 직접 write 안 함 (PoC는 결과 JSON만 생성)
- 기존 자동 체인 / promote_all 영향 0 (별도 모듈)
- 의존성 설치는 ruci venv에 (sudo 불필요)
- robots.txt 미준수 사이트는 PoC 대상에서 제외 (`robots_blocked` 분류 제외)

---

## 15. 결과 의존 후속 작업

PoC 결과에 따라:
- **성공**: 별도 plan 문서로 production 통합 (base_crawler + configs 옵션)
- **부분 성공** (30-50%): 일부 사이트만 stealth로 처리, 나머지는 wait
- **실패** (< 30%): residential proxy 옵션 spec 작성 + 의사결정

문서 마지막 갱신: 2026-05-29

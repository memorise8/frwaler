# libertree 자료 품질 검수 문서

> **목적**: libertree 수집 자료를 제3자가 검수할 수 있도록 현재 상태·품질·접근 방법을 정리.
> **작성**: 2026-07-30 (세션 기반). 모든 수치는 `scripts/audit/data_quality_audit.py`로 재현 가능(읽기 전용).
> **DB**: `libertree-app/data/libertree.db` (5.2 GB) · **블롭**: `libertree/` (1.7 TB, 586,385 파일)

> ## ⚠️ 프로젝트 범위 (2026-07-31 정정 — 검수 전 필독)
> 이 프로젝트는 **신문/역사 아카이브 데이터 수집**이 목적이다. (finance/law = 별개의 겹치는 프로젝트.)
> 따라서:
> - **etera-ee(신문 아카이브, 100,403건=20.8%)는 범위 이탈이 아니라 의도한 핵심 수집 대상.**
> - 신문호 초록이 "서지정보 템플릿"(예: `Title:… Type: Ajaleht… Publication date:…`)인 것은 정상 — 신문 한 호엔 초록이 없고, **실제 콘텐츠는 PDF 스캔본**에 있다. 초록 칸의 값은 날조가 아니라 실제 도서관 카탈로그 메타데이터다.
> - **같은 신문 제목의 서로 다른 일자 발행호는 중복이 아니다.** 학술논문식 근접중복(임베딩 cosine) 로직을 적용하면 착시가 생긴다(전체 근접중복 후보의 77%가 etera 신문호였음).
> - 아카이브에선 초록이 아니라 **PDF가 콘텐츠**이므로, 최우선 결함은 §3의 **깨진 PDF 17,584건**이다(etera의 깨진 PDF는 0건, 한국 정부사이트 등에 집중).

---

## 1. 검수용 접근 방법

### 웹 (권장) — Cloudflare Tunnel로 공개됨
- **URL**: https://libertree.financenow.kr
- **인증**: HTTP Basic Auth — 계정 `admin`, 비번은 `libertree-app/.admin-credentials.env` 참조
- **기능**: 카탈로그 브라우즈(대륙/국가/분류), 전문 검색, 문서 상세, 원문 PDF/텍스트 열람
- ⚠️ **주의**: 현재 origin은 개발 서버(`next dev`, 로컬 3002)라 **상시 보장되지 않음**. 내려가 있으면 아래 "재기동" 참고.

### 로컬 실행
```bash
# libertree-app (열람 전용 후속 앱)
cd libertree-app
export LIBERTREE_APP_MODE=read-only
export LIBERTREE_DB_PATH=/data_raid/ruci_workspace/frwaler_job/data/libertree.db
export LIBERTREE_BLOB_ROOT=/data_raid/ruci_workspace/frwaler_job/libertree
set -a; . ./.admin-credentials.env; set +a   # ADMIN_USER/ADMIN_PASSWORD
./node_modules/.bin/next dev -p 3002

# finolaw (기능 많은 관리자 앱: 대시보드/크롤러/요약/리포트) — 3001
cd finolaw && npm run dev   # ADMIN_USER/ADMIN_PASSWORD in finolaw/.env.local (계정 ruci)
```

### 공개 터널 재기동 (origin이 죽었을 때)
```bash
bash scripts/expose_libertree_tunnel.sh              # 설정 검증 + DNS route + cloudflared start/reload
bash scripts/expose_libertree_tunnel.sh --rollback   # 문제 시 롤백
```

---

## 2. 수집 규모 (2026-07-30 기준)

| 항목 | 수치 | 비율 |
|---|---:|---:|
| **총 문서** | **481,669** | 100% |
| PDF 다운로드 | 297,756 | 61.8% |
| 텍스트 추출 | 288,124 | 59.8% |
| 초록 보유 | 481,667 | ~100% |
| 요약 완료 | 66,248 | 13.8% |
| PDF 총 용량 | 1,775 GB | |
| 수집 사이트 | 761 (762 등록) | |
| 수집 기간 | 2026-05-08 ~ 2026-07-20 | |

**48만은 실제 고유 문서 수가 맞습니다** — 중복으로 부풀려진 것이 아님(§4 참조).

---

## 3. 품질 검증 결과 — 종합 판정

> **[2026-07-30 전수 스캔으로 정정됨]** 초기 샘플 기반 "garbage ~1%"는 **과소평가**였다.
> 전체 PDF 297,756건을 매직바이트 전수 검사한 결과 **깨진 PDF는 17,584건(전체 문서의 3.7%, 다운로드 PDF의 5.91%)** 으로 확정. 재현: `python3 scripts/audit/full_pdf_scan.py`.

### 확정 garbage (제거/격리 후보) — 전수 확정
| 유형 | 건수 | 근거 |
|---|---:|---|
| **PDF가 실제로는 HTML/에러** | **17,584** | 블롭에 `%PDF-` 없음 (전수 매직바이트 검사, 297,756건 전량). 목록 `scripts/audit/out/pdf_garbage.csv` |
| **PDF 내용(sha256) 중복 잉여** | **9,706** | 바이트 동일 PDF. 교차사이트 중복 내용 2,040종 포함 |
| 진짜 중복(메타 엄격) | 3,234 | meta_url+title+abstract 전부 동일 = 같은 페이지 2번 저장 |
| 초록이 CSS/HTML 마크업 | 99 | `gform_wrapper` 등 (엄격 패턴). 느슨한 패턴 시 ~1,479 |
| 초록 mojibake(깨진 문자) | 852 | U+FFFD 포함 |
| 요약 30자 미만 | 417 | 요약 계층 취약 |

**깨진 PDF 17,584건의 성격 (통째 폐기 아님)**:
- 100%가 초록 50자+ 메타데이터 정상 → **카탈로그 레코드는 유효, 깨진 것은 PDF 본문뿐**. "본문 없음"으로 취급.
- 이 중 11,065건은 `text_extracted=1` → 비-PDF(HTML 에러/리다이렉트)에서 **쓰레기 텍스트 추출** → 전문검색·본문 레이어 오염.
- 요약까지 오염된 것은 247건뿐(요약 파이프라인이 대부분 회피).
- 집중: 한국 정부·기관 사이트가 로그인/에러 HTML을 `.pdf`로 저장 (§5, §8 참조).

### 오탐이었던 것 (정상인데 garbage로 오인 쉬움)
- "제목에 error" 749건 → 학술용어(`error bounds`, `silent errors`). 정상.
- 5자 미만 제목 9,737건 → CJK 제목(`申报`, `決算`, 지명). 정상.
- 대량 동일 PDF 대용량건(예: Bank of Canada 328KB×8) → 오배정이나 **본문은 실제**. 삭제 금지.
- title+abstract 동일 11,903건 중 **8,808건은 별개 문서**(meta_url 다름, boilerplate 초록). 삭제 금지.

### 콘텐츠 품질 계층
| 계층 | 건수 | 비율 | 의미 |
|---|---:|---:|---|
| A. 고유 초록 | 451,712 | 93.8% | 양호 |
| B. boilerplate 초록 + PDF본문 있음 | 20,243 | 4.2% | 실질 내용은 PDF에 |
| C. boilerplate 초록 + PDF본문 없음 | 9,714 | 2.0% | 실질 취약(대부분 정상 데이터셋 시리즈) |

### 파이프라인 건전성
- 블롭 디스크 정합성: pdf 누락 **0 / 297,756 (전수)**, txt 누락 0%(샘플 3,000). DB↔디스크 완전 일치. ✅ 전수에서도 유지.
- ⚠️ 단, "다운로드 성공(pdf_downloaded=1)" ≠ "유효한 PDF" — 5.91%는 HTML을 PDF로 잘못 저장(위 참조). 크롤러의 콘텐츠 타입 검증 부재가 원인.
- 추출 텍스트 극소(<100자): 0.3%.

---

## 4. 중복 분석 상세

| 중복 정의 | 초과행 | 판정 |
|---|---:|---|
| meta_url만 동일 | 9,632 | 목록페이지 URL 아티팩트, 신뢰불가 |
| title+abstract 동일 | 11,903 | 대부분 가짜중복(아래) |
| └ 진짜중복(meta_url도 동일) | **3,234** | ✅ 안전 제거 대상 → 고유 478,435 |
| └ 가짜중복(URL 다름·boilerplate) | 8,808 | ❌ 별개 문서, 삭제 시 손실 |
| pdf_sha256 동일 | 9,706 | 대용량은 정상, tiny만 garbage |

---

## 5. 문제 집중 사이트

오염 사이트는 **68 / 761 (9%)**에 불과하고, 성격이 3가지로 갈림:

- **A. PDF 완전 파손** (재수집 필요): `comwel-or-kr-comwel`(471, 전부 286B HTML), `research-thea-ie-browse`(446), `srnl-gov-newsroom`, `cnl-ca-news-publications`
- **B. PDF 오배정** (같은 파일 다수 매핑): `doc-cerema-fr-default`(389), `bdap-opendata-rgs-mef-gov-it`(295), `data-nasa-gov-dataset`(357), `data-gov-au-data`(213)
- **C. 추출 실패** (주로 한국 정부/기관, 스캔본·HWP 의심): `search-open-canada`(693), `incheon-go-kr`(616), `forest-go-kr`(519), `book-ioj-go-kr`(500), `gri-re-kr`(489), `kihasa-re-kr`(393)

---

## 6. 결정된 정리 방침 (아직 미적용)

세션에서 합의된 방향 — **탐지 → 소프트 플래그 격리 (재수집 없음, 가역)**:
1. 격리 방식: `documents`에 `quarantined`/`quarantine_reason` 컬럼 추가(소프트, blob·행 보존). **미적용**.
2. 탐지: 메타데이터만 믿지 않고 **블롭 콘텐츠 검증**(`%PDF-` 매직바이트 등) — 오탐 방지.
3. 사용자 요청: "정상(고유 초록 451,712)만 남기고 나머지 격리" — 단, **아직 실행 안 함**. 현재 앱은 481,669 전량 노출 중.

⚠️ 요약 파이프라인은 `text_extracted=1`에서만 돌고 PDF 텍스트(.txt)를 우선 사용(abstract는 fallback). 따라서 깨진 PDF 문서는 요약에서 자동 제외되나, **오배정 PDF로 이미 생성된 요약 ~1,583건**은 검토 필요.

---

## 7. Compact 이후 자료 검증 이어가기

**핵심 진입점** (세션이 리셋돼도 이 파일 + 아래 스크립트로 재개 가능):

| 자원 | 경로 | 용도 |
|---|---|---|
| 통합 감사 스크립트 | `scripts/audit/data_quality_audit.py` | 전 수치 재현(읽기전용·샘플). `python3 scripts/audit/data_quality_audit.py` |
| **PDF 전수 스캔** | `scripts/audit/full_pdf_scan.py` | 깨진 PDF/내용중복 **전수 확정**(~35초). 결과 CSV → `scripts/audit/out/` |
| 터널 노출 스크립트 | `scripts/expose_libertree_tunnel.sh` | 공개 URL 재기동/롤백 |
| 기존 복구 도구 | `scripts/recover_pdfs.py` | `pdf_downloaded=0` PDF 재다운로드(browser UA) |
| 기존 재추출 도구 | `scripts/backfill_text_extraction.py` | `text_extracted=0` 재추출 |
| DB | `libertree-app/data/libertree.db` (=`data/libertree.db` 심볼릭) | |
| 블롭 규칙 | `libertree/XXXX/YYYY/{seq12}.{pdf|txt}` | `crawler/blob_storage.py` |

**미완료 작업 (다음 단계 후보)**:
1. **격리 실제 적용** — `quarantined` 컬럼 추가 + 탐지 스크립트로 확정 garbage/진짜중복 플래그 + libertree-app 쿼리에 `WHERE quarantined=0` 배선.
2. **정식 배포** — dev 서버 → `next build && next start` + systemd 상주 (공개 URL 상시화).
3. **문제 사이트 재수집** — A그룹(comwel 등) 어댑터 수정 후 재수집.
4. **오배정 요약 검토** — 대량 동일 PDF에서 생성된 요약 1,583건.
5. (선택) 추출 실패 C그룹 — 스캔본/HWP 재추출 전략.

**재개 시 첫 명령**:
```bash
python3 scripts/audit/data_quality_audit.py    # 현재 수치 확인 (수집이 진행됐다면 갱신됨)
curl -s -o /dev/null -w "%{http_code}\n" https://libertree.financenow.kr/   # 401이면 공개 정상
```

---

## 8. 배포 기술 메모 (Cloudflare Tunnel)

- 터널: `fino-tunnel` (`f6fbef83-...`), systemd `cloudflared.service`, 설정 `/etc/cloudflared/config.yml`
- ingress: `libertree.financenow.kr → http://localhost:3002` (catch-all `http_status:404` 앞)
- **521 원인이었던 것**: ingress만으로는 부족 — 호스트를 터널로 보내는 **DNS CNAME**이 없어 와일드카드(죽은 origin)로 빠짐. `cloudflared tunnel route dns fino-tunnel libertree.financenow.kr`로 해결(CNAME 영구 생성됨).
- Vercel 배포는 **불가**(5.2GB 로컬 DB + 1.7TB 로컬 블롭 = 서버리스 부적합). 필요 시 영속 디스크 호스팅(Fly.io/VPS) 또는 데이터 계층 이전(Turso+R2) 필요.

# 크롤러 납품 검증 — 재검증(감사의 감사) 결과

> **목적**: 납품 전, 1차 감사 결과가 정말 맞는지 *다른 방법으로* 재검증(접근 A: 계층별 독립 재검증).
> **작성**: 2026-07-31. 모든 수치 재현: `scripts/audit/verify/v1_libmagic.py`, `v2_mapping.py`, `v4_content_llm.py`, `v6_live_safe.py` + `scripts/audit/*`.
> **원칙**: V1~V5 읽기전용, V6 라이브는 프로덕션 무오염(읽기전용 네트워크 + 코드 정적분석).

---

## 0. 한 줄 결론
1차 감사는 **대체로 정확했으나 크롤러 총수에서 큰 누락이 있었다.** 재검증으로 정정·확증 완료. 수집된 데이터의 의미성(구조 99.2%, 내용 ~93%)은 재확인되었고, 손봐야 할 항목이 더 명확해졌다.

---

## 1. 재검증으로 잡은 **정정 사항** (1차 감사가 틀렸던 것)

| 항목 | 1차 감사 | **정정(재검증)** | 근거 |
|---|---|---|---|
| **크롤러 총수** | 783 (custom .py만) | **1,202** (custom 783 + config전용 419) | V2 — `configs/*.json` 437개를 통째로 누락했었음 |
| **진짜 ORPHAN(데이터 있는데 정의 없음)** | 3개 | **0개** | V2 — 그 3개는 config가 정의. 오탐이었음 |
| **데이터 0 크롤러** | 22 | **441** (config전용 416 + custom 25) | V2 — 정의됐지만 미수집인 config 크롤러가 다수 |

**핵심 정정**: 정의된 크롤러 **1,202개 중 실제로 데이터를 수집한 것은 761개(63%)**. 나머지 441개(대부분 config형 선언 크롤러)는 수집 0 — 미실행/실패 여부는 라이브 확인 필요.

---

## 2. 재검증으로 **확증된 것** (1차 감사가 맞았음)

- **파일유형 분류 정확** (V1, libmagic 교차): '깨진 PDF' 중 콘텐츠 보유 **81.8%**(내 82%와 0.1% 차이), 진짜 결함(HTML류) ~3,100(내 3,178과 일치). **정상 PDF 표본 2,000건 = 100% application/pdf → 위양성 0.**
- **FORMAT_MISMATCH는 온전한 문서** (V5): HWP 중앙값 236KB·JPEG 747KB·Office 272KB·PNG 267KB. 썸네일 의심(<20KB) 1~6%뿐 → "콘텐츠 확보, 확장자만 오류(재라벨 대상)" 판정 유효.
- **NO_DATA custom 25개는 전부 실제 크롤러 코드** (V6-B, 190~733줄) — 테스트 스텁 아님. 수집 0의 원인은 미실행/실행실패.
- **크롤러 파일 구문오류 0** (1차), 파일명=코드 site_id 불일치 0 (V2).

---

## 3. 재검증으로 **세분·보정된 것**

### 3-1. "DEFECT 14개"는 균질하지 않음 (V6-A, pdf_url 실시간 GET)
| 유형 | 크롤러 | 시사점 |
|---|---|---|
| 진짜 HTML 저장 | gri-re, comwel, research-thea, krihs-re, didaktorika (~5) | 다운로드 로직 수정 필요 |
| 지금은 차단(403/봇) | dtic-dimensions, rdr-kuleuven, idiap-ch, issnationallab (~4) | 헤더/세션/안티봇 대응 필요 |
| 지금은 실제 콘텐츠 반환 | sodha(PDF), economie-fgov(PDF), en-ird(PDF), pps-go-kr(HWP) (~4) | 재실행 시 성공 가능 (일시적 실패였을 수) |
| 서버 500 | book-ioj (1) | 원본 사이트 문제 |

### 3-2. 내용 정합성 (V4, 크롤러당 ≥1 표본 LLM, 1,520건)
- **OK 92.6% / JUNK 5.9% / MISMATCH 1.5%.**
- 단, 실물 교정 결과 **JUNK/MISMATCH의 약 절반은 오탐**: LLM이 서지·데이터셋 메타형식(etera, 데이터카탈로그)이나 추출 뒤집힘(bfr-bund)을 실패로 오판.
- **진짜 내용결함도 존재**(소수): 오배정(bankofcanada 제목2021↔본문2015, cnl-ca), 초록 네비게이션 혼입(mois-go-kr). 추정 진짜 결함률 ~3~4%.

---

## 4. 정정된 납품 검증 최종표

| 상태 | 크롤러 | 문서 | 신뢰도 |
|---|---:|---:|---|
| **정의된 크롤러 총수** | **1,202** | — | 확정(V2) |
| ├ 데이터 수집 성공 | **761** | 481,669 | 확정 |
| │  ├ 정상 납품가능(구조+내용 양호) | ~740 | ~455,000 | 높음(V1/V4/V5) |
| │  ├ FORMAT_MISMATCH(재라벨 대상) | 44 | ~21,000 | 확정, 콘텐츠 온전 |
| │  └ DEFECT(수정 필요, 세분됨) | 14 | ~4,000 | §3-1 참조 |
| └ 데이터 0 (미실행/실패) | **441** | 0 | config전용 416 + custom 25 |

**데이터 의미성**: 구조적 99.2% + 내용적 ~93%(오탐 제외 시 ~96%). **수집된 481,669건은 납품 가능 수준**이며, 결함은 소수 크롤러에 집중.

---

## 5. 남은 처리 목록 (납품 전, 우선순위)
1. **441 데이터 0 크롤러 처리 방침 결정** — config 선언만 하고 미수집인 416개를 (a)납품 목록에서 제외 (b)실행해서 수집 (c)실패원인 진단. custom 25개는 라이브 재실행으로 원인 규명.
2. **DEFECT 14개 세분 대응** — HTML 저장 5개는 로직 수정, 차단 4개는 헤더/세션, "지금 되는" 4개는 재실행.
3. **FORMAT_MISMATCH 44개 재라벨** — .pdf→실제 확장자(.hwp/.jpg 등). 콘텐츠 재수집 불필요.
4. **내용결함 표본 정밀검토** — 오배정(bankofcanada류)·네비혼입(mois류) 실제 범위 확인.

## 6. 재현
```bash
.venv-embed/bin/python scripts/audit/verify/v2_mapping.py       # 크롤러↔사이트 정합(1202/761/441)
.venv-embed/bin/python scripts/audit/verify/v1_libmagic.py      # 파일유형 재분류
.venv-embed/bin/python scripts/audit/verify/v5 ...              # (site_profile/format 크기)
.venv-embed/bin/python scripts/audit/verify/v6_live_safe.py     # DEFECT 실시간 진단 + NO_DATA 분류
.venv-embed/bin/python scripts/audit/verify/v4_content_llm.py   # 내용 정합성 LLM 표본 (GPU)
```
CSV 산출: `scripts/audit/out/` (v4_content_llm.csv, crawler_delivery_audit.csv, pdf_scan_by_site.csv 등)

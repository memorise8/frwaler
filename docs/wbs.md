# WBS — livertree (2026-05-01 ~ 2026-07-31)

## 단계별 일정

### 5월 — 핵심 파이프라인 + AI 요약 + 관리자 UI 보강

| 주차 | 작업 | 산출물 | 상태 |
|---|---|---|---|
| 1주 (5/1~5/3) | livertree 스키마 (`documents`) + 12자리 계층 + 어댑터 | `crawler/db.py`, `crawler/storage.py`, `crawler/livertree_adapter.py`, `tests/test_storage.py` (8 cases) | ✅ |
| 1주 (5/3) | Codex 10 라운드 리뷰 → P1/P2 fix | metadata 재도입, txt_path semantics, magic-byte sniffer, FTS 일시 disable, agent_tools/codex_runner 정합성, summary 보존, URL change reset, cleanup_doc_files | ✅ |
| 1주 (5/3) | 계약서 / 프로젝트 계획서 / 잔여 작업 정의 | `docs/프로젝트계획서.md`, `livertree-shimmying-otter.md` 플랜 확장 | ✅ |
| 2주 (5/3 후속) | Phase A — `cmd_summarize` AI 요약 파이프라인 | `crawler/summarizer.py`, `cmd_summarize`, 4 신규 테스트 | ✅ |
| 2주 | Phase B — `/admin/status` 수집 현황 | `getCollectionProgress`, page.tsx, NAV 추가 | ✅ |
| 2주 | Phase C — `/admin/summary` 요약 결과 + 재요약 API | `getDocumentsBySummaryStatus`, page.tsx, RegenerateButton, `/api/admin/summarize` | ✅ |
| 2주 | Phase D — FTS5 `documents_fts` 재구축 | `scripts/migrate_documents_fts.py`, `searchPapers` FTS path 복원 | ✅ |
| 2주 | Phase F — QA 검증 스크립트 | `qa_crawl_accuracy.py`, `qa_summary_quality.py` | ✅ |
| 2주 | Phase E — 배포 자동화 (스케줄러 + 가이드) | supervisord scheduler 추가, `cron_crawl.sh`, `docs/deploy.md`, `.dockerignore` | ✅ |
| 2주 | Phase G — 정식 산출물 문서 (요구사항, 기능정의, WBS, API 명세, 테스트 보고서, 운영매뉴얼, 개발환경, 인프라) | 본 시점 작성 중 | 🟡 |

### 6월 — 실데이터 운영 + 품질 평가

| 작업 | 산출물 | 추정 |
|---|---|---|
| 5사이트 × 100건 실데이터 수집 + qa_crawl_accuracy 점검 | xlsx 보고서 | 3일 |
| AI 요약 30건 LLM-as-judge 평가 + 프롬프트 튜닝 | 품질 리포트 | 2일 |
| 신규 사이트 (Auto-Add) 추가 검증 | generic config 5종 추가 | 3일 |
| FTS5 검색 사용자 수용 테스트 | 피드백 → 토크나이저/필터 조정 | 2일 |
| 모니터링/로그 강화 (실패 알림) | scheduler 로그 알람 | 2일 |
| 문서 보강 + UI 개선 사항 반영 | 페이지 수정 | 5일 |

### 7월 — 통합 테스트 + 검수 + 기술 이전

| 작업 | 산출물 | 추정 |
|---|---|---|
| 본 운영 환경 배포 | docker compose up + 도메인/HTTPS | 3일 |
| 통합 테스트 (전체 시나리오) | 결과 보고서 | 5일 |
| 사용자 수용 테스트 | 피드백 처리 | 5일 |
| 운영 매뉴얼 + 사용 가이드 최종화 | Notion / PDF | 3일 |
| 기술 이전 (소스 인계, 운영 시연) | 인계 회의록 | 2일 |
| 검수 (계약서 산출물 16종 충족 확인) | 검수 체크리스트 | 2일 |

## 누적 진척도 (2026-05-03 기준)

| 영역 | 완료율 | 비고 |
|---|---|---|
| 백엔드 (크롤러 + 파이프라인) | 100% | crawl/download/convert/summarize 전 단계 + auto-add |
| Frontend (UI) | 95% | 5개 핵심 페이지 + admin 2종 추가 완료 |
| DB / 저장소 | 100% | livertree 스키마 + FTS5 + 12자리 계층 |
| QA / 테스트 | 80% | 26 단위 테스트 + Codex 10 라운드 + qa 스크립트 (실데이터 평가는 6월) |
| 배포 | 90% | Dockerfile + compose + scheduler 완비 (실 배포는 6월) |
| 문서 | 70% | 핵심 7종 작성 완료, Excel/Notion 변환 잔존 |

## 마일스톤

| 일자 | 마일스톤 |
|---|---|
| 2026-05-31 | 모든 코드 산출물 완료 + 정식 문서 1차본 |
| 2026-06-30 | 실데이터 운영 + 품질 검증 완료 |
| 2026-07-15 | 통합 테스트 통과 |
| 2026-07-31 | 검수 + 기술 이전 완료 (납품일) |

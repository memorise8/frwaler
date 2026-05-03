# 테스트 결과 보고서 — livertree (2026-05-03)

## 1. 단위 테스트 (`tests/test_storage.py`)

총 **26 케이스 / 26 통과 (100%)** — `python -m unittest tests.test_storage -v`

### TestDocIdToPath (8 cases)
| 테스트 | 검증 내용 |
|---|---|
| `test_pdf_paths_match_user_examples` | 사용자 명세 6 케이스 (0/9999/10000/20000/100000000/999999999999) |
| `test_txt_paths_mirror_pdf_with_extension` | .txt 경로가 .pdf 와 동일 base + 확장자만 변경 |
| `test_extension_normalisation` | `.pdf` 와 `pdf` 모두 동일 결과 |
| `test_relative_helpers` | doc_id_to_relative_pdf/txt 정확성 |
| `test_negative_id_rejected` | doc_id < 0 → ValueError |
| `test_overflow_rejected` | doc_id > 10¹² → ValueError |
| `test_non_integer_rejected` | str/bool 입력 → TypeError |
| `test_ensure_parent_creates_directory` | 부모 디렉터리 자동 생성 |

### TestDetectExtension (5 cases)
PDF / HWP (OLE) / HWPX (ZIP) / 비매치(.bin) / 빈 입력(.bin) magic-byte 분기.

### TestFollowupBehaviour (5 cases)
P1/P2 리뷰 fix 회귀 방지:
- `test_save_document_does_not_set_paths` — `_save_document` 후 pdf/txt_path NULL
- `test_pending_convert_uses_txt_path_null_marker` — 변환 큐 정합성
- `test_paper_to_document_preserves_metadata` — 어댑터 metadata 보존 + category/doi promote
- `test_get_stats_counts_documents_not_papers` — stats 가 papers 가 아닌 documents 카운트
- `test_convert_limit_advances_past_converted_rows` — `--limit` 무한 루프 방지
- `test_recrawl_preserves_llm_summary_and_metadata` — recrawl 시 summary/metadata 보존
- `test_recrawl_with_pdf_url_change_resets_file_state` — URL 변경 시 paths/status reset + 디스크 cleanup

### TestSummarizer (4 cases)
Phase A AI 요약 통합:
- `test_summarize_text_routes_to_provider` — provider arg / env 라우팅
- `test_summarize_text_returns_none_without_api_key` — graceful no-key fallback
- `test_cmd_summarize_updates_summary_column` — 큐 처리 + DB persist
- `test_cmd_summarize_doc_id_targets_single_row` — `--doc-id` 단일 row 모드

## 2. 코드 리뷰 (Codex 10 라운드)

각 라운드는 작업 트리 변경에 대해 GPT-5.4 기반 Codex 리뷰어가 P1/P2/P3
이슈를 식별 → fix → 재리뷰 사이클. 100% 발견 → 100% 해소율.

| 라운드 | 발견 | 결과 |
|---|---|---|
| 1 | P1 papers reader 미동기화, P2 txt_path semantics, P2 magic-byte sniffing 누락 | fix |
| 2 | P2 smart_find txt_path 미수정, P1 legacy backfill (OOS) | fix + OOS 확정 |
| 3 | P2 NTS incremental papers 쿼리, P2 summary erasure on recrawl | fix |
| 4 | P2 agent_tools papers 쿼리 | fix |
| 5 | P2 converter skip txt_path 미동기화, P2 codex_runner template | fix |
| 6 | P2 convert --limit 무한 루프, P1 legacy backfill (재거론, OOS) | fix |
| 7 | P2 export_papers_md keywords format, P3 pending status 카운팅 | fix |
| 8 | P2 pdf_url 변경 시 file state reset 누락 | fix |
| 9 | P2 stale 디스크 파일 reuse (8번 fix 의 후속) | fix (cleanup_doc_files) |
| 10 | P1 legacy backfill 만 재거론 (OOS 확정 항목) | 종료 — 새 actionable 0건 |

## 3. End-to-End 시나리오 검증

mock 환경 (LIVERTREE_DATA_ROOT=tmp + subprocess monkeypatch + LLM stub)
에서 다음 시나리오 100% 통과:

1. paper_dict (NTS-shape) → 어댑터 → documents.metadata 보존 (NTS 필드 + category/doi promote 확인)
2. `_save_paper` → documents 행 1, paths NULL
3. extensionless `boardDownload.es?bid=…` → magic-byte 0xD0CF11E0 → `.hwp` 으로 정확 분기
4. download status='downloaded' → pending_convert 큐에 정확히 노출
5. `cmd_summarize` mock → `summary` 컬럼 채워짐, recrawl 시 보존
6. URL 변경 시 file state + 디스크 파일 모두 초기화

## 4. 정적 검사

| 도구 | 대상 | 결과 |
|---|---|---|
| `tsc --noEmit` | `finolaw/src` 전체 | 0 errors |
| `eslint` (next/core-web-vitals) | `finolaw/src` 전체 | 0 errors / warnings |
| `bash -n` | `scripts/cron_crawl.sh` | OK |
| `docker compose config` | `docker-compose.yml` | OK (env_file 더미 후) |

## 5. QA 스크립트 검증

`scripts/qa_crawl_accuracy.py` + `scripts/qa_summary_quality.py` 를 픽스처
DB (8 row) 에 적용 → 사이트별 카운트, 진행률, 휴리스틱 결과 모두 정상 출력.

## 6. 발견된 한계 / 후속 작업

| 항목 | 분류 | 처리 |
|---|---|---|
| Korean 2자 검색어 | trigram 한계 | `searchPapers` 가 자동 LIKE 폴백 |
| legacy `papers.db` 백필 | 그린필드 가정으로 OOS | 사용자 확정 |
| 실데이터 5사이트 × 100건 검증 | 6월 운영 단계 | 일정 준수 |
| LLM-as-judge 요약 평가 | 6월 운영 단계 | 일정 준수 |

## 7. 결론

전체 단위/통합/정적 검사 통과. Codex 다중 라운드 리뷰 사이클로 회귀 방지
망 구축. 6월 실데이터 운영 + LLM judge 까지 완료되면 계약 검수 기준 충족.

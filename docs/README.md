# Libertree 프로젝트 문서

**프로젝트**: 1,994개 글로벌 정부·연구 사이트의 메타+PDF+요약 수집 시스템

**마지막 갱신**: 2026-07-10

---

## 📚 활성 문서 (현재 작업용)

| 문서 | 용도 | 우선순위 |
|---|---|---|
| **[CURRENT_STATE.md](CURRENT_STATE.md)** | 🔥 **프로젝트 종합 상태 (2026-07-10)** — 새 세션 시작 시 **첫 번째 읽기** | ⭐⭐ 필수 |
| **[NEXT_SESSION_PROMPT.md](NEXT_SESSION_PROMPT.md)** | 새 세션 복사용 재개 프롬프트 (요약판) | ⭐ |
| [미수집_사이트_설명.md](미수집_사이트_설명.md) | 미수집 202 사이트 분류 설명 (비개발자용) | 참고 |
| [country_library_ui_plan.md](country_library_ui_plan.md) | 신규 UI: 나라별 도서관 페이지 트리 계획 (~6h) | 보류 |
| [dashboard_1994_funnel_plan.md](dashboard_1994_funnel_plan.md) | 신규 UI: 대시보드 funnel 추가 (~2h) | 보류 |
| [uncollected_sites_directory_plan.md](uncollected_sites_directory_plan.md) | 신규 UI: 수집 불가 사이트 디렉토리 (~2h) | 보류 |
| [SESSION_HANDOFF_2026-06-05.md](SESSION_HANDOFF_2026-06-05.md) | 과거 세션 핸드오프 (이력용) | 아카이브 |

개발 상세 이력(페이즈별 변경 로그)은 저장소 루트의 `libertree_crawler.md` 참조.

---

## 🚀 새 세션 시작 가이드

```bash
cd /data_raid/ruci_workspace/frwaler_job
cat docs/CURRENT_STATE.md          # ← 이거 먼저 (상태 확인 명령 포함)
```

---

## 🌐 접속 정보 (Production)

- URL: **https://celebrity-annie-schedules-passing.trycloudflare.com**
- ID: `ruci`
- Password: `aKInNf7gljXlDHIj1P8t`

(URL은 cloudflared 재시작 시 변경됨 — CURRENT_STATE.md 참조)

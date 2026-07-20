# 다음 세션 시작 프롬프트

새 세션에서 아래 블록을 그대로 복사해 붙여넣으면 컨텍스트 0에서 바로 이어서 작업할 수 있습니다.

---

## 복사용 프롬프트 (이 아래를 붙여넣기)

```
libertree 수집 파이프라인 프로젝트를 이어서 작업한다. 작업 디렉토리는 /data_raid/ruci_workspace/frwaler_job.

## 현재 상태 (2026-07-20 기준, 반드시 DB로 재확인)
- data/libertree.db: 문서 481,669건 / 적재 hosts 694/832 (83.4%)
- pdf_downloaded ~297,800 / text_extracted ~288,100
- 2026-07-18~20 대규모 수집 완료(증분 재크롤+재개방 46곳+robots허가 37곳), 시스템 idle
- Gemma 요약 트랙은 일시중지 유지
- finolaw 웹앱 도커 가동 중(포트 3001, Basic Auth). 납품 패키지(외장디스크)는 스냅샷·manifest 미착수

## 상태 확인 명령 (첫 실행)
.venv/bin/python -c "import sqlite3; c=sqlite3.connect('data/libertree.db'); print('docs', c.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 'pdf', c.execute('SELECT COUNT(*) FROM documents WHERE pdf_downloaded=1').fetchone()[0])"
ps -ef | grep -E "recover_pdfs|promote_all|backfill" | grep -v grep

## 최근 완료된 것 (6월~7/14)
- PDF 회복 프로젝트: +21K PDF, +29K text 회복 (recover_pdfs.py로 UA/Referer/verify=False/GET 결손 보완)
- fake-HTML 블록 정리, converter.py에 HWP/HWPX 매직바이트 감지 추가
- 미수집 사이트를 사유별 분류 + 비개발자용 보고 파일 작성 완료
  → 대표본: data/audit/수집현황_전체.xlsx (시트: 수집성공635 / 미수집198)
- 라벨오류 18개 정정 (7/7, 613→631) + livertree→libertree 리네이밍 (7/7)
- 메타데이터 정제 + 가짜 PDF 3,704건 리셋 + masaf/그리스 백필 (7/11, 상세: data/audit/metadata_cleanup_final_report.md)
- "회원가입 필요 51" 전수 실사로 봇차단 재분류 (7/13) + 재개방 4사이트 수집·cap 확장 재수집 (7/14~17, 631→635, +8,143건)

## 미수집 198 분류 (7/13 실사 반영)
정책상포기 63 / 봇차단(전면37+부분9) 46 / 재시도실패 48 / 절대불가 38 / 확인중 2 / 미분류 1(gob.mx)
- 남은 경로는 전부 수동: 봇차단 쿠키작업(46), 운영자 컨택(63), Wayback(38)

## 다음에 할 수 있는 작업 (우선순위)
1. 봇차단 46 사이트 수동 쿠키 작업 (순서표: data/audit/로그인사이트_실사_20260713.csv)
2. Gemma 요약 트랙 재개 (API key 트랙)
3. (완료 2026-07-07) livertree → libertree 리네이밍

## 주의사항
- runner 결과를 메인 디렉토리로 cp할 때 cp -n 금지 (옛 실패 JSON이 새 성공 결과를 가림)
- summary_model은 UI 노출 금지
- DB는 운영 중 — 쓰기는 promote_all/runner/recover_pdfs만
- 크롤러 생성기는 OAuth, analyzer/summarizer는 API key (비용 정책, 변경 금지)

먼저 위 상태 확인 명령을 실행해서 현재 DB 상태를 파악한 뒤, 내가 지시하는 작업을 진행해줘.
```

---

## 참고: 세션 정리 방법
- 가볍게 이어가기: `/compact` (맥락 요약 유지)
- 완전 초기화: `/clear` → 위 프롬프트 붙여넣기 (메모리도 자동 로드됨)

# Libertree Delivery 최종 준비 상태

## 제품 기능

- 문서 검색·필터·facet·페이지네이션, 상세·번역·요약 열람
- PDF·추출 텍스트의 정규 경로 기반 안전 제공
- 크롤러 등록·실행, 상태·영속 로그, 실패 재시도, 대기/실행 작업 취소
- 사이트별 증분 수집 예약 생성·변경·중지·재개와 중복 enqueue 방지
- 내부 Qwen/외부 OpenAI 호환 provider, 작업 lease·재시도·circuit breaker·batch 제한
- 자동 요약 품질 판정, 위험 큐와 품질 현황 화면
- 단일 고객용 Cloudflare Access 경계와 서버측 Delivery API token
- DB 현황·정합성·최신화 및 운영 관측 화면

## 2026-08-13 검증 결과

- 대표 크롤러 5종(HTML, API, 논문, 보고서, Playwright): 각각 임시 PostgreSQL에 3건 저장 PASS
- Python/PG/BE/Worker 전체: 157개 중 156 PASS, 1개는 worktree에 호환 symlink가 없는 환경 가정 테스트
- 신규 스케줄·협력 취소/API 통합: 27 PASS
- FE typecheck, ESLint, Next.js production build PASS
- PostgreSQL·BE·Worker·FE 4컨테이너 build/start/health PASS
- 읽기 전용 dump → 빈 임시 DB restore → 행 수·seq checksum·SHA-256 PASS
- 스냅샷 schema additive 적용과 원본/대상 집계 대조 PASS

## 운영 반영 전 남은 승인 작업

아래는 제품 코드 미완료가 아니라 실제 자산 또는 외부 인프라에 영향을 주므로 자동 실행하지 않았다.

1. 실데이터 PostgreSQL dump/restore와 1.8TB blob manifest 최종 생성
2. 실데이터 복제 스냅샷에 additive schema 적용
3. GPU 0 내부 Qwen endpoint로 고정 100건 canary 및 품질/지연 관찰
4. 운영 schema 적용, 실제 blob read-only mount, 최신 이미지 전환
5. 현재 서버 Cloudflare Tunnel에 새 FE hostname route 추가
6. 고객 계정 1개만 허용하는 Cloudflare Access 정책 적용
7. 새 서버 재부팅 후 데이터 유지·복구 최종 확인

실행 순서는 `DELIVERY_DEPLOYMENT_RUNBOOK_20260813.md`를 따른다. 운영 SQLite, 기존 Elasticsearch, GPU 1과 기존 Tunnel route는 변경하지 않는다.

## 납품 산출물

커밋된 소스만 패키징하고 비밀정보·DB·blob을 제외한다.

```bash
mkdir -p /verified/release
DELIVERY_RELEASE_CONFIRM=BUILD_COMMITTED_SOURCE_BUNDLE \
RELEASE_DIR=/verified/release \
delivery/scripts/build_release_bundle.sh
```

생성된 archive와 manifest는 mode 0600이며 manifest에 Git revision과 archive SHA-256이 기록된다.

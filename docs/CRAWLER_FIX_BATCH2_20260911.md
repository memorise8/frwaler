# 수집기 순차 수정 2차 — 2026-09-11

앞선 수정 커밋 `d84e5ca`는 원격의 기존 변경과 충돌 없이 병합한 `1b1b33f`를 통해 `origin/libertree`에 반영했다. HTTPS 인증이 없어 기존 SSH 인증으로 일반 push를 수행했다. 강제 push는 사용하지 않았다.

## 404 오류 출처 2개 복구

| 출처 | 이전 문제 | 변경 | 검증 |
|---|---|---|---|
| `arcep-fr` | 오래된 필터 매개변수·목록 주소로 HTTP 404, 표 선택자도 실제 목록과 불일치 | 기존 `arcep-fr-actualites`의 연차보고서 필터·페이지 넘김·브라우저 처리를 재사용하는 전용 클래스 등록 | 문서 3건 저장 |
| `scaht-org` | 폐기된 `/en/research/publications/`가 HTTP 404 | 기존 `scaht-org-en`의 출판물 상세·원출처 메타데이터 처리를 재사용하고 시작 주소를 `/en/publications/`로 변경 | 문서 3건 저장 |

두 출처의 기존 식별자를 유지한다. 불완전한 JSON 설정을 제거하고 동일 식별자의 전용 클래스로 교체했으며, 전체 등록 우선순위는 변경하지 않았다. 각 전용 구현의 원래 식별자도 유지한다. 이미 존재하는 출처 간 자료 중복을 통합하는 변경은 아니다.

ARCEP 표본에는 `L'état d'internet en France - édition 2026`, `L'Arcep et les marchés régulés` 등의 제목·발행일·PDF 링크가 저장됐다. ARCEP의 abstract는 기존 구현이 목록의 제목·문서 설명을 조합한 값이며 원문 전체 요약이 아니다. SCAHT 표본에서는 문서 제목·발행일·저자·초록을 확인했다. PDF 링크가 없는 문서에 가짜 PDF 주소를 추가하지 않는다.

## 새 Docker 이미지 실행에 필요한 의존성 보완

현재 브랜치의 `delivery/Dockerfile.worker`에는 Playwright Python 패키지만 있고 Chromium 실행 파일과 시스템 라이브러리는 없었다. 또한 일부 수집기가 호출하는 `pdftotext`도 없었다.

- `python -m playwright install --with-deps chromium` 추가.
- `poppler-utils` 설치 추가.
- 다른 납품 브랜치의 워커·큐·프런트엔드 코드는 일괄 교체하지 않았다.

## 검증 환경과 결과

`libertree-crawler-audit:20260911-fixed` 이미지를 현재 Dockerfile로 빌드했다. 최종 검사는 crawler 폴더를 호스트에서 덮어씌우지 않고 **이미지에 COPY된 코드**를 사용했다.

- 회귀·저장·메타데이터 검사 82개 통과. 기존 식별자가 의도한 전용 구현으로 등록되는 검사에 두 출처를 추가했다.
- Chromium 실행 성공, `pdftotext -v` 실행 성공.
- 레지스트리 1,243개, Python/JSON 구문 오류 0, 포착된 import 오류 0.
- 두 수집기를 각각 최대 3건·90초, 동시 실행 1개로 검사했다. 출처별 임시 SQLite에 실제 저장했다. 운영 DB·원문 저장소·운영 컨테이너는 사용하지 않았다.
- 이미지의 두 수집기 모두 3건씩, 총 6건 저장. 전체 기간·모든 PDF의 다운로드 및 변환을 보증하는 검사는 아니다.

로컬 증거 자료: `scripts/audit/out/fixes_20260911_batch2/`

- `build.log`, `build_final.log`: Docker 빌드
- `tests.log`, `registry_fresh.json`, `pdftotext.log`: 자동 검사·실행 의존성
- `arcep_initial/`, `scaht_initial/`: 순차 수정 직후 검사
- `fresh_live/`: 최종 이미지에 포함된 코드로 검사
- `summary.json`: 최종 표본 저장 결과

기존 전수 감사 및 1차 수정 결과는 보존한다. 이번 2개를 제외한 BAW, BFE, GSI, Pasteur의 404와 NHC의 502, 다른 시간 초과·0건·외부 차단은 아직 해결됐다고 판정하지 않았다. 노동부의 HTTP 403도 남아 있다. 기존 ScienceON 공통 등록 누락과 18개 중복 식별자는 이번 변경 범위 밖이다.

검증 이미지 빌드는 운영 배포가 아니다. 공유 서버에서 반영하려면 최신 브랜치를 받은 뒤 워커 이미지를 재빌드하고 컨테이너를 재생성해야 한다.

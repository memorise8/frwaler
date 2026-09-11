# 수집기 오류 수정 결과 — 2026-09-11

확정 오류 7개와 예산보고서 오수집 1개를 수정 대상으로 검사했다. **8개 중 7개에서 각각 3건, 총 21건의 문서 메타데이터 저장을 확인했다.** 노동부는 설정 누락을 보완했지만 HTTP 403 때문에 실제 문서 선택·저장은 아직 검증하지 못했다. 전체 수집기의 정상화나 전량 수집 완료를 의미하지 않는다.

## 수정 내용

| 식별자 | 원인 및 변경 | 재검증 |
|---|---|---|
| `noc-ac-uk` | 손상된 JSON 복구, 개편된 Literature and Brochures 목록의 문서 링크 선택. 공통 압축 헤더 오류도 수정 | 3건 저장, 레지스트리 등록 복구 |
| `directives-doe-gov-directives-` | 빈 설정을 제거하고 기존 DOE Directives Library 전용 수집기 재사용. 기존 식별자 유지 | 3건 저장 |
| `doi-gov-guidance` | `/guidance`가 테스트 문구만 반환. 공식 Departmental Guidance Documents and Portals에서 안내된 기관 목록의 직접 PDF를 수집 | 3건 저장 |
| `doi-gov-performance-reports` | 실제 Performance Reports 주소와 PDF 선택자 설정 | 3건 저장 |
| `justice-gov-guidance` | Guidance Documents 목록의 문서 제목·링크·발행일·기관 선택 및 페이지 넘김 설정 | 3건 저장 |
| `ncha-gov-cn` | 잘못된 CSS뿐 아니라 숨겨진 데이터·페이지 넘김 처리 필요. 기존 NCHA dataproxy 전용 수집기 재사용. 기존 식별자 유지 | 3건 저장 |
| `doi-gov-budget-briefs` | 메뉴의 `Home` 저장을 중단하고 연도별 예산보고서 링크와 상세 PDF를 선택 | FY 2027·2026·2025 보고서 3건 저장 |
| `dol-gov-economicdata` | 누락된 목록 설정 보완. 공식 Statistics 페이지의 직접 PDF로 제한 | HTTP 403, 0건. 선택자 적합성과 실제 수집은 미검증 |

공식 페이지 확인 근거: [NOC 자료 목록](https://www.noc.ac.uk/who-we-are/our-purpose/literature-and-brochures), [DOI 안내문서 포털](https://www.doi.gov/document-library/departmental-guidance-documents-portals), [DOI 성과보고서](https://www.doi.gov/performance/performance-reports), [DOJ 안내문서](https://www.justice.gov/guidance), [DOL 통계 안내](https://www.dol.gov/general/topic/statistics).

공통 코드도 두 곳 수정했다.

- `BaseCrawler`: Brotli 해제기가 없는 환경에서도 `br`을 강제로 요청하던 헤더를 제거했다. 이제 requests가 설치된 디코더에 맞게 제공하는 기본 헤더를 사용한다. 수정 전 NOC 응답은 HTTP 200이지만 압축 바이트가 HTML로 해석되어 0건이었다. 수정 후 3건 저장됐다.
- `GenericCrawler`: 페이지 넘김이 없는 HTML 목록은 한 번만 조회한다. URL 중복을 제거하고, 페이지를 넘겨도 새 항목이 없으면 종료한다. 이전에는 같은 목록·문서를 무한 반복할 수 있었다.

## 검증 및 재현 자료

- 새 회귀 검사와 기존 SQLite 선택·저장 경로·메타데이터 검사 **82개 통과**. 정적 목록 종료, 마지막 페이지 반복, 중복 제거, limit 준수, 압축 헤더, JSON 파싱, 기존 식별자 보존, 메뉴 오수집 방지, 안내 포털과 문서 구분 포함.
- 레지스트리 **1,242 → 1,243개**. Python/JSON 문법 오류 0, 검사 도구가 포착한 import 오류 0.
- 기존 `scienceon-api` 공통 등록 누락 1개와 중복 식별자 18개는 이번 수정 범위 밖이며 남아 있다. 별도 ScienceON 작업 파일을 변경하지 않았다.
- 실제 저장은 운영 이미지 의존성 + 현재 crawler 읽기 전용 마운트, 사이트별 임시 SQLite, 최대 3건·60초 제한으로 확인했다. 운영 DB와 원문 저장소는 마운트하지 않았다.
- 표준 PDF 다운로드는 감사 도구에서 제한한다. 개별 전용 수집기의 자체 PDF 처리까지 모두 막는 검사는 아니며, 전량 원문 다운로드·변환 검증도 아니다.

자료는 `scripts/audit/out/fixes_20260911/`에 있다.

- `tests.log`: 82개 검사 결과
- `registry.json`: 수정 후 등록·구문 검사와 파일 해시
- `live_v1/`: 최초 8개 대상 재검증
- `live_v2/`: 압축 헤더 수정 후 NOC 재검증
- `live_v3/`: 최종 DOL 주소 설정 후 재검증
- `summary.json`, `metadata_samples.csv`: 각 대상의 최종 결과와 저장 표본

최초 전수 감사 `scripts/audit/out/release_20260911/`는 덮어쓰지 않았다.

## 남은 한계와 반영 상태

DOI 안내문서는 공식 포털에서 연결된 기관 페이지의 직접 PDF만 수집한다. 기관별 하위 목록·페이지 넘김까지 모두 수집하는 구현은 아니다. 이번 표본은 DOI 자체 SOL 의견서 목록에서 저장됐다. DOJ 링크는 HTML과 파일이 섞여 있어 일괄 PDF로 표기하지 않았다. 발행일 등 메타데이터의 추가 정규화와 전체 기간 수집은 별도 검증이 필요하다.

DOL은 외부 차단 해소 후 목록의 실제 구조를 확인해야 한다. 앞선 전수 검사에서 시간 초과·0건·차단으로 남은 다른 출처들은 이번 8개 재검증으로 정상 판정하지 않았다.

변경은 현재 `libertree` 작업 디렉터리에 있다. 원격 push나 운영 컨테이너 재빌드·교체는 수행하지 않았다. 실제 공유 중인 납품 브랜치와 현재 체크아웃의 delivery 구성이 다르므로 적용 시 이 변경 파일들을 해당 버전에 반영하고 워커 이미지를 재빌드해야 한다.

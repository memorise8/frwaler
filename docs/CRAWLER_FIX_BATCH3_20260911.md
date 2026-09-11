# 수집기 순차 수정 3차 — 2026-09-11

이전 전수 감사에서 404 또는 0건 종료로 남았던 **BAW·Pasteur·GSI·BFE 4개 출처**를 조사하고 수정했다. 같은 출처의 기존 식별자까지 포함해 7개 등록 수집기에서 실제 저장을 검사했다. 운영 수집 작업이나 운영 컨테이너를 실행·교체한 작업은 아니다.

## 수정과 검증

| 출처 / 등록 식별자 | 원인 및 수정 | 저장 검사 |
|---|---|---|
| BAW / `baw-de`, `baw-de-en` | 옛 목록 HTML은 404지만 SOAP 출판물 API는 유효. 레거시 ID를 해당 전용 구현에 연결. 문서 URL이 옛 404 주소를 가리키던 문제도 수정 | 식별자별 실제 PDF 1건 다운로드, pdftotext 추출, SQLite 저장 |
| Pasteur / `pasteur-fr`, `pasteur-fr-en` | `/en/search-press` 폐기 및 Drupal 화면 개편. 새 목록 주소·기사 카드·본문 섹션·발행일·다음 페이지 선택자 수정 | 식별자별 3건 저장, 별도로 두 번째 목록 페이지까지 검사 |
| GSI / `gsi-ie`, `gsi-ie-en-ie` | SharePoint 주소가 WordPress 개편 후 모두 404. 새 `/publications/` 목록의 실제 문서 항목과 상세 PDF를 선택하도록 교체 | 식별자별 3건 저장 |
| BFE / `bfe-admin-ch` | 폐기된 `mm-test.html` 설정을 새 `/de/medienmitteilungen`의 보도자료 카드와 `/de/newnsb/` 상세로 교체 | 3건 저장 |

기존 등록 식별자는 보존했다. 같은 출처의 식별자별 표본은 서로 중복될 수 있으므로 이 표의 저장 수를 서로 다른 문서 총수로 합산하지 않는다.

현재 공식 목록: [Pasteur 보도자료](https://www.pasteur.fr/en/whats-new/press-area/press-releases-and-press-kits), [GSI 출판물](https://www.gsi.ie/publications/), [BFE 보도자료](https://www.bfe.admin.ch/de/medienmitteilungen). BAW는 `https://www.baw.de/content/v2/publis/ws.php`의 기존 SOAP 응답에서 실제 PDF 주소를 확인했다.

## 데이터 정확성 보완

- BAW: 연차보고서의 연도를 임의의 `YYYY-01-01` 발행일로 저장하지 않는다. 실제 일자를 모르면 발행일은 비우고 보고서 연도는 metadata.year에 보존한다. 저장 URL은 확인된 문서 PDF 주소다.
- GSI: 목록의 `Published 21 July 2026` 형식을 `2026-07-21`로 정규화한다. GenericCrawler에 설정으로 지정한 날짜 형식을 처리하는 기능을 추가했다. 형식이 달라지면 관측 원문을 보존한다.
- Pasteur: 새 목록의 읽기 시간 문구를 날짜에서 제외하고 본문 섹션들을 합친다. 메뉴·푸터 문구를 본문에 포함하지 않는 회귀 검사를 추가했다.

## 검증 환경과 자료

격리 이미지 `libertree-crawler-audit:20260911-fixed`의 의존성을 사용하고, 수정한 crawler 코드를 읽기 전용으로 마운트했다. 각 출처는 별도 임시 SQLite를 사용하며 운영 DB·blob 저장소에 접근하지 않았다.

- 회귀·저장·메타데이터 검사 **86개 통과**.
- HTML 출처는 기본 최대 3건·90초, 동일 호스트 동시 1개로 검사했다.
- BAW는 원문 의존 수집기이므로 별도 검사 스크립트에서 직접 PDF 요청 제한을 해제했다. 식별자별 최대 1건·180초, `/tmp` 512MB, 메모리 1GB의 임시 컨테이너에서 실제 다운로드와 텍스트 추출을 확인했다. PDF 파일은 검사 종료 시 보존하지 않는다.
- 일반 감사 도구가 BAW의 PDF 요청을 막아 발생한 이전 시간 초과를 실제 수집기 고장으로 단정하지 않았다.

로컬 증거 폴더: `scripts/audit/out/fixes_20260911_batch3/`

- `tests.log`: 86개 검사
- `baw_final/`: BAW 두 식별자의 최종 실제 PDF 검사
- `pasteur_live/`: Pasteur 두 식별자의 저장 검사
- `pasteur_pagination/`: Pasteur 11건 목표로 두 번째 목록 페이지 검사
- `final_html/`: GSI 두 식별자와 BFE 최종 검사
- `summary.json`: 최종 출처별 결과와 별도 페이지 넘김 검사

## 남은 범위

BFE는 새 공식 페이지에 노출된 최근 보도자료 목록을 복구했다. 별도 `news.admin.ch` 전체 기록의 페이지 넘김과 별도 출판물 DB 식별자 `bfe-admin-ch-bfe`는 아직 수정하지 않았다. GSI는 현재 단일 목록에 노출된 문서를 순회하며, 상세의 설명이 짧으면 원문 내용을 만들어 채우지 않는다. Pasteur 영어 목록과 프랑스어 전체 자료의 범위는 동일하다고 가정하지 않는다.

NHC의 502, 노동부의 403, 나머지 시간 초과·0건 출처는 이번 결과로 해결됐다고 판정하지 않았다. 전체 PDF의 무결성과 전체 기간 수집 완료도 별도 검증 대상이다. 공유 서버 반영에는 최신 코드로 워커 이미지 재빌드·컨테이너 재생성이 필요하다.

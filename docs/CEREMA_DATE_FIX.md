# Cerema 날짜 결함 수정 — 2026-10-13

실제 사이트·원격 시범 DB를 재검증한 결과가 아니다. 새 워크트리 `frwaler-collector-fixes`, 브랜치 `collector-fixes-20261013`에서만 수정했다. 기준 커밋은 `9e80d315d744e1d2e052f2cf729d2ca059fd66d7`이다.

## 변경

- `crawler/sites/custom/cerema-fr-fr.py`: 프랑스어·영어 12개월의 전체 이름/약어, 대소문자, 악센트, 끝의 점 하나를 처리한다. Jun/Jul을 포함하며 임의 접두어(Juniper 등)는 월로 추측하지 않는다. 명시된 4자리 연도·일을 `datetime.date`로 검증하여 윤년·실재하지 않는 날짜를 거부한다. 알 수 없거나 빠진 날짜는 빈 값으로 유지한다. 수집/저장/식별값 코드는 바꾸지 않았다.
- `crawler/sites/configs/cerema-fr.json`: 목록 날짜 선택자만 `.publication-date`로 지정했다. 상세 날짜 선택자는 null로 유지한다.
- `tests/test_cerema_date_regression.py`: 네트워크/DB 연결을 차단한 합성 입력 회귀 시험 12개. 원래 코드에서 실패를 먼저 확인하고 구현을 수정했다.

## generic 전달 규칙과 BUG-02의 한계

기존 `GenericCrawler._extract_list_items`는 날짜 노드에 `get_text(strip=True)`를 적용하고 `published_date`에 넣는다. `listed_date`는 만들지 않는다. `_fetch_detail`은 상세 날짜 선택자가 null이면 목록 값을 유지한다. `paper_to_document`도 없는 listed_date를 복사해 만들지 않는다. 이 공통 코드는 변경하지 않았다.

|합성 목록 표시|수집기가 전달한 값|정규화 결과|상태|
|---|---|---|---|
|하나의 텍스트 노드 `25 Jun 2026`|`25 Jun 2026`|`2026-06-25`|normalized/day, 원문 보존|
|day/month/year가 각각 span인 `25`·`Jun`·`2026`|`25Jun2026`|NULL|unrecognized, 원문 보존|
|날짜 없음|빈 값|NULL|missing|

**설정 선택자 수정만으로는 분리된 span의 날짜가 정규화된 컬럼까지 채워진다고 보장할 수 없다.** 이제 원문을 읽지만 공통 generic의 공백 없는 결합과 날짜 정규화 규칙 때문에 `25Jun2026`은 미인식으로 남는다. 빈 값을 오늘 날짜로 채우거나 연/월/일을 추측하지 않으며, 이를 ISO 날짜가 복구됐다고 보고하지 않는다. 이를 추가로 해결하려면 generic 추출 또는 공통 정규화에 별도 검토·수정 범위가 필요하다. 이번 지시의 다른 수집기·틀 코드 수정 금지를 지켰다.

커스텀 수집기는 기존대로 파싱한 목록 날짜를 published_date/listed_date에 사용한다. `Publié le 27/06/2026`인 상세와 `25 Jun 2026`인 목록이 다를 때도 목록 값을 임의로 덮어쓰지 않는 시험을 포함했다. 기존 원문 metadata 보존 경로를 확장하거나 저장된 행을 보정하지 않았다.

## 검증과 재현

수정 전 회귀 시험: 12개 실행, subTest 실패 175개와 미지원 None 월 처리 오류 1개. 이는 175개의 독립 시험 메서드가 아니라 alias 행렬의 subTest 실패 수다. 첫 시험 harness의 GenericCrawler read-only 속성 설정 오류를 고친 뒤 얻은 실제 결함 재현 로그는 `.audit/logs/red-regression.log`이다.

수정 후 새 회귀 시험: 12개 통과, 실패/건너뜀 0. `.audit/logs/green.log`.

기존 틀 전체 시험: **189개 실행, 실패 0, 건너뜀 1**(PostgreSQL). 새 날짜 시험 12개와 합쳐 201개 실행/건너뜀 1이다. `.audit/logs/framework.log` 및 `.audit/verification.json`에 기록했다. 기존 테스트의 컨테이너 내부 임시 SQLite fixture만 사용하며 운영·고객·외부 DB에는 접속하지 않는다. 새 날짜 시험 자체는 sqlite3.connect를 차단한다. PostgreSQL DSN은 제공하지 않으며 관련 시험은 기존 방식대로 건너뛴다.

Docker `python:3.12-slim`, 이름 `enum-test-` 접두, `--rm`, 소스 읽기 전용 마운트를 사용했다. 의존성 wheel은 최초 시험 준비 때만 내려받았고, 실제 회귀/틀 시험은 `--network none` 및 `pip --no-index`로 실행했다. 사이트 요청·상세/첨부 다운로드·운영 DB 연결·배포·푸시·병합은 하지 않았다. 전체 시험용 임시 복사에서는 기존 dangling symlink를 따라가지 않고 보존했다.

오프라인 재현 명령(컨테이너 내, 의존성 준비 후):

```sh
python -m unittest tests.test_cerema_date_regression
python -m unittest discover -s tests -p 'test_enumeration_*.py' -t .
```

원격 53/53 및 55/294 누락 행 전체, 실제 저장 백엔드, 현재 사이트 HTML 변동은 확인하지 못했다. 기존 저장 행의 수정/재수집은 별도 범위다.

## 후속 수정 (공통 generic, 1순위)

`crawler/generic_crawler.py`의 목록 날짜(`_extract_list_items`)와 상세 날짜(`_fetch_detail`)가 자식 요소 텍스트를 구분자 없이 이어 `25Jun2026`을 만들던 것을 공백 구분(`get_text(" ", strip=True)`)으로 바꿨다. 전체 시험 중 실제 날짜 읽기 1185건 비교에서 바뀐 것은 cerema 합성 시험 2건(`25Jun2026` -> `25 Jun 2026`)뿐이고, 기존에 정규화되던 날짜 값은 바뀌지 않았다. 위 표의 `25Jun2026` 행은 이제 `2026-06-25`로 정규화된다. 이미 수집된 cerema-fr 행은 재수집해야 갱신된다.

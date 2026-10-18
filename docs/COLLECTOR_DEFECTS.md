# 11 납품 수집기 날짜 결함 배치

**합성 시험 통과이며 실제 사이트·기존 저장 행의 날짜가 복구됐다는 증거가 아니다. 사이트 요청 0건.**

## 선정과 범위

입력 defect-sites.csv(448곳/678행), defect-types.csv(10유형), 01-defect-design.md를 읽었다. 납품 166곳 중 반박된 결함만 있는 곳을 제외한 후보는 153곳이다. 이 대장의 대다수 결함은 주소/범위/상한이며 날짜 누락 자체를 기록한 것으로 바꾸지 않았다. 날짜 문제는 해당 납품 수집기 소스를 추가 분석하고 합성 HTML/구조화 날짜로 재현했다.

한 배치 25곳만 검토: **17곳 날짜 부분 수정, 8곳 확인 필요**. 수정 대상 모두 delivery=True, custom 등록 경로이며 동명 generic config는 없다. 나머지 납품 사이트의 검토·수정은 이번 배치 범위 밖이다. 원래의 다른 결함(범위/주소/상한 등)을 함께 해소한 것으로 세지 않는다. 사이트별 근거·메서드·기존 유형·보류 사유는 COLLECTOR_DEFECTS_SELECTION.csv 참조.

## 수정 내용

- APRA, Archives nationales, Pembina, PNNL, Public Health Scotland, Treasury AU, WTI: 명시된 일/월이름/연도가 HTML 자식 텍스트 결합으로 붙으면 기존 파서가 값을 못 읽는 경우를 복구. 새 source_date_text는 알려진 월 이름의 경계만 나누며 달력 부분이나 월 별칭을 추측하지 않는다. 공개 날짜가 없는 제목의 연도를 날짜로 쓰지 않는다.
- CE Delft, WTI, Datenportal, Science Libraries Canada, Public Service NZ, Earth-prints, CERN, SINTEF, TOI: 연/월만 주어진 원천 값에 01일 또는 1월 1일을 추가하지 않도록 정밀도 보존. SINTEF/TOI의 month 누락+day 존재, bool/소수 날짜 부분은 거부한다.
- Defense FR: 모르는 월을 1월로 대체하지 않음. Public Health Scotland: Juneper 등의 접두어를 June로 추측하지 않음.
- DOI BPP: FY URL의 연도에서 발행일을 1년 빼서 1월 1일로 만들던 fallback 제거. 명시된 dcterms.date는 기존대로 사용.

위 유형은 사이트가 중복되므로 합산하지 않는다. generic_crawler.py와 document_dates.py는 기준 3ab6507과 바이트 단위로 같으며 새로운 generic 변경은 없다. 공통 유틸은 명시적으로 import한 8곳에서만 쓰고 기존 날짜 파서가 월 의미·형식을 판정한다.

## NULL·원문·정밀도

원천 년도/월을 Paper에는 YYYY 또는 YYYY-MM로 유지한다. 공통 normalize_date는 이 원문과 precision=year/month를 보존하면서 DATE 컬럼 value는 **NULL**로 둔다. NULL을 줄이려고 없는 일자를 넣지 않는다. 원천 날짜가 없거나 모호한 값도 NULL이다. 따라서 부분 정밀도 수정 사이트는 DB 일자 컬럼이 채워지는 수정으로 보고하면 안 된다.

기존 수집기 원문 전달·Paper/문서 변환 경로와 post_number 규칙은 변경하지 않았다. 현재 실제 날짜 노드 형태, 원문 누락 행, 기존 DB의 부분 정밀도 상태는 확인 못 했다. 제목·파일명·접근 시각·추정 회계연도로 날짜를 생성하지 않는다.

## 검증

- 기준 전체 틀: 189개, 건너뜀 1.
- 신규 회귀: 사이트별 5개 × 17곳 = 85개. 현재 tests/fixtures/collector_defects/cases.json의 명시된 source grammar를 합성 HTML/time/script 입력으로 시험한다.
- 수정 전: 기준 소스를 임시 컨테이너에 overlay해 동일 최종 시험에서 33개 실패(일부 subTest 포함), 오류 0. 17곳 모두 날짜 회귀 하나 이상 실패했다. 초기 harness 오류 로그는 근거로 쓰지 않고 .audit/logs/red-final.log를 사용한다.
- 수정 후: 전체 **274개 중 273개 통과, 건너뜀 1**, 별도 기존 Cerema 날짜 시험 **13개 전부 통과**. 총 실행 287개/건너뜀 1.
- 17곳의 기존 full date control과 날짜 메서드 밖 전체 소스 fingerprint가 유지됨을 각각 검증했다. 이 fingerprint는 crawl/ID 계산·선택자·전송·저장 코드가 그대로라는 증거이며 모든 실세계 입력 결과를 전수 검증했다는 뜻은 아니다.
- 새 시험은 socket/DNS/subprocess/SQLite/psycopg 연결을 막고 constructor 없이 등록 클래스/순수 함수만 호출한다. 전체 기존 시험의 임시 SQLite와 컨테이너 내부 loopback fixture는 사용했으며 운영/고객/외부 DB 접속은 0이다.

## 확인 필요 8곳

Anses, ASN, Ineris, IRSN, gov.scot statistics, KMA, Barnardos, Lenus. CSV에 각 주소/범위/정렬 문제와 실제 날짜 필드 확인 필요를 기록했다. 새 CSS 선택자·원천 하한·안정 정렬을 코드만으로 추측하지 않았다. 이 보류가 날짜 결함 확인이나 영구 수집 불가 판정이라는 뜻은 아니다.

워크트리 frwaler-collector-defects, 브랜치 collector-defects-20261013, 기준 3ab6507. 구현 커밋 `a47a981435817be21ff9693ce80c06aca6fb0d06`. Docker python:3.12-slim / enum-test-* / --rm --network none / pip --no-index local wheels 사용. 푸시·배포·병합 없음. 실제 날짜 복구량/납품 가능량은 확인 못 함.

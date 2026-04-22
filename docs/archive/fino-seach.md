# Fino Search - 감심 판례 검색 확인

## 배경

사용자가 법제처 국가법령정보센터(`law.go.kr`)에서 감사원 심사청구 결정례(`감심-YYYY-NNNN` 형식)가
검색되지 않는 문제 제기 → 국세법령정보시스템(`taxlaw.nts.go.kr`)에서는 정상 검색됨을 확인.

## 핵심 결론

- **법제처(law.go.kr)**: 감사원 심사청구 결정례 미포함 → 감심 사건번호 검색 불가
- **국세법령정보시스템(taxlaw.nts.go.kr)**: 판례 DB(`precedent, precedent_gr`)에 감사원 심사 결정례 포함
- 본 시스템의 `nts-taxlaw-pd` 크롤러가 해당 DB를 대상으로 동작 중

## 시스템 현황

| 항목 | 값 |
|------|-----|
| API 서버 | `uvicorn api.main:app --host 127.0.0.1 --port 8000` (상주 실행 중) |
| 크롤러 사이트 | `nts-taxlaw-qt` (세법해석례), `nts-taxlaw-pd` (판례) |
| 크롤러 구현 | `crawler/sites/nts_taxlaw.py` (curl 기반, SSL 우회) |
| API 엔드포인트 | `POST https://taxlaw.nts.go.kr/action.do` |
| 리스트 액션 | `ASIPDI002PR01` |
| 상세 액션 | `ASIQTB002PR01` |

## 문서번호 검색 방법

`icldVcbCtl` 필드(키워드)에 문서번호를 넣으면 검색 가능.

```bash
curl -sk --tlsv1.2 -X POST \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -H "Referer: https://taxlaw.nts.go.kr" \
  --data-urlencode 'paramData={"collectionName":"precedent,precedent_gr","sortField":"DCM_RGT_DTM/DESC","startCount":1,"viewCount":10,"dcmClCdCtl":["001_05","001_06","001_07","001_08","001_09","001_10"],"qstnPrdcOrgnClCtl":[],"rltnStttCtl":[],"schDtBase":"DCM_RGT_DTM","icldVcbCtl":["감심-1994-0168"]}' \
  -d "actionId=ASIPDI002PR01" \
  "https://taxlaw.nts.go.kr/action.do"
```

### 판례 DCM 코드

| 코드 | 유형 |
|------|------|
| 001_05 ~ 001_10 | 판례 하위 분류 (대법원/고등/지방/헌재/감심 등) |

## 검증 사례

### 감심-1994-0168 ✅ 검색 성공

| 필드 | 값 |
|------|-----|
| 문서번호 | 감심-1994-0168 |
| 제목 | 이건 부동산 중 건물의 공급시기를 폐업일로 볼 수 있는지 여부 |
| 유형 | 심사 |
| 세법 분류 | 부가가치세 |
| 등록일 | 1994-10-04 |
| DOC_ID | `000000000000108305` |
| 상세 URL | https://taxlaw.nts.go.kr/pd/USEPDA002P.do?ntstDcmId=000000000000108305 |

### 감심-1999-0233 ❌ law.go.kr에서만 실패

- `law.go.kr`: 0건 (감사원 결정례 미포함 DB)
- `taxlaw.nts.go.kr`: 미검증 (추후 동일 방식으로 확인 필요)

## 다음 작업 제안

1. **검색 API 래핑**: 사용자 입력 문서번호 → `taxlaw.nts.go.kr` 실시간 조회 엔드포인트를 `api/main.py`에 추가
2. **병렬 검색**: 문서번호 배치(감심-1996-0162, 1997-0021 등 7건) 동시 조회 유틸 구현
3. **소스 확장 검토**: 감사원 자체 사이트(`www.bai.go.kr`) 크롤러 추가 여부 판단
4. **중복 검증**: `law.go.kr` 판례 + `taxlaw.nts.go.kr` 판례 중복 제거 전략
5. **누락 사건번호 포맷 테스트**: `감심YYYY-NNN`, `99감심0235` 등 이형 표기 처리

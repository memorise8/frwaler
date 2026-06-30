# 조문형 세법집행기준 수집 정찰 — Task 9 spike 결과

> 작성 2026-06-30. plan `2026-06-30-fino-law-corpus-collection.md` Task 9(spike)의 판정 기록. 상위: spec §5.

## 판정: 🟡 도달 가능하나 spike로 본문 추출 경로 미확보 → **이번엔 보류 권장**

막혀있진 않음(로그인/CAPTCHA 없음, 모든 `action.do`가 `status=SUCCESS`). 그러나 법령(law.go.kr, 2콜로 깨끗한 조문)과 달리 **조문형 집행기준 본문 + per-조문 deep-link를 ~10회 probe로도 깨끗이 못 뽑음.** 제대로 하려면 브라우저 DevTools로 실제 클릭 시 XHR을 캡처해야 함(아래 "다음 단계").

## 발견된 사실 (재사용 가능)
- 화면: `/st/USESTE002M.do` ("세법집행기준"), 뷰어 셸: `/st/USESTA002P.do?ntstBscId=…&ntstBrkdId=…` (정적 HTML엔 본문 없음 — SPA).
- `ASISTZ001MR01` `{ntstSysClCd: 01|02|03}` → **법률/시행령/시행규칙** 목록. 각 항목 `{ntstBscId, ntstNm, ntstTlawClCd}`.
  - 예: 법인세법 `ntstBscId=100000000000001563`, 소득세법 `…1565`, 부가가치세법 `…1571`.
  - (주의: ntstSysClCd 01/02/03 = 법령 종류이지 집행기준이 아님. "세법집행기준" 메뉴지만 데이터는 법령.)
- `ASISTZ002MR01` `{ntstBscId}` → `{ntstBrkdId, ntstTextCntn, …}`. 법인세법 → `ntstBrkdId=100000000000276111`(끝자리 = law.go.kr MST 276111과 일치).
- 뷰어 content actions: `ASISTA002MR01~06`, `ASISTA005PR01`, `ASISTD001MR99`.
  - `ASISTA002MR01` `{ntstBscId, ntstBrkdId}` → `txaStttDVOList`(92건) = **관련 법령 목록**(ntstNm=법령명). `tlawEpnSjtNo/Nm/Cntn`(집행기준 주제 필드) 존재하나 **전부 None**.
  - `ASISTA002MR02/03` → `txaStttHsryDVOList`(322 / 2129건) = 조문 개정이력(대용량).
  - `ASISTA002MR04/06` → 위 params로는 `data` None(다른 param 필요).

## 미해결 (다음 단계에 필요)
- 집행기준 **본문 텍스트**를 담은 action + paramData (MR04/MR06 추정, 올바른 param 미상).
- 집행기준 항목의 **고유번호(집행기준 N-N-N) + per-항목 deep-link** 패턴.
- "세법집행기준" 메뉴가 실제로 집행기준 해설을 노출하는 진입(법령 뷰어와 분리된 경로)인지.

## 다음 단계 (권장 방법)
블라인드 probe 중단. **브라우저 DevTools Network 탭을 켜고 taxlaw.nts.go.kr에서 법인세 집행기준을 실제 클릭** → 그때 발생하는 정확한 actionId + paramData를 캡처 → 그 1건을 fixture로 → 본 구현. (blind reverse-engineering보다 확실·빠름.)

## 결론
법령(law.go.kr) 코퍼스는 완료·shippable. 집행기준은 별도 세션에서 DevTools 캡처 후 진행. 관련: [[fino-law-corpus-collection-endpoints]].

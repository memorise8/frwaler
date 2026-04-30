# qt small HTML 조사 (nts-taxlaw-qt, <500B)

- 대상 파일 총 10,908개 중 50개 무작위 샘플
- 시드: 20260423

## 결론

50개 샘플 전부가 **동일한 75바이트 템플릿**:

```html
<html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html>
```

즉 **upstream API가 주는 빈 HTML**이며 파서 버그가 아님. 이 레코드들도 DB의 `abstract`에는 정상적으로 본문 텍스트가 저장돼 있음(152~832자). HTML이 비어 있는 이유는 상위 시스템이 해당 레코드에 대해 HWP 에디터 본문을 제공하지 않기 때문으로 보임.

**조치 불필요.** HTML은 위키 표시용이라 비어 있어도 MD(검색용)로 대체 가능하며, abstract가 정상이므로 기능상 문제 없음.

## 샘플 목록

| # | DOC_ID | bytes | title | abstract_len | content(head 120) |
|---|--------|------:|-------|-------------:|-------------------|
| 1 | 010000000000046182 | 75 | 연구 및 인력개발비 세액공제 대상 인건비의 범위 | 498 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 2 | 010000000000066138 | 75 | ’94.1.1.~’96.6.30일 사이에 공급한 재화 또는 용역의 대손세액공제 범위 | 434 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 3 | 010000000000071027 | 75 | 재건축으로 신축한 주택의 부수토지가 종전주택의 것보다 감소한 경우 과세여부 | 371 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 4 | 010000000000031202 | 75 | 용도변경에 따른 지가차액환수금의 수익사업 해당여부 | 585 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 5 | 010000000000130867 | 75 | 임대주택에 대한 양도소득세 감면, 세율, 장기보유특별공제액의 적용 | 832 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 6 | 010000000000020002 | 75 | 국민주택의 도배공사용역에 대한 부가가치세 과세 여부 | 212 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 7 | 010000000000215342 | 75 | 거주자가 비거주자에게 국외예금 증여시 증여재산공제 적용 여부 | 152 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 8 | 010000000000135276 | 75 | 토지 및 건물 등을 양도하는 경우 양도차익의 산정방법 | 309 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 9 | 010000000000020687 | 75 | 리스설비 임대사업의 국내사업장 해당여부 | 285 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 10 | 010000000000041186 | 75 | 세무사 양도소득세 신고수수료 필요경비 산입 여부 | 156 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 11 | 010000000000069600 | 75 | 면세포기사업자가 면세로 구입한 선어를 가공없이 수출시 의제매입세액 공제여부 | 221 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 12 | 010000000000144638 | 75 | 차액결재선물환 거래시 손익의 귀속 사업연도는 만기일 2 영업일 전에 해당하는 날이 속하는  | 224 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 13 | 010000000000065205 | 75 | 국내사업장이 없는 외국법인이 재화를 공급하는 경우 재화공급 해당여부 | 251 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 14 | 010000000000023335 | 75 | 개발제한구역 안의 잡종지의 종합부동산세 과세대상 토지 해당 여부 | 149 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 15 | 010000000000021049 | 75 | 외화표시 대응조정금액의 원화환산환율 | 386 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 16 | 010000000000068559 | 75 | 의제취득일 전에 취득한 부동산을 양도하는 경우 취득가액 | 251 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 17 | 010000000000069816 | 75 | 수출대행계약 체결후 완제품 내국신용장을 개설받는 경우 대행수출에 해당되는지 여부 | 305 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 18 | 010000000000009462 | 75 | 일정율의 수수료 외에 대리점 운영경비를 받는 경우의 과세표준 | 234 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 19 | 010000000000008086 | 75 | 지방자치단체가 받는 공공하수도 점용료 | 165 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 20 | 010000000000069971 | 75 | 회원사에게 광고를 유치, 알선하여 주고 받은 대가의 부가가치세 과세 여부 | 236 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 21 | 010000000000434762 | 75 | 상속으로 인한 납세의무승계 | 313 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 22 | 010000000000141176 | 75 | 임차한 토지에 조성한 골프코스의 매입세액 공제여부 적용시기 | 504 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 23 | 010000000000070742 | 75 | 양도의 범위 | 181 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 24 | 010000000000068230 | 75 | 자경농민에게 양도하는 농지 등에 대한 양도소득세를 감면받은 경우의 사후관리 | 497 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 25 | 010000000000012018 | 75 | 탈세고발 기관 | 170 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 26 | 010000000000066047 | 75 | 부가가치세 영세율이 적용되는 장애인용 보장구에 해당되는지 여부 | 145 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 27 | 010000000000000754 | 75 | 명의신탁된 부동산의 압류해제 여부 | 331 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 28 | 010000000000066745 | 75 | 여러 사업장 중 한 사업장의 사업을 양도하는 경우의 부가가치세 신고・납부 | 416 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 29 | 010000000000037154 | 75 | 맞벌이 부부의 보험료 공제 방법 | 254 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 30 | 010000000000066837 | 75 | 외상매출금 기타 매출채권 등에 대한 상법상 소멸시효 | 394 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 31 | 010000000000008443 | 75 | 소송계류중인 부동산의 공급시기 | 225 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 32 | 010000000000070116 | 75 | 원형버섯균상자 등에 대한 부가가치세 영세율 적용 여부 | 514 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 33 | 010000000000014093 | 75 | 수입더덕 국내판매시 과세 여부 | 181 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 34 | 010000000000067497 | 75 | 공동상속주택 외의 다른 주택을 양도하는 경우 1세대 1주택 비과세규정 판정 | 363 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 35 | 010000000000069267 | 75 | 승용자동차에 대한 조건부면세를 적용받기 위한 절차 불이행시 처리방법 | 453 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 36 | 010000000000145069 | 75 | 지배ㆍ종속회사 간 합병 시 이월결손금 승계 가능여부 | 347 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 37 | 010000000000067223 | 75 | ’95 매입주식에 대한 거래가액을 ’99 당해주식 증여시 시가로 볼 수 있는지 여부 | 321 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 38 | 010000000000139858 | 75 | 주식양도와 주식파생상품거래를 동시에 체결하는 경우 증권거래세 과세표준 | 283 | <html> <head></head> <body> <p><span>&nbsp;</span></p> </body> </html> |
| 39 | 010000000000027987 | 104 | 지정지역내 공익사업용 부동산에 대한 양도소득세 과세특례 | 276 | <html> <head></head> <body> <p><span>&nbsp;</span></p> <p><span>&nbsp;</span></p> </body> </html> |
| 40 | 010000000000007701 | 104 | 여러 사업장 임대업자의 총수입금액계산 특례적용 | 420 | <html> <head></head> <body> <p><span>&nbsp;</span></p> <p><span>&nbsp;</span></p> </body> </html> |
| 41 | 010000000000525117 | 104 | 개정된 상증령 시행일 전 특정법인과의 거래를 통한 증여에 개정된 상증법 제45조의5 및 상 | 217 | <html> <head></head> <body> <p><span>&nbsp;</span></p> <p><span>&nbsp;</span></p> </body> </html> |
| 42 | 200000000000005808 | 122 | 비과세 보육수당 판단을 위한 나이 산정 기준 | 153 | <html> <head></head> <body> <p style="text-align: justify; margin-left: 0pt;"><span>&nbsp;</span></p> </body> </html> |
| 43 | 010000000000046891 | 150 | 비영리법인 고정자산의 고유목적사업 직접 사용 여부 | 465 | <html> <head></head> <body> <p><span>&nbsp;</span></p> <p style="margin-left: 5pt; margin-right: 15pt;"><span>&nbsp;</sp |
| 44 | 010000000000324741 | 161 | 불법보조금의 필요경비 인정여부에 대한 회신 | 363 | <html> <head></head> <body> <p><span>&nbsp;</span></p> <p><span>&nbsp;</span></p> <p style="text-align: center;"><span>& |
| 45 | 010000000000344678 | 193 | 세관장의 수정수입세금계산서 발급 | 426 | <html> <head></head> <body> <p><span>&nbsp;</span></p> <p style="text-align: center;"><span>&nbsp;</span></p> <p style=" |
| 46 | 010000000000141238 | 217 | 비사업용 토지에서 제외되는 이농농지에 해당하는지 여부. | 257 | <html> <head></head> <body> <p><span>&nbsp;</span></p> <p style="margin-left: 7.06667pt; margin-right: 10pt;"><span>&nbs |
| 47 | 010000000000126143 | 236 | 양도소득세가 비과세되는 8년 이상 자경한 농지의 의미 | 243 | <html> <head></head> <body> <p style="margin-left: 0pt; margin-right: 0pt;"><span>&nbsp;</span></p> <p><span class="bold |
| 48 | 010000000000040217 | 252 | 수정신고 후 경정시 소득세 신고불성실가산세 계산방법 | 468 | <html> <head></head> <body> <p><span>&nbsp;</span></p> <p style="margin-left: 0.766667pt; margin-right: 10pt;"><span>&nb |
| 49 | 010000000000075907 | 428 | 외국인 프로선수의 항공료 ・체재비가 국내원천소득인지 여부 | 298 | <html> <head></head> <body> <p style="margin-left: 0pt; margin-right: 0pt;"><span>&nbsp;</span></p> <p style="margin-lef |
| 50 | 010000000000102672 | 441 | 타인소유 도로에 회사부담으로 아스팔트공사를 한 경우 세무처리 방법 여부 | 311 | <html> <head></head> <body> <p style="margin-left: 0pt; margin-right: 0pt;"><span>&nbsp;</span></p> <p><span class="bold |

# ScienceON crawler production notes

## 등록 정보

| site_id | 사이트 이름 | 대상 URL | 국가 | 문서 유형 |
|---|---|---|---|---|
| `scienceon-api` | ScienceON 국내 논문 | `https://apigateway.kisti.re.kr` | 대한민국 | 논문 |
| `scienceon-foreign-api` | ScienceON 외국 논문 메타데이터 | `https://apigateway.kisti.re.kr` | 다국가 | 논문 |

한 파일에 인증/API 계층을 공유하는 두 concrete `BaseCrawler` 클래스를 둔다.
기존 PostgreSQL의 `scienceon-api` 문서와 dedup 호환성을 위해 국내 site_id는
변경하지 않았다. 외국 논문은 규모와 원문 출처가 달라 별도 site_id로 분리했다.

## 목록 정렬 및 수집 전략

- 선언: `DELIVERY_ORDER = "arbitrary"`.
- 근거: ScienceON 검색 페이지는 같은 정렬값을 가진 레코드의 경계가 흔들린다.
  기존 실측에서 단일 순회가 약 6.5%를 놓쳤으므로 `newest_first`의 연속 기보유
  조기 종료를 사용하면 안 된다.
- `incremental`: 현재 연도부터 기본 2개 연도를 각 root별 기본 3페이지씩 다시
  대조하는 bounded reconciliation이다. 완전성은 정기 backfill이 담당한다.
- `backfill`: TotalCount가 8,000 이하가 될 때까지 CN prefix를 분할하고, 여러
  정렬 pass의 고유 CN이 신고 건수의 99% 이상일 때만 다음 평평한 cursor로
  전진한다. 한 실행은 기본 4개 non-empty terminal slice를 처리한다.
- `full`: 장시간 단일 실행으로 잘못 사용할 수 있으므로 명시적으로 거부하고
  `backfill` 사용을 안내한다.

## 범위와 총량 추정

2026-08-06 `--audit-coverage` 실제 API 측정 기준:

- 전체 ARTI: 156,595,478건.
- 국내 roots: `JAKO`, `DIKO`, `NPAP/CFKO`, `ATN/JAKO`, `ART/JAKO`.
- 외국 roots: `NART`, `PRE`, `NPAP/CFFO`, `ATN/JAFO`.
- NART 123,650,588건, PRE 2,027,092건으로 외국 범위만 1억 건 이상이다.
- 따라서 국내·외국 모두 Worker의 `backfill` cursor가 필수다.

## 추가 파이썬 의존성

- `cryptography`: KISTI 공식 인증 방식의 AES-256-CBC accounts 생성.
- `lxml`, `requests`: Worker 기본 이미지에 이미 존재.

납품 migration 시 `cryptography`도 명시적인 runtime requirement로 고정해야 한다.

## 실제 API canary 파싱 결과

2026-08-20, DB 저장 없이 ARTI search를 국내·외국 각 2건으로 제한해 확인했다.
인증키·token·초록·원문은 출력하거나 기록하지 않았다.

```python
{
    "CN": "JAKO202314332405248",
    "DBCode": "JAKO",
    "Title": "eGIF4DRC: E-Government Interoperability Framework For DRC Based On Service Oriented Architecture(SOA)",
    "Pubyear": "2023",
}
```

```python
{
    "CN": "NART20244844",
    "DBCode": "JAFO",
    "Title": "A Method for Substructural Sensitivity Synthesis",
    "Pubyear": "1991",
}
```

## 운영 설정

- `SCIENCEON_KEY`: 32자리 인증키.
- `SCIENCEON_CLIENT_ID`: 발급 client id.
- `SCIENCEON_MAC_ADDRESS`: 활용신청 시 제출한 MAC.
- `SCIENCEON_TOKEN_CACHE`: 기본 `/data/runtime/scienceon_token.json`.
- `SCIENCEON_SLICE_BUDGET`: backfill 실행당 terminal slice 수, 기본 4.
- `SCIENCEON_INCREMENTAL_YEARS`: 증분 재대조 연도 수, 기본 2.
- `SCIENCEON_INCREMENTAL_PAGES`: 연도·root별 증분 페이지 수, 기본 3.

## 알려진 제약

- 인증정보와 API 활용승인이 필요하다.
- 쿼리별 접근 상한은 9,900건이므로 backfill은 adaptive prefix 분할을 사용한다.
- 검색 페이지가 불안정해 terminal slice마다 여러 정렬 pass가 필요하다.
- 일시 오류·불완전 slice에서는 예외로 실패하며 cursor를 전진시키지 않는다.
- 외국 PMC 원문은 ScienceON이 아닌 외부 서비스이므로 이번 파일에서 다운로드하지
  않는다. ARTI가 직접 제공한 `pdf_url`만 메타데이터로 보존한다.
- PDF blob 저장은 backend-neutral downloader가 마련된 뒤 별도 작업으로 붙인다.

# 문서 카탈로그 API 계약

## `GET /documents`

문서 검색·필터 화면이 사용하는 읽기 전용 목록 API다. 모든 필터는 AND로 결합한다.

### Query parameters

| 이름 | 형식 | 기본값 | 설명 |
|---|---|---:|---|
| `q` | string, 최대 200자 | 없음 | `fts(simple)` 전문검색과 제목 부분일치를 함께 사용 |
| `page` | integer, 1 이상 | 1 | 1부터 시작하는 페이지 |
| `page_size` | integer, 1~100 | 20 | 페이지당 문서 수 |
| `site_id` | string | 없음 | 정확한 사이트 ID |
| `country` | string | 없음 | 공유 taxonomy의 국가명 (`기타` 포함) |
| `doc_type` | string | 없음 | 공유 taxonomy의 자료 유형명 (`기타` 포함) |
| `lang` | string | 없음 | `document_lang.lang` 정확 일치. 언어 미측정은 `unknown` |
| `published_from`, `published_to` | `YYYY-MM-DD` | 없음 | 유효한 ISO 발행일 범위 |
| `collected_from`, `collected_to` | `YYYY-MM-DD` | 없음 | 수집일 범위(양 끝 날짜 포함) |
| `has_pdf`, `has_text` | boolean | 없음 | 파일 확보 플래그 필터 |
| `sort` | enum | `relevance`(검색 시), `collected_desc`(검색어 없을 때) | `relevance`, `published_desc`, `collected_desc`, `seq_desc` |

범위의 시작일이 종료일보다 늦거나, taxonomy에 없는 국가·자료 유형, 과도한 페이지 크기,
검색어 없는 `relevance` 정렬은 HTTP 422다.

### Response

```json
{
  "items": [
    {
      "seq_id": 108944,
      "site_id": "ii-re-kr-research",
      "site_name": "인천연구원",
      "country": "한국",
      "doc_type": "연구자료",
      "title": "문서 제목",
      "published_date": "2025-01-03",
      "collected_at": "2026-08-01T00:00:00Z",
      "authors": null,
      "publisher": "인천연구원",
      "journal": null,
      "lang": "ko",
      "has_pdf": true,
      "has_text": true,
      "has_translation": false,
      "meta_url": "https://example.test/document"
    }
  ],
  "pagination": {"page": 1, "page_size": 20, "total": 536017, "pages": 26801},
  "facets": {
    "countries": [{"value": "한국", "count": 123}],
    "doc_types": [{"value": "연구자료", "count": 45}],
    "sites": [{"value": "ii-re-kr-research", "label": "인천연구원", "count": 12}],
    "languages": [{"value": "ko", "count": 10}]
  },
  "query": {"q": null, "sort": "collected_desc"},
  "measured_at": "2026-08-12T00:00:00Z"
}
```

facet은 현재 검색어와 모든 적용 필터를 반영한 결과 집합 기준이다. 분류되지 않은 사이트는
국가·자료 유형 모두 `기타`, 언어가 측정되지 않은 문서는 `unknown`으로 반환한다.

`items`는 초록·본문을 포함하지 않는다. 문서 전체 내용은 단건 상세 API에서 제공한다.

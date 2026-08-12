# 문서 상세 API 계약

## `GET /documents/{seq_id}`

저장된 문서와 완료된 번역을 읽기 전용으로 반환한다. 이 요청은 번역 작업을 생성하거나 외부 API를 호출하지 않는다.

```json
{
  "seq_id": 108944,
  "site": {"site_id": "example", "site_name": "Example", "site_url": "https://example.test", "sheet": "한국완료"},
  "classification": {"country": "한국", "doc_type": "연구자료"},
  "source": {
    "lang": "en", "title": "Original title", "abstract": "Original abstract",
    "authors": null, "publisher": "Publisher", "journal": null, "keywords": null,
    "published_date": "2025-01-01", "listed_date": null, "collected_at": "2026-08-01T00:00:00Z",
    "summary": null, "summary_model": null, "meta_url": "https://example.test/1", "pdf_url": null
  },
  "translations": {
    "target_locale": "ko-KR",
    "title": {"text": "번역 제목", "model_version": "model", "prompt_version": "v1", "completed_at": "2026-08-02T00:00:00Z"},
    "description": null
  },
  "files": {"has_pdf": true, "has_text": true, "pdf_size_bytes": 1234, "original_filename": "a.pdf"}
}
```

동일 필드의 완료 번역이 여러 개이면 `completed_at`, `updated_at`, `translation_id` 순으로 가장 최신 결과를 선택한다.
문서가 없으면 404, 0 이하 ID는 FastAPI validation에 따라 422다.

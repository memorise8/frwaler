"""Read-only document detail and stored translation projection."""
from __future__ import annotations

from delivery.be.catalogue import load_taxonomy


def collect_document_detail(conn, seq_id: int, taxonomy: dict | None = None) -> dict | None:
    taxonomy = taxonomy if taxonomy is not None else load_taxonomy()
    row = conn.execute(
        """
        SELECT d.*, s.site_name, s.site_url, s.sheet, COALESCE(dl.lang, 'unknown') AS lang
        FROM documents d JOIN sites s USING (site_id)
        LEFT JOIN document_lang dl USING (seq_id)
        WHERE d.seq_id=%s
        """,
        (seq_id,),
    ).fetchone()
    if row is None:
        return None
    translations = conn.execute(
        """
        SELECT DISTINCT ON (source_field) source_field, translation_text, model_version,
               prompt_version, completed_at
        FROM document_translations
        WHERE seq_id=%s AND target_locale='ko-KR' AND state='completed'
          AND source_field IN ('title', 'description')
        ORDER BY source_field, completed_at DESC NULLS LAST, updated_at DESC, translation_id DESC
        """,
        (seq_id,),
    ).fetchall()
    translated = {
        item["source_field"]: {
            "text": item["translation_text"], "model_version": item["model_version"],
            "prompt_version": item["prompt_version"], "completed_at": item["completed_at"],
        }
        for item in translations
    }
    summary = conn.execute(
        """SELECT summary_text,key_points,institutions,source_facts,model_version,prompt_version,completed_at
           FROM document_summaries WHERE seq_id=%s AND target_locale='ko-KR' AND state='completed'
           ORDER BY completed_at DESC NULLS LAST,updated_at DESC,summary_id DESC LIMIT 1""",
        (seq_id,),
    ).fetchone()
    classification = taxonomy.get(row["site_id"], {"country": "기타", "doc_type": "기타"})
    return {
        "seq_id": row["seq_id"],
        "site": {key: row[key] for key in ("site_id", "site_name", "site_url", "sheet")},
        "classification": classification,
        "source": {
            key: row[key] for key in (
                "lang", "title", "abstract", "authors", "publisher", "journal", "keywords",
                "published_date", "listed_date", "collected_at", "summary", "summary_model",
                "meta_url", "pdf_url",
            )
        },
        "translations": {
            "target_locale": "ko-KR", "title": translated.get("title"),
            "description": translated.get("description"),
        },
        "generated_summary": dict(summary) if summary else None,
        "files": {
            "has_pdf": bool(row["pdf_downloaded"]), "has_text": bool(row["text_extracted"]),
            "pdf_size_bytes": row["pdf_size_bytes"], "original_filename": row["original_filename"],
        },
    }

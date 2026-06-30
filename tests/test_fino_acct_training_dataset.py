from pathlib import Path
import json

from crawler.fino_acct.training_dataset import (
    chunk_document,
    load_generic_paper_documents,
    load_kicpa_documents,
    load_public_markdown_documents,
    make_training_pairs,
)
from crawler.fino_acct.training_sources import (
    strip_front_matter,
)


def test_strip_front_matter_when_markdown_has_yaml_header() -> None:
    markdown = "---\ntitle: sample\n---\n\n본문입니다.\n"

    text = strip_front_matter(markdown)

    assert text == "본문입니다."


def test_chunk_document_when_text_is_long_enough() -> None:
    chunks = chunk_document(
        source_id="doc-1",
        source_key="kicpa:ifrs_cases",
        title="수익 인식 사례",
        text="문단 하나입니다. " * 80,
        max_chars=300,
        min_chars=80,
    )

    assert chunks
    assert chunks[0].chunk_id == "doc-1:0001"
    assert chunks[0].source_key == "kicpa:ifrs_cases"


def test_load_public_markdown_documents_when_manifest_points_to_file(tmp_path: Path) -> None:
    markdown_path = tmp_path / "doc.md"
    markdown_path.write_text("---\ntitle: 공시\n---\n\n공시 본문 " * 80, encoding="utf-8")
    manifest_path = tmp_path / "manifest.jsonl"
    manifest_path.write_text(
        json.dumps(
            {
                "attachment_id": "7",
                "filename": "공시.pdf",
                "agency": "금융감독원",
                "target_name": "감독행정",
                "source_type": "supervision",
                "source_subtype": "FSS",
                "detail_url": "https://example.test/detail",
                "markdown_path": str(markdown_path),
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    documents, events = load_public_markdown_documents(manifest_path)

    assert len(documents) == 1
    assert documents[0].title == "공시.pdf"
    assert events[0].status == "loaded"


def test_load_kicpa_documents_when_manifest_has_attachment(tmp_path: Path) -> None:
    root = tmp_path / "kicpa"
    attachment = root / "attachments" / "ifrs" / "1" / "sample.txt"
    attachment.parent.mkdir(parents=True)
    attachment.write_text("KICPA 본문 " * 120, encoding="utf-8")
    manifest_path = root / "manifest.jsonl"
    manifest_path.write_text(
        json.dumps(
            {
                "source_key": "ifrs_cases",
                "source_name": "IFRS실무사례",
                "board_id": "accstd02",
                "bltn_no": "1",
                "title": "수익 인식",
                "attachments": [{"label": "sample.txt", "path": str(attachment.relative_to(tmp_path))}],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    documents, events = load_kicpa_documents(root, workspace_root=tmp_path)

    assert len(documents) == 1
    assert documents[0].source_key == "kicpa:ifrs_cases"
    assert events[0].status == "loaded"


def test_load_generic_paper_documents_when_db_has_unrelated_sites(tmp_path: Path) -> None:
    import sqlite3

    db_path = tmp_path / "papers.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE papers (id TEXT, site_id TEXT, title TEXT, abstract TEXT, url TEXT, category TEXT, crawled_at TEXT)"
        )
        conn.execute(
            "INSERT INTO papers VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("1", "arxiv-astro-ph-2026-01", "외부", "외부 본문 " * 100, "https://example.test/a", "misc", "2026"),
        )
        conn.execute(
            "INSERT INTO papers VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "2",
                "better-fsc-go-kr-fsc_new",
                "비조치",
                "질의요지와 회답 본문 " * 100,
                "https://better.fsc.go.kr/detail",
                "비조치의견서",
                "2026",
            ),
        )

    documents, events = load_generic_paper_documents(db_path)

    assert len(documents) == 1
    assert documents[0].source_key == "public:better-fsc-go-kr-fsc_new"
    assert len(events) == 1


def test_make_training_pairs_when_chunks_have_same_domain_negative() -> None:
    chunks = []
    for index in range(3):
        chunks.extend(
            chunk_document(
                source_id=f"doc-{index}",
                source_key="kicpa:audit_review_cases",
                title=f"감리지적 사례 {index}",
                text=f"감리지적 사례 {index} 본문 " * 100,
                max_chars=500,
                min_chars=120,
            )
        )

    pairs = make_training_pairs(chunks)

    assert pairs
    assert pairs[0].hard_negatives
    assert pairs[0].positive_chunk_id != pairs[0].hard_negative_chunk_id

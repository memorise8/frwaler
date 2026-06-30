from pathlib import Path
import json

from crawler.fino_acct.curate_markdown import curate_markdown, parse_markdown_document, quality_tier_for


def test_parse_markdown_document_reads_front_matter_and_body(tmp_path: Path) -> None:
    path = tmp_path / "doc.md"
    markdown = "\n".join(
        [
            "---",
            "title: 수익 인식",
            "agency: 금융감독원(FSS)",
            "source_priority: 3",
            "source_type: qna",
            "---",
            "",
            "회계 질의 본문입니다.",
        ]
    )
    _ = path.write_text(markdown, encoding="utf-8")

    document = parse_markdown_document(path)

    assert document.title == "수익 인식"
    assert document.metadata["source_priority"] == "3"
    assert document.body == "회계 질의 본문입니다."


def test_quality_tier_for_marks_priority_three_as_include() -> None:
    tier = quality_tier_for(source_priority="3", body="회계 질의 본문입니다. " * 20, title="GAAP 질의")

    assert tier == "include"


def test_curate_markdown_writes_manifests_without_touching_raw_input(tmp_path: Path) -> None:
    input_dir = tmp_path / "raw"
    docs_dir = input_dir / "documents" / "03"
    docs_dir.mkdir(parents=True)
    source = docs_dir / "1_GAAP.md"
    markdown = "\n".join(
        [
            "---",
            "title: GAAP 질의",
            "agency: 금융감독원(FSS)",
            "source_priority: 3",
            "source_type: qna",
            "detail_url: https://example.test/gaap",
            "---",
            "",
            "일반기업회계기준 질의 본문입니다. " * 30,
        ]
    )
    _ = source.write_text(markdown, encoding="utf-8")
    source_hash_before = source.read_bytes()
    output_root = tmp_path / "curated"

    summary = curate_markdown(input_dir=input_dir, output_root=output_root, run_id="qa-run")

    assert summary["raw_documents"] == 1
    assert summary["include"] == 1
    assert source.read_bytes() == source_hash_before
    manifest = output_root / "qa-run" / "curated_manifest.jsonl"
    rows = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["quality_tier"] == "include"
    assert Path(rows[0]["curated_markdown_path"]).exists()


def test_curate_markdown_records_duplicate_alias(tmp_path: Path) -> None:
    input_dir = tmp_path / "raw"
    docs_dir = input_dir / "documents" / "05"
    docs_dir.mkdir(parents=True)
    body = "감리지적 사례 본문입니다. " * 30
    for name in ["1_case.md", "2_case.md"]:
        markdown = "\n".join(
            [
                "---",
                f"title: {name}",
                "source_priority: 5",
                "source_type: supervisory_case",
                "---",
                "",
                body,
            ]
        )
        _ = (docs_dir / name).write_text(markdown, encoding="utf-8")

    summary = curate_markdown(input_dir=input_dir, output_root=tmp_path / "curated", run_id="dup-run")

    assert summary["secondary"] == 1
    assert summary["duplicate_aliases"] == 1
    alias_path = tmp_path / "curated" / "dup-run" / "duplicate_aliases.jsonl"
    aliases = [json.loads(line) for line in alias_path.read_text(encoding="utf-8").splitlines()]
    assert aliases[0]["quality_tier"] == "duplicate_alias"

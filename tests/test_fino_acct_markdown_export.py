from pathlib import Path
import zipfile

from crawler.fino_acct.export_markdown import build_markdown, decode_hwp_section_text, extract_text_docx, extract_text_hwpx, safe_markdown_name


def test_extract_text_hwpx_when_section_xml_contains_text(tmp_path: Path) -> None:
    path = tmp_path / "sample.hwpx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Contents/section0.xml", "<root><p><t>첫 문장</t><t>둘째 문장</t></p></root>")

    text = extract_text_hwpx(path)

    assert text == "첫 문장\n둘째 문장"


def test_extract_text_docx_when_document_xml_contains_text(tmp_path: Path) -> None:
    path = tmp_path / "sample.docx"
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>회계</w:t></w:r><w:r><w:t>감리</w:t></w:r></w:p></w:body>"
        "</w:document>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)

    text = extract_text_docx(path)

    assert text == "회계\n감리"


def test_build_markdown_includes_metadata_and_text() -> None:
    markdown = build_markdown(
        title="회계기준 적용 감독지침",
        metadata={"agency": "금융위원회", "source_type": "policy"},
        text="본문입니다.",
    )

    assert markdown.startswith("---\n")
    assert "title: 회계기준 적용 감독지침" in markdown
    assert "agency: 금융위원회" in markdown
    assert markdown.endswith("본문입니다.\n")


def test_safe_markdown_name_limits_bytes_and_uses_md_suffix() -> None:
    name = safe_markdown_name(123, "회계법인 품질관리 감리 결과 개선권고사항" * 20)

    assert len(name.encode("utf-8")) <= 240
    assert name.endswith(".md")


def test_decode_hwp_section_text_when_para_text_record_exists() -> None:
    payload = "회계 질의".encode("utf-16le")
    header = (67 | (len(payload) << 20)).to_bytes(4, "little")

    text = decode_hwp_section_text(header + payload)

    assert text == "회계 질의"

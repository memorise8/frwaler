from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
import zlib

from .fetch import safe_filename


DEFAULT_DB_PATH = Path("data/fino_acct.db")
DEFAULT_OUTPUT_DIR = Path("/data_raid/ruci_workspace/acct_rag_data")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m crawler.fino_acct.export_markdown")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--limit", type=int, default=0)
    return parser


def extract_text_hwpx(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            names = sorted(name for name in archive.namelist() if "section" in name.lower() and name.endswith(".xml"))
            return "\n".join(_xml_texts(archive.read(name)) for name in names).strip()
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        return ""


def extract_text_docx(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            return _xml_texts(archive.read("word/document.xml")).strip()
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        return ""


def extract_text_hwp(path: Path) -> str:
    try:
        import olefile
    except ImportError:
        return ""
    if not olefile.isOleFile(str(path)):
        return ""
    ole = olefile.OleFileIO(str(path))
    try:
        if ole.exists("PrvText"):
            data = ole.openstream("PrvText").read()
            return data.decode("utf-16-le", errors="ignore").strip("\x00").strip()
        return extract_hwp_body_text(ole)
    finally:
        ole.close()


def extract_hwp_body_text(ole) -> str:
    header = ole.openstream("FileHeader").read() if ole.exists("FileHeader") else b""
    compressed = bool(header[36] & 1) if len(header) > 36 else True
    parts: list[str] = []
    for stream_parts in ole.listdir():
        name = "/".join(stream_parts)
        if not name.startswith("BodyText/Section"):
            continue
        data = ole.openstream(name).read()
        if compressed:
            try:
                data = zlib.decompress(data, -15)
            except zlib.error:
                continue
        text = decode_hwp_section_text(data)
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def decode_hwp_section_text(data: bytes) -> str:
    position = 0
    output: list[str] = []
    while position + 4 <= len(data):
        header = struct.unpack_from("<I", data, position)[0]
        position += 4
        tag = header & 0x3FF
        size = (header >> 20) & 0xFFF
        if size == 0xFFF:
            if position + 4 > len(data):
                break
            size = struct.unpack_from("<I", data, position)[0]
            position += 4
        payload = data[position : position + size]
        position += size
        if tag == 67:
            output.append(clean_hwp_text(payload.decode("utf-16le", errors="ignore")))
    return "\n".join(part for part in output if part).strip()


def clean_hwp_text(text: str) -> str:
    chars = [char if char >= " " or char in "\n\t" else " " for char in text]
    lines = [" ".join(line.split()) for line in "".join(chars).splitlines()]
    return "\n".join(line for line in lines if line)


def extract_text_pdf(path: Path) -> str:
    if shutil.which("pdftotext") is None:
        return ""
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = Path(tmp_dir) / "out.txt"
        result = subprocess.run(
            ["pdftotext", "-layout", str(path), str(out_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not out_path.exists():
            return ""
        return out_path.read_text(encoding="utf-8", errors="ignore").strip()


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    match suffix:
        case ".hwpx":
            return extract_text_hwpx(path)
        case ".hwp":
            return extract_text_hwp(path)
        case ".pdf":
            return extract_text_pdf(path)
        case ".docx":
            return extract_text_docx(path)
        case _:
            return ""


def _xml_texts(data: bytes) -> str:
    root = ET.fromstring(data)
    texts: list[str] = []
    for element in root.iter():
        if element.text and element.tag.rsplit("}", 1)[-1] == "t":
            texts.append(element.text)
    return "\n".join(texts)


def build_markdown(*, title: str, metadata: dict[str, str], text: str) -> str:
    lines = ["---", f"title: {yaml_scalar(title)}"]
    for key, value in metadata.items():
        if value:
            lines.append(f"{key}: {yaml_scalar(value)}")
    lines.extend(["---", "", text.strip(), ""])
    return "\n".join(lines)


def yaml_scalar(value: str) -> str:
    cleaned = value.replace("\r", " ").replace("\n", " ").strip()
    if not cleaned:
        return '""'
    if any(char in cleaned for char in [":", "#", "|", "\"", "'"]):
        return json.dumps(cleaned, ensure_ascii=False)
    return cleaned


def safe_markdown_name(attachment_id: int, title: str) -> str:
    stem = safe_filename(f"{attachment_id}_{title}")
    if stem.lower().endswith(".md"):
        return stem
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    return safe_filename(f"{stem}.md")


def export_markdown(db_path: Path, output_dir: Path, limit: int = 0) -> tuple[int, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    documents_dir = output_dir / "documents"
    documents_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "_manifest.jsonl"
    failures_path = output_dir / "_failures.jsonl"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    query = attachment_query(limit)
    rows = conn.execute(query).fetchall()
    converted = 0
    failed = 0
    with manifest_path.open("w", encoding="utf-8") as manifest, failures_path.open("w", encoding="utf-8") as failures:
        for row in rows:
            source_path = Path(row["local_path"])
            text = extract_text(source_path)
            if not text:
                failures.write(json.dumps(row_to_event(row, "extract_failed", ""), ensure_ascii=False) + "\n")
                failed += 1
                continue
            target_dir = documents_dir / f"{int(row['source_priority']):02d}"
            target_dir.mkdir(parents=True, exist_ok=True)
            output_path = target_dir / safe_markdown_name(int(row["attachment_id"]), row["filename"])
            markdown = build_markdown(title=row["filename"], metadata=row_to_metadata(row), text=text)
            output_path.write_text(markdown, encoding="utf-8")
            manifest.write(json.dumps(row_to_event(row, "converted", str(output_path)), ensure_ascii=False) + "\n")
            converted += 1
    conn.close()
    write_readme(output_dir, converted, failed)
    return converted, failed


def attachment_query(limit: int) -> str:
    base = "SELECT a.id AS attachment_id, a.filename, a.local_path, a.url AS attachment_url, d.id AS document_id, d.agency, d.target_name, d.source_priority, d.source_url, d.source_type, d.source_subtype, d.index_name, d.title AS source_title, d.detail_url FROM acct_attachments a JOIN acct_documents d ON d.id = a.document_id WHERE a.status = 'downloaded' AND a.local_path != '' ORDER BY d.source_priority, a.id"
    if limit > 0:
        return f"{base} LIMIT {limit}"
    return base


def row_to_metadata(row: sqlite3.Row) -> dict[str, str]:
    return {
        "agency": row["agency"],
        "target_name": row["target_name"],
        "source_priority": str(row["source_priority"]),
        "source_type": row["source_type"],
        "source_subtype": row["source_subtype"],
        "index_name": row["index_name"],
        "source_title": row["source_title"],
        "detail_url": row["detail_url"],
        "attachment_url": row["attachment_url"],
        "attachment_id": str(row["attachment_id"]),
        "document_id": str(row["document_id"]),
    }


def row_to_event(row: sqlite3.Row, status: str, markdown_path: str) -> dict[str, str]:
    event = row_to_metadata(row)
    event.update({"status": status, "filename": row["filename"], "local_path": row["local_path"], "markdown_path": markdown_path})
    return event


def write_readme(output_dir: Path, converted: int, failed: int) -> None:
    text = f"# Accounting RAG Markdown\n\n- converted: {converted}\n- failed: {failed}\n\nGenerated from `data/fino_acct.db` and `data/fino_acct_docs`.\n"
    (output_dir / "README.md").write_text(text, encoding="utf-8")


def main() -> int:
    args = build_parser().parse_args()
    converted, failed = export_markdown(args.db_path, args.output_dir, args.limit)
    print(f"converted={converted} failed={failed} output_dir={args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

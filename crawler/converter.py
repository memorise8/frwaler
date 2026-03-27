# -*- coding: utf-8 -*-
"""Convert downloaded HWP/HWPX/PDF files to Markdown."""

import os
import zipfile

from bs4 import BeautifulSoup


def extract_text_hwp(filepath):
    """Extract text from HWP (binary) via PrvText OLE stream."""
    import olefile

    if not olefile.isOleFile(filepath):
        return None
    ole = olefile.OleFileIO(filepath)
    try:
        if not ole.exists("PrvText"):
            return None
        data = ole.openstream("PrvText").read()
        return data.decode("utf-16-le", errors="ignore").strip("\x00").strip()
    finally:
        ole.close()


def extract_text_hwpx(filepath):
    """Extract text from HWPX (ZIP/XML) via section XML files."""
    try:
        zf = zipfile.ZipFile(filepath)
    except (zipfile.BadZipFile, Exception):
        return None

    sections = sorted(
        [n for n in zf.namelist() if "section" in n.lower() and n.endswith(".xml")]
    )
    if not sections:
        return None

    parts = []
    for sec in sections:
        xml = zf.read(sec)
        soup = BeautifulSoup(xml, "xml")
        for t in soup.find_all("t"):
            if t.string:
                parts.append(t.string)
    zf.close()
    return "\n".join(parts) if parts else None


def extract_text_pdf(filepath):
    """Extract text from PDF using pdfplumber (if available) or fallback."""
    try:
        import pdfplumber

        with pdfplumber.open(filepath) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
        return "\n\n".join(pages).strip()
    except ImportError:
        pass

    try:
        from PyPDF2 import PdfReader

        reader = PdfReader(filepath)
        pages = [p.extract_text() or "" for p in reader.pages]
        return "\n\n".join(pages).strip()
    except ImportError:
        return None


def text_to_markdown(text, title="", metadata=None):
    """Format extracted text as Markdown."""
    lines = []
    if title:
        lines.append(f"# {title}")
        lines.append("")
    if metadata:
        for key, val in metadata.items():
            if val:
                lines.append(f"- **{key}**: {val}")
        lines.append("")
        lines.append("---")
        lines.append("")
    lines.append(text)
    return "\n".join(lines)


def convert_file(filepath):
    """Extract text from a file based on its extension. Returns text or None."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".hwpx":
        return extract_text_hwpx(filepath)
    elif ext == ".hwp":
        return extract_text_hwp(filepath)
    elif ext == ".pdf":
        return extract_text_pdf(filepath)
    else:
        # Try HWP binary for extensionless files
        return extract_text_hwp(filepath)


def convert_site_files(conn, site_id=None, limit=None):
    """Convert downloaded files to Markdown for a site."""
    base_dir = os.path.join(os.path.dirname(__file__), "..")

    query = """SELECT id, site_id, external_id, title, published_date, category, pdf_url
               FROM papers WHERE download_status = 'downloaded'"""
    params = []
    if site_id:
        query += " AND site_id = ?"
        params.append(site_id)
    if limit:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    print(f"Found {len(rows)} downloaded papers to convert")

    converted = 0
    failed = 0
    skipped = 0

    for i, row in enumerate(rows, 1):
        s_id = row["site_id"]
        ext_id = row["external_id"]
        title = row["title"] or ""
        pdf_url = row["pdf_url"] or ""

        # Determine source file extension
        if ".hwpx" in pdf_url.lower():
            ext = ".hwpx"
        elif ".hwp" in pdf_url.lower():
            ext = ".hwp"
        elif ".pdf" in pdf_url.lower():
            ext = ".pdf"
        else:
            ext = ""

        src_path = os.path.join(base_dir, "downloads", s_id, f"{ext_id}{ext}")
        if not os.path.exists(src_path):
            # Try without extension
            src_path = os.path.join(base_dir, "downloads", s_id, ext_id)
            if not os.path.exists(src_path):
                continue

        # Output path
        out_dir = os.path.join(base_dir, "converted", s_id)
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"{ext_id}.md")

        if os.path.exists(out_path):
            skipped += 1
            continue

        text = convert_file(src_path)
        if not text:
            failed += 1
            if i <= 20 or i % 100 == 0:
                print(f"  [{i}/{len(rows)}] FAILED: {title[:50]}")
            continue

        md = text_to_markdown(
            text,
            title=title,
            metadata={
                "날짜": row["published_date"] or "",
                "분류": row["category"] or "",
                "원본": os.path.basename(src_path),
            },
        )

        with open(out_path, "w", encoding="utf-8") as f:
            f.write(md)
        converted += 1

        if i <= 10 or i % 100 == 0:
            print(f"  [{i}/{len(rows)}] Converted: {ext_id}.md ({len(text)} chars)")

    print(f"\nDone. Converted: {converted}, Skipped: {skipped}, Failed: {failed}")
    return converted

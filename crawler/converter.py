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
    """Extract text from downloaded documents into the matching ``.txt`` file.

    For each ``documents`` row with ``download_status = 'downloaded'`` the
    corresponding PDF/HWP at ``pdf_path`` is read and the extracted plain
    text is written to a ``.txt`` file with the SAME 12-digit base name in
    the SAME directory (i.e. ``data/AAAA/BBBB/AAAABBBBCCCC.txt``).

    Markdown export is intentionally NOT performed here — that lives in
    ``scripts/export_papers_md.py`` if needed separately.
    """
    from . import storage as _storage

    # Only pull rows that still need conversion (txt_path NULL/empty).
    # Without this filter, ``--limit N`` would keep re-selecting the lowest
    # N already-converted IDs, skip them because the .txt file exists, and
    # never advance past them.
    query = """SELECT id, site_id, external_id, title, pdf_url, pdf_path, txt_path
               FROM documents
               WHERE download_status = 'downloaded'
                 AND (txt_path IS NULL OR txt_path = '')"""
    params = []
    if site_id:
        query += " AND site_id = ?"
        params.append(site_id)
    query += " ORDER BY id"
    if limit:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    print(f"Found {len(rows)} downloaded documents to convert")

    converted = 0
    failed = 0
    skipped = 0

    for i, row in enumerate(rows, 1):
        doc_id = row["id"]
        title = row["title"] or ""

        # Source ext comes from the actual saved pdf_path — that's whatever
        # the magic-byte sniffer picked at download time. Falling back to URL
        # hint and finally ``.pdf`` only when the row is missing pdf_path.
        pdf_path_rel = row["pdf_path"] or ""
        ext = os.path.splitext(pdf_path_rel)[1].lower() if pdf_path_rel else ""
        if ext not in (".pdf", ".hwp", ".hwpx", ".bin"):
            url_lower = (row["pdf_url"] or "").lower()
            if ".hwpx" in url_lower:
                ext = ".hwpx"
            elif ".hwp" in url_lower:
                ext = ".hwp"
            else:
                ext = ".pdf"
        src_path = str(_storage.doc_id_to_path(doc_id, ext))

        if not os.path.exists(src_path):
            failed += 1
            print(f"  [{i}/{len(rows)}] MISSING: {src_path}")
            continue

        # Output: same directory, same 12-digit base, .txt extension.
        out_path = _storage.doc_id_to_txt_path(doc_id)
        _storage.ensure_parent(out_path)

        if out_path.exists() and out_path.stat().st_size > 0:
            # File already converted on disk. Make sure documents.txt_path
            # reflects that — without this, a row with txt_path NULL would
            # stay in get_documents_pending_convert() forever.
            if not (row["txt_path"] or "").strip():
                rel_txt = _storage.doc_id_to_relative_txt(doc_id)
                from . import db as _db
                _db.update_document_paths(conn, doc_id, txt_path=rel_txt)
            skipped += 1
            continue

        text = convert_file(src_path)
        if not text:
            failed += 1
            if i <= 20 or i % 100 == 0:
                print(f"  [{i}/{len(rows)}] FAILED extract: {title[:50]}")
            continue

        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
        # Make sure the DB has the canonical relative txt_path even if upstream
        # _save_document already filled it in.
        rel_txt = _storage.doc_id_to_relative_txt(doc_id)
        from . import db as _db
        _db.update_document_paths(conn, doc_id, txt_path=rel_txt)

        converted += 1
        if i <= 10 or i % 100 == 0:
            print(f"  [{i}/{len(rows)}] Converted: {out_path.name} ({len(text)} chars)")

    print(f"\nDone. Converted: {converted}, Skipped: {skipped}, Failed: {failed}")
    return converted

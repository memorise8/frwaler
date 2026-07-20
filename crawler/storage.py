# -*- coding: utf-8 -*-
"""libertree storage layout.

12자리 zero-padded 시퀀스 ID 를 3-level 계층 디렉터리로 매핑한다.

    data / N[0:4] / N[4:8] / N.{ext}

각 leaf 폴더는 최대 10,000개 파일만 보유하므로
10,000 × 10,000 × 10,000 = 1e12 (1조) 까지 안전하게 분산 저장 가능하다.
"""

from __future__ import annotations

import os
from pathlib import Path

MAX_DOC_ID = 999_999_999_999  # 1조 - 1
DEFAULT_DATA_ROOT = Path(
    os.environ.get(
        "LIBERTREE_DATA_ROOT",
        os.path.join(os.path.dirname(__file__), "..", "data"),
    )
).resolve()


def doc_id_to_path(doc_id: int, ext: str, root: Path | str | None = None) -> Path:
    """Return the 3-level path for a document id.

    Examples
    --------
    >>> str(doc_id_to_path(0, ".pdf", root="data")).replace(os.sep, "/")
    'data/0000/0000/000000000000.pdf'
    >>> str(doc_id_to_path(100_000_000, ".pdf", root="data")).replace(os.sep, "/")
    'data/0001/0000/000100000000.pdf'
    """
    if not isinstance(doc_id, int) or isinstance(doc_id, bool):
        raise TypeError(f"doc_id must be int, got {type(doc_id).__name__}")
    if doc_id < 0 or doc_id > MAX_DOC_ID:
        raise ValueError(
            f"doc_id out of range (0..{MAX_DOC_ID}): {doc_id}"
        )
    if not ext.startswith("."):
        ext = "." + ext
    name = f"{doc_id:012d}"
    base = Path(root) if root is not None else DEFAULT_DATA_ROOT
    return base / name[0:4] / name[4:8] / f"{name}{ext}"


def doc_id_to_pdf_path(doc_id: int, root: Path | str | None = None) -> Path:
    return doc_id_to_path(doc_id, ".pdf", root)


def doc_id_to_txt_path(doc_id: int, root: Path | str | None = None) -> Path:
    return doc_id_to_path(doc_id, ".txt", root)


def doc_id_to_relative(doc_id: int, ext: str) -> str:
    """Return ``data/AAAA/BBBB/N.<ext>`` style path string suitable for DB storage."""
    if not (0 <= doc_id <= MAX_DOC_ID):
        raise ValueError(f"doc_id out of range: {doc_id}")
    if not ext.startswith("."):
        ext = "." + ext
    name = f"{doc_id:012d}"
    return f"data/{name[0:4]}/{name[4:8]}/{name}{ext}"


def doc_id_to_relative_pdf(doc_id: int) -> str:
    return doc_id_to_relative(doc_id, ".pdf")


def doc_id_to_relative_txt(doc_id: int) -> str:
    return doc_id_to_relative(doc_id, ".txt")


def ensure_parent(path: Path) -> Path:
    """Make sure ``path``'s parent directory exists. Returns ``path``."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return Path(path)


# Magic-byte signatures for the file types we extract.
_PDF_MAGIC = b"%PDF-"
_OLE_MAGIC = b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"  # HWP (binary OLE compound document)
_ZIP_MAGIC = b"PK\x03\x04"                         # HWPX (zip), but also any zip


def cleanup_doc_files(doc_id: int, root: Path | str | None = None) -> int:
    """Remove all on-disk files derived from ``doc_id`` (every known ext).

    Returns the number of files actually deleted. Used by ``upsert_document``
    when a recrawl changes ``pdf_url`` — the DB state is reset, and the
    previously downloaded/converted files on disk would otherwise be
    silently reused by ``cmd_download`` / ``convert_site_files``.
    """
    deleted = 0
    for ext in (".pdf", ".hwp", ".hwpx", ".bin", ".tmp", ".txt"):
        p = doc_id_to_path(doc_id, ext, root)
        try:
            if p.exists():
                p.unlink()
                deleted += 1
        except OSError:
            pass
    return deleted


def detect_extension(first_bytes: bytes) -> str:
    """Sniff a downloaded blob's first bytes and return the canonical extension.

    Returns one of ``".pdf"``, ``".hwp"``, ``".hwpx"``, or ``".bin"`` when no
    known signature matches. ``".bin"`` lets the converter still attempt a
    last-resort HWP extraction, matching the ``fino-crawler`` behaviour for
    extensionless attachment endpoints (MOHW ``boardDownload.es``,
    FSC ``displayFile.do`` etc).

    The discrimination between HWPX and other ZIP-based formats is left to
    the converter (``extract_text_hwpx`` walks ``section*.xml`` entries),
    so we surface ``.hwpx`` for any ZIP — that is the dominant case for
    Korean public-sector attachments.
    """
    if not first_bytes:
        return ".bin"
    if first_bytes.startswith(_PDF_MAGIC):
        return ".pdf"
    if first_bytes.startswith(_OLE_MAGIC):
        return ".hwp"
    if first_bytes.startswith(_ZIP_MAGIC):
        return ".hwpx"
    return ".bin"

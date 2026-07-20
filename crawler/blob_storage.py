# -*- coding: utf-8 -*-
"""libertree blob storage layout (Phase A — new infra).

12자리 zero-padded seq_id 를 2-level 폴더로 분산하여 1조개 (1e12) 까지 저장.

    {root}/{name[0:4]}/{name[4:8]}/{name}.{ext}

여기서 ``name = f"{seq_id:012d}"`` 이다. 각 leaf 디렉터리당 최대 10,000개 파일.

이 모듈은 ``crawler/storage.py`` (legacy, ``data/`` 루트) 과 별개로 동작하며
디폴트 루트는 프로젝트 루트의 ``libertree/`` 폴더다. 이로써 기존 papers.db /
data/ 레이아웃과 완전히 분리된다.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

# 1조 - 1 (10^12 - 1)
MAX_SEQ_ID = 999_999_999_999

# 디폴트 루트: <project_root>/libertree
BLOB_ROOT = Path(
    os.environ.get(
        "LIBERTREE_BLOB_ROOT",
        Path(__file__).resolve().parent.parent / "libertree",
    )
).resolve()


def _validate_seq(seq_id: int) -> None:
    if not isinstance(seq_id, int) or isinstance(seq_id, bool):
        raise TypeError(f"seq_id must be int, got {type(seq_id).__name__}")
    if seq_id < 0 or seq_id >= 10**12:
        raise ValueError(f"seq_id out of range [0, 10^12): {seq_id}")


def _normalize_ext(ext: str) -> str:
    if not ext:
        return ""
    if ext.startswith("."):
        ext = ext[1:]
    return ext.lower()


def get_blob_path(seq_id: int, ext: str = "pdf", root: Path | str | None = None) -> Path:
    """seq_id (0 ≤ seq_id < 10^12) → root/XXXX/YYYY/ZZZZZZZZZZZZ.ext

    Examples
    --------
    >>> str(get_blob_path(0, "pdf", root="libertree")).replace(os.sep, "/")
    'libertree/0000/0000/000000000000.pdf'
    >>> str(get_blob_path(123_456_789, "txt", root="libertree")).replace(os.sep, "/")
    'libertree/0000/1234/000123456789.txt'
    """
    _validate_seq(seq_id)
    e = _normalize_ext(ext)
    name = f"{seq_id:012d}"
    base = Path(root) if root is not None else BLOB_ROOT
    leaf = f"{name}.{e}" if e else name
    return base / name[0:4] / name[4:8] / leaf


def ensure_dir(seq_id: int, root: Path | str | None = None) -> Path:
    """Make parent dir if missing, return parent path."""
    path = get_blob_path(seq_id, "pdf", root)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.parent


def has_blob(seq_id: int, ext: str = "pdf", root: Path | str | None = None) -> bool:
    """Existence check."""
    return get_blob_path(seq_id, ext, root).exists()


def save_pdf(
    seq_id: int,
    content: bytes,
    *,
    root: Path | str | None = None,
) -> tuple[Path, int, str]:
    """Save PDF bytes, return (path, size_bytes, sha256_hex)."""
    if not isinstance(content, (bytes, bytearray)):
        raise TypeError(f"content must be bytes, got {type(content).__name__}")
    path = get_blob_path(seq_id, "pdf", root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = bytes(content)
    with open(path, "wb") as f:
        f.write(data)
    size = len(data)
    sha = hashlib.sha256(data).hexdigest()
    return path, size, sha


def save_text(
    seq_id: int,
    text: str,
    *,
    root: Path | str | None = None,
) -> Path:
    """Save extracted text as .txt next to pdf."""
    if not isinstance(text, str):
        raise TypeError(f"text must be str, got {type(text).__name__}")
    path = get_blob_path(seq_id, "txt", root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path

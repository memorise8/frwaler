"""Secure seq_id-derived blob resolution."""
from __future__ import annotations

import os
from pathlib import Path

from crawler.blob_storage import get_blob_path


def resolve_blob(conn,seq_id:int,kind:str) -> tuple[Path,str,str] | None:
    if kind not in ("pdf","text"):raise ValueError("invalid blob kind")
    row=conn.execute("SELECT pdf_downloaded,text_extracted,original_filename FROM documents WHERE seq_id=%s",(seq_id,)).fetchone()
    if row is None:return None
    flag=row["pdf_downloaded"] if kind=="pdf" else row["text_extracted"]
    if not flag:raise FileNotFoundError("blob not available")
    root=Path(os.environ.get("LIBERTREE_BLOB_ROOT","/data/blob")).resolve()
    ext="pdf" if kind=="pdf" else "txt"
    path=get_blob_path(seq_id,ext,root=root).resolve()
    if root not in path.parents:raise FileNotFoundError("invalid blob path")
    if not path.is_file():raise FileNotFoundError("blob missing")
    media="application/pdf" if kind=="pdf" else "text/plain; charset=utf-8"
    filename=(row["original_filename"] or f"{seq_id:012d}.pdf") if kind=="pdf" else f"{seq_id:012d}.txt"
    return path,media,filename

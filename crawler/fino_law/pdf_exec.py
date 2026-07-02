from __future__ import annotations

import re
import subprocess
from pathlib import Path

_HEADER = re.compile(r"^집행기준\s+(\d+(?:-\d+)+)\s+(.*\S)")
_PAGE_NO = re.compile(r"^-?\s*\d+\s*-?$")


def pdf_to_text(pdf_path: Path) -> str:
    result = subprocess.run(
        ["pdftotext", "-layout", str(pdf_path), "-"],
        capture_output=True, text=True, timeout=180,
    )
    return result.stdout


def extract_bodies(text: str, page_headers: tuple[str, ...]) -> dict[str, str]:
    """pdftotext 결과 → {집행기준번호: 본문}. 목차/페이지노이즈 제거, 본문 헤더로 분할."""
    bodies: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            if current is not None:
                buf.append("")
            continue
        if s in page_headers or _PAGE_NO.match(s):
            continue  # 페이지 머리말/꼬리말/쪽번호
        m = _HEADER.match(s)
        # 본문 헤더 = 목차 리더(·/…)·끝 페이지번호 없음
        if m and "…" not in s and "·" not in s and not re.search(r"\d+$", s):
            if current is not None:
                bodies[current] = "\n".join(buf).strip()
            current = m.group(1)
            buf = []
            continue
        if current is not None:
            buf.append(line)
    if current is not None:
        bodies[current] = "\n".join(buf).strip()
    return {k: v for k, v in bodies.items() if len(v) > 5}

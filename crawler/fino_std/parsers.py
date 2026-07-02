from __future__ import annotations

import html as _html
import re

from .models import ParagraphRecord, Section
from .sources import para_citation_url

_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(raw: str) -> str:
    text = _TAG_RE.sub(" ", raw or "")
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def pick_big_sections(titles_json: dict) -> list[Section]:
    out: list[Section] = []
    for t in titles_json.get("titles") or []:
        if t.get("type") != "big":
            continue
        out.append(Section(
            document_id=str(t.get("documentId") or ""),
            title=(t.get("title") or "").strip(),
            ref=str(t.get("ref") or ""),
        ))
    return [s for s in out if s.document_id]


def parse_content(content_json: dict, *, std_num: int, start_seq: int) -> list[ParagraphRecord]:
    """clauses 스트림 순회. title clause는 level 스택으로 섹션경로를 만들고,
    paragraph clause는 문단 레코드로 변환한다."""
    stack: dict[int, str] = {}
    out: list[ParagraphRecord] = []
    seq = start_seq
    for cl in content_json.get("clauses") or []:
        kind = cl.get("type")
        if kind == "title":
            level = int(cl.get("level") or 0)
            title = (cl.get("title") or "").strip()
            for deeper in [k for k in stack if k >= level]:
                del stack[deeper]
            if title:
                stack[level] = title
        elif kind == "paragraph":
            raw = cl.get("content") or ""
            text = html_to_text(raw)
            if not text:
                continue
            num = str(cl.get("number") or "").strip()
            titles = [stack[k] for k in sorted(stack)]
            dedup = [t for i, t in enumerate(titles) if i == 0 or t != titles[i - 1]]
            out.append(ParagraphRecord(
                para_num=num,
                section_path=" > ".join(dedup),
                body_html=raw,
                body_text=text,
                seq=seq,
                source_url=para_citation_url(std_num, num),
            ))
            seq += 1
    return out

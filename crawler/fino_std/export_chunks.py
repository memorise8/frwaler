"""크롤러 DB → 회계 청크 디렉토리.

    python -m crawler.fino_std.export_chunks --out /path/out --std kifrs:1109

`0.chunking_data` 규약에 맞춰 쓴다 — 디렉토리 하나가 문서 하나, 파일 하나가
인용 단위 하나, 본문은 `## 문단 N` 헤딩으로 시작한다. 백엔드 청커가 이 꼴을
그대로 읽어 ndjson 을 만든다.

## 무번호 clause

KASB 조문 API 는 번호 없는 조각에도 `number` 를 붙여 보내는데, 그 값이
`웩1`·`왝0` 처럼 뜻 없는 한글이다. 실물을 보면 대부분(86%) 80자 이하의
**소제목**이다 — "유효이자율법", "제각", "기대주가변동성". 이것을 문단 번호로
쓰면 인용란에 `제1109호 문단 웩4` 같은 것이 뜬다.

그래서 둘로 가른다.

    짧고 종결어미가 없다  →  소제목. 뒤따르는 문단들의 맥락으로만 쓰고 버린다
    그 밖                →  본문. 번호 없이 `무번호_NNNNN.md` 로 남긴다

버리는 쪽이 대부분이지만 본문 쪽도 161건 있다(제1012호 적용사례 등). 현행
코퍼스는 이 둘을 통째로 빠뜨렸다.

## 문단 번호 충돌

같은 문서 안에서 문단 번호가 되돌아가는 자리가 둘 있다.

**개정 회차** — 제60장 '시행일 및 경과규정' 은 회차마다 문단 1 부터 다시
시작한다. 파일명으로 쓰면 조용히 덮어써진다(69개가 30개가 된다).
`--split-by-section` 을 주면 최상위 절을 문서로 갈라 회차별로 나눈다.

**소수의견** — 위원 한 사람의 의견이 한 묶음이고, 묶음마다 DO1 로 되돌아간다.
묶음을 가르는 것은 위원 이름을 적은 소제목인데, 그것이 위에서 버리는 무번호
조각이다. 그래서 번호가 겹치는 순간 그 소제목을 이름으로 삼아 딸림 문서를
연다(`..._McConnell_위원의_소수의견`). 겹치는 동안은 계속 그 문서에 쌓는다.

두 장치로도 안 풀리는 충돌은 오류로 세운다 — 조용한 유실보다 낫다.

## 조문 API 는 최신 개정을 담고 있지 않다

`db.kasb.or.kr` 조문 API 는 공표 게시판 PDF 보다 **뒤처져 있다.** 2026-08 현재
목차에 아래가 없다.

    제1001호 72A·72B·139U·139W   부채의 유동·비유동 분류(2020·2023 개정)
    제1007호 44F~44H·62·63       공급자금융약정(2023 개정)
    제1012호 4A·88A~88D·98M      국제조세개혁 필라2(2023 개정)
    제1116호 102A·C1D·C20E       판매후리스에서 생기는 리스부채(2023 개정)

그래서 크롤러만으로 갈아끼우면 **시행 중인 요구사항이 색인에서 사라진다.**
`--fill-from` 을 주면 기존 코퍼스(공표 PDF 계열)에만 있는 문단을 얹는다.
본문이 크롤러 쪽에 이미 들어 있으면 얹지 않는다 — PDF 추출이 한 문단을
쪼개 놓은 부스러기(`58당`, `IE12A`)를 거르기 위해서다.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from .db import connect_db

DEFAULT_DB_PATH: Final = Path("data/fino_std.db")

# 문단 번호로 인정하는 한글 접두. 이 밖의 한글로 시작하면 API 의 무번호 표시다.
#   한  한국채택 시 추가된 문단(한2.1)      실  실무지침
#   결  결론도출근거                        소  소수의견        사  사례
_NUMBER_PREFIXES: Final = frozenset("한실결소사")
# 소제목으로 볼 최대 길이. 90 백분위가 134자, 75 백분위가 41자다.
_SUBHEADING_MAX: Final = 120
# 종결어미. 문장으로 끝나면 제목이 아니라 본문이다.
_SENTENCE_END: Final = re.compile(r"(다|음|함|것|요)\.?\s*$|[.。]\s*$")
# 파일명에 쓸 수 없는 글자.
_UNSAFE: Final = re.compile(r"[/\\:*?\"<>|\x00-\x1f]")
_WS: Final = re.compile(r"\s+")
_SOURCE_NO_RE: Final = re.compile(r"제(\d+)(?:호|장)")
_COMMENT_SOURCE: Final = re.compile(r"<!--\s*source:\s*(.*?)\s*-->", re.DOTALL)
# 본문 대조용. 띄어쓰기와 문장부호는 두 계열이 서로 다르게 넣는다.
_PUNCT: Final = re.compile(r"[\s​·,.()\[\]{}‘’“”'\"~∼–—:;-]")
# 딸림 문서 이름에 쓸 절 제목의 최대 길이. 소수의견 제목은 위원 이름을 다
# 늘어놓아 200자가 넘기도 한다.
_LABEL_MAX: Final = 40

_CATEGORY: Final = {
    "gaap": "GAAP",
    "kifrs": "KIFRS",
    "kifrs_interp": "KIFRS",
    "kifrs_etc": "KIFRS",
}

# 절 이름 → chunk_type. 번호 접두로 판정이 안 될 때만 쓰인다(청커가 접두를 먼저 본다).
_SECTION_TYPES: Final = (
    ("용어의 정의", "glossary"),
    ("결론도출근거", "basis"),
    ("소수의견", "basis"),
    ("적용사례", "illustrative"),
    ("사례", "illustrative"),
    ("실무적용지침", "implementation_guidance"),
    ("실무지침", "practice"),
    ("적용보충기준", "application_guidance"),
    ("부록", "application_guidance"),
)


@dataclass(slots=True)
class DocStats:
    doc_id: str
    paragraphs: int = 0
    unnumbered_body: int = 0
    subheadings: int = 0
    refs: list[str] = field(default_factory=list)


def is_unnumbered(para_num: str) -> bool:
    """API 가 붙인 뜻 없는 번호인가."""
    num = para_num.strip()
    if not num:
        return True
    head = num[0]
    return "가" <= head <= "힣" and head not in _NUMBER_PREFIXES


def is_subheading(text: str) -> bool:
    """무번호 조각이 소제목인가 — 짧고 문장으로 끝나지 않는다."""
    body = text.strip()
    return len(body) <= _SUBHEADING_MAX and _SENTENCE_END.search(body) is None


def safe_component(name: str) -> str:
    """파일·디렉토리 이름 조각으로 안전하게 만든다."""
    cleaned = _UNSAFE.sub("_", unicodedata.normalize("NFC", name)).strip(" .")
    return _WS.sub("_", cleaned)


def chunk_type_for(section_path: str) -> str:
    for needle, kind in _SECTION_TYPES:
        if needle in section_path:
            return kind
    return "standard"


def top_section(section_path: str) -> str:
    return section_path.split(" > ", maxsplit=1)[0].strip()


def document_dir_name(
    *, title: str, std_num: int, std_type: str, section: str = "", existing: str = "",
) -> str:
    """문서 디렉토리 이름.

    기존 코퍼스와 같은 꼴로 맞춘다 — 청커가 이 이름에서 `source_name` 과
    `source_no` 를 뽑기 때문이다(`제(\\d+)(호|장)`).

    이미 코퍼스에 있는 문서면 **그 이름을 그대로 쓴다.** 청크 식별자가
    `{디렉토리}::{파일}` 이라서, 이름을 바꾸면 골든셋 정답 라벨 430개가
    통째로 어긋난다 — 최신화 전후를 견줄 수 없게 된다.
    """
    label = f"제{std_num}장" if std_type == "gaap" else f"제{std_num}호"
    base = existing or f"{label}_{safe_component(title)}"
    if not section:
        return base
    slug = safe_component(section)
    # 절 이름이 이미 문서 제목을 되풀이하면(`시행일 및 경과규정(2009.12.30.)`)
    # 겹쳐 쓰지 않는다.
    if slug.startswith(safe_component(title)):
        prefix = base[: base.index(label)] if label in base else ""
        return f"{prefix}{label}_{slug}"
    return f"{base}_{slug}"


def existing_dir_names(corpus_root: Path) -> dict[tuple[str, int], str]:
    """코퍼스에 이미 있는 문서 디렉토리 이름을 `(카테고리, 번호)` 로 찾아 둔다."""
    found: dict[tuple[str, int], str] = {}
    for category in ("GAAP", "KIFRS"):
        base = corpus_root / f"{category}_chunks"
        if not base.is_dir():
            continue
        for path in sorted(base.iterdir()):
            if not path.is_dir():
                continue
            match = _SOURCE_NO_RE.search(unicodedata.normalize("NFC", path.name))
            if match is None:
                continue
            key = (category, int(match.group(1)))
            # 빈 껍데기 디렉토리가 섞여 있다. 내용이 있는 쪽을 고른다.
            if key in found and not any(path.rglob("*.md")):
                continue
            found[key] = path.name
    return found


def _render(
    *, std_type: str, std_num: int, collected_at: str, para_num: str,
    section_path: str, body: str, heading: str,
) -> str:
    return (
        f"<!-- source: kasb-api:{std_type}/{std_num}@{collected_at[:10]}"
        f" / {para_num or heading} -->\n"
        f"<!-- chunk_type: {chunk_type_for(section_path)} -->\n"
        f"<!-- section: {section_path} -->\n"
        f"\n## {heading}\n{body}\n"
    )


def export_document(
    conn: sqlite3.Connection,
    *,
    std_type: str,
    std_num: int,
    out_root: Path,
    split_by_section: bool = False,
    existing_name: str = "",
) -> list[DocStats]:
    doc = conn.execute(
        "SELECT id, title, collected_at FROM documents WHERE std_type = ? AND std_num = ?",
        (std_type, std_num),
    ).fetchone()
    if doc is None:
        raise SystemExit(f"문서 없음: {std_type} {std_num}")
    rows = conn.execute(
        "SELECT seq, para_num, section_path, body_text FROM paragraphs"
        " WHERE document_id = ? ORDER BY seq",
        (doc["id"],),
    ).fetchall()

    category = _CATEGORY[std_type]
    base_dir = out_root / f"{category}_chunks"
    stats: dict[str, DocStats] = {}
    written: dict[Path, str] = {}
    # 소제목은 다음 무번호 본문의 헤딩이자, 번호가 겹칠 때 딸림 문서의 이름이다.
    # 절이 바뀌면 버린다 — 남의 절 헤딩이 되면 안 된다.
    pending_heading = ""
    current_top = ""
    # 번호 충돌로 열린 딸림 문서. 소제목이 바뀌면 닫는다.
    open_block = ""

    for row in rows:
        section = row["section_path"] or ""
        body = row["body_text"].strip()
        if not body:
            continue
        section_top = top_section(section)
        if section_top != current_top:
            current_top, pending_heading, open_block = section_top, "", ""

        base_name = document_dir_name(
            title=doc["title"], std_num=std_num, std_type=std_type,
            section=section_top if split_by_section else "", existing=existing_name,
        )

        if is_unnumbered(row["para_num"]):
            if is_subheading(body):
                # 새 소제목은 앞 묶음의 끝이기도 하다.
                pending_heading, open_block = body, ""
                stats.setdefault(base_name, DocStats(doc_id=base_name)).subheadings += 1
                continue
            heading = pending_heading or section.split(" > ")[-1] or doc["title"]
            file_name = f"무번호_{row['seq']:05d}.md"
            para_label = ""
        else:
            num = row["para_num"].strip()
            heading = f"문단 {num}"
            file_name = f"문단_{safe_component(num)}.md"
            para_label = heading

        dir_name = _resolve_dir(
            base_name=base_name, open_block=open_block, file_name=file_name,
            written=written, base_dir=base_dir,
            label=pending_heading or section.split(" > ")[-1],
        )
        open_block = dir_name[len(base_name) :].lstrip("_") if dir_name != base_name else ""
        path = base_dir / dir_name / file_name

        stat = stats.setdefault(dir_name, DocStats(doc_id=dir_name))
        if para_label:
            stat.paragraphs += 1
            stat.refs.append(row["para_num"].strip())
        else:
            stat.unnumbered_body += 1
        written[path] = _render(
            std_type=std_type, std_num=std_num, collected_at=doc["collected_at"],
            para_num=para_label, section_path=section, body=body, heading=heading,
        )

    for path, text in written.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(text, encoding="utf-8")
    return list(stats.values())


def fill_from_corpus(
    conn: sqlite3.Connection,
    *,
    std_type: str,
    std_num: int,
    corpus_dir: Path,
    target_dir: Path,
) -> list[str]:
    """크롤러가 놓친 문단을 기존 코퍼스에서 얹는다.

    얹는 조건은 둘. 번호가 크롤러 쪽에 없어야 하고, **본문도 크롤러 쪽에 없어야
    한다.** 두 번째가 핵심이다 — PDF 추출이 한 문단을 둘로 쪼개 놓으면 뒤쪽
    조각이 `IE12A` 같은 없는 번호를 달고 나타나는데, 본문은 이미 `IE12` 안에
    들어 있다. 번호만 보고 얹으면 같은 글이 두 번 색인된다.
    """
    doc = conn.execute(
        "SELECT id FROM documents WHERE std_type = ? AND std_num = ?", (std_type, std_num)
    ).fetchone()
    if doc is None:
        return []
    rows = conn.execute(
        "SELECT para_num, body_text FROM paragraphs WHERE document_id = ?", (doc["id"],)
    ).fetchall()
    known = {r["para_num"].strip() for r in rows}
    blob = _normalize(" ".join(r["body_text"] for r in rows))

    added: list[str] = []
    for path in sorted(corpus_dir.rglob("*.md")):
        if "check" in path.parts:
            continue
        raw = path.read_text(encoding="utf-8")
        source = _COMMENT_SOURCE.search(raw)
        for ref, body in _corpus_segments(raw):
            if ref in known or not body:
                continue
            probe = _normalize(body)[:40]
            if not probe or probe in blob:
                continue
            out = target_dir / f"문단_{safe_component(ref)}.md"
            if out.exists():
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            _ = out.write_text(
                f"<!-- source: {source.group(1).strip() if source else path.name} -->\n"
                # 청커가 번호 접두(BC·IE·IG…)를 먼저 보므로 이 값은 접두가 없는
                # 본문 문단(44F·102A·4A)에만 쓰인다.
                f"<!-- chunk_type: standard -->\n"
                f"<!-- fill_reason: 조문 API 미수록 (공표 PDF 계열에서 보충) -->\n"
                f"\n## 문단 {ref}\n{body}\n",
                encoding="utf-8",
            )
            added.append(ref)
            known.add(ref)
    return added


def _corpus_segments(raw: str) -> list[tuple[str, str]]:
    body = re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL)
    matches = list(re.finditer(r"^##\s*문단\s*(\S+)\s*$", body, re.MULTILINE))
    out: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        out.append((m.group(1), body[m.end() : end].strip()))
    return out


def _normalize(text: str) -> str:
    return _PUNCT.sub("", text)


def _resolve_dir(
    *, base_name: str, open_block: str, file_name: str,
    written: dict[Path, str], base_dir: Path, label: str,
) -> str:
    """이 문단을 어느 문서 디렉토리에 쓸지 정한다.

    이미 딸림 문서가 열려 있으면 그대로 쓴다 — 한 묶음이 반쯤은 본문서에,
    반쯤은 딸림 문서에 흩어지면 안 되기 때문이다. 열려 있지 않은데 이름이
    겹치면 그때 딸림 문서를 연다.
    """
    if open_block and base_dir / f"{base_name}_{open_block}" / file_name not in written:
        return f"{base_name}_{open_block}"
    if base_dir / base_name / file_name not in written:
        return base_name
    slug = safe_component(label)[:_LABEL_MAX].rstrip("_") or "묶음"
    for suffix in ("", *(f"_{n}" for n in range(2, 50))):
        candidate = f"{base_name}_{slug}{suffix}"
        if base_dir / candidate / file_name not in written:
            return candidate
    raise SystemExit(
        f"파일명 충돌을 풀지 못했습니다: {base_name}/{file_name} — 문서를 손으로 나누세요."
    )


def parse_target(spec: str) -> tuple[str, int]:
    std_type, _, num = spec.partition(":")
    if std_type not in _CATEGORY or not num.isdecimal():
        raise SystemExit(f"대상 형식이 잘못됨: {spec!r} (예: kifrs:1109)")
    return std_type, int(num)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m crawler.fino_std.export_chunks")
    _ = p.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    _ = p.add_argument("--out", type=Path, required=True, help="청크를 쓸 최상위 디렉토리")
    _ = p.add_argument(
        "--std", action="append", default=[],
        help="대상 문서 `타입:번호` (여러 번 지정 가능). 생략하면 --all 필요",
    )
    _ = p.add_argument("--all", action="store_true", help="DB 의 모든 문서")
    _ = p.add_argument(
        "--split-by-section", action="append", default=[],
        help="최상위 절을 문서로 가를 대상 `타입:번호` (제60장처럼 번호가 겹치는 문서)",
    )
    _ = p.add_argument(
        "--match-existing", type=Path,
        help="기존 코퍼스 경로. 이미 있는 문서는 그 디렉토리 이름을 그대로 쓴다"
             " (청크 식별자가 바뀌면 골든셋 라벨이 어긋난다)",
    )
    _ = p.add_argument(
        "--fill-from", type=Path,
        help="기존 코퍼스 경로. 조문 API 에 없는 최신 개정 문단을 이 코퍼스에서 얹는다",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.std and not args.all:
        print("--std 또는 --all 이 필요합니다", file=sys.stderr)
        return 2

    conn = connect_db(args.db_path)
    try:
        if args.all:
            targets = [
                (r["std_type"], r["std_num"])
                for r in conn.execute(
                    "SELECT std_type, std_num FROM documents ORDER BY std_type, std_num"
                ).fetchall()
            ]
        else:
            targets = [parse_target(s) for s in args.std]
        split = {parse_target(s) for s in args.split_by_section}
        names = existing_dir_names(args.match_existing) if args.match_existing else {}

        total = Counter()
        for std_type, std_num in targets:
            for stat in export_document(
                conn, std_type=std_type, std_num=std_num, out_root=args.out,
                split_by_section=(std_type, std_num) in split,
                existing_name=names.get((_CATEGORY[std_type], std_num), ""),
            ):
                dup = [n for n, k in Counter(stat.refs).items() if k > 1]
                flag = f"  ⚠ 중복번호 {dup}" if dup else ""
                print(
                    f"{stat.doc_id[:58]:<58} 문단 {stat.paragraphs:>5}"
                    f"  무번호본문 {stat.unnumbered_body:>4}"
                    f"  소제목 {stat.subheadings:>4}{flag}"
                )
                total["paragraphs"] += stat.paragraphs
                total["unnumbered_body"] += stat.unnumbered_body
                total["subheadings"] += stat.subheadings

            if args.fill_from is None:
                continue
            category = _CATEGORY[std_type]
            source_name = existing_dir_names(args.fill_from).get((category, std_num))
            base = names.get((category, std_num), "")
            if source_name is None or not base:
                continue
            added = fill_from_corpus(
                conn, std_type=std_type, std_num=std_num,
                corpus_dir=args.fill_from / f"{category}_chunks" / source_name,
                target_dir=args.out / f"{category}_chunks" / base,
            )
            total["filled"] += len(added)
            if added:
                print(f"{'':4}↳ 조문 API 미수록 {len(added)}건 보충: {sorted(added)[:8]}")
        print(
            f"\n문서 {len(targets)}종 · 문단 {total['paragraphs']}"
            f" · 무번호본문 {total['unnumbered_body']} · 소제목(버림) {total['subheadings']}"
            f" · 코퍼스 보충 {total['filled']}"
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

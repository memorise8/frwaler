"""공표 게시판 판본 대장 ↔ 보유 원본 대조.

    python -m crawler.fino_std.board_diff --holdings /path/to/MD_SET

파일명에 판본이 박혀 있으므로(`..._수정목록_26-1_...`) 본문을 받지 않고도
무엇이 낡았는지 가릴 수 있다. 판정은 셋 중 하나다.

    같음    보유 파일명이 게시판 것과 일치 — 최신
    낡음    같은 기준서인데 파일명이 다름 — 개정판이 올라와 있다
    없음    보유분에 아예 없음 — 미수집

기준서를 잇는 열쇠는 파일명이 아니라 **문서 번호**다. 보유분은 MD_SET 접두번호
(`2.시행중_...`)가 붙어 있고 게시판 것은 없으며, 개정 표기도 서로 다르기 때문이다.
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .board import PublishedFile
from .collect_board import DEFAULT_DB_PATH, load_registry

# 파일명에서 기준서를 특정하는 번호. 앞의 것이 먼저 걸린다.
_KEYS: Final = (
    re.compile(r"제\s*(\d{4})\s*호"),                    # K-IFRS 제1101호 / 해석서 제2112호
    re.compile(r"제\s*(\d{1,2})\s*장"),                  # 일반기업회계기준 제29장
    re.compile(r"(재무회계개념체계)"),
    re.compile(r"(재무보고를_?위한_?개념체계)"),
    re.compile(r"(보험업회계처리준칙)"),
    re.compile(r"(시행일_?및_?경과규정)"),
    re.compile(r"(영문양식)"),
)
_VERSION_NOISE: Final = re.compile(r"[\s_]+")


@dataclass(frozen=True, slots=True)
class DiffRow:
    key: str
    verdict: str          # 같음 / 낡음 / 없음
    published: str        # 게시판 파일명
    held: str             # 보유 파일명 (없으면 "")


def doc_key(name: str) -> str:
    """파일명 → 기준서 식별자. 못 찾으면 확장자 뗀 이름 자체."""
    stem = unicodedata.normalize("NFC", name).rsplit(".", 1)[0]
    for pattern in _KEYS:
        found = pattern.search(stem)
        if found is not None:
            token = found.group(1)
            return f"제{token}호" if token.isdigit() and len(token) == 4 else (
                f"제{token}장" if token.isdigit() else token
            )
    return _VERSION_NOISE.sub("_", stem)


def version_sig(name: str) -> str:
    """파일명 → 판본 표기.

    이름 전체로 비교하면 안 된다. 보유분은 MD_SET 접두번호와 정리 날짜가 붙어
    있고(`4.제4장_...(...)_25.11.7.md`) 게시판 것은 없다(`제4장_...(...).pdf`).
    판본은 괄호 안에만 들어 있으므로 그것만 본다.

        (2018년_개정_반영_수정목록_20-1_반영)  →  2018년개정반영수정목록20-1반영

    괄호가 없으면 빈 문자열 — 판본 표기가 아예 없는 문서끼리는 같다고 본다.
    """
    stem = unicodedata.normalize("NFC", name).rsplit(".", 1)[0]
    groups = re.findall(r"\(([^()]*)\)", stem)
    if not groups:
        return ""
    return _VERSION_NOISE.sub("", "".join(groups)).replace(".", "")


def compare(
    published: list[PublishedFile], holdings: list[str], *, ext: str = "pdf"
) -> list[DiffRow]:
    held_by_key: dict[str, list[str]] = {}
    for name in holdings:
        held_by_key.setdefault(doc_key(name), []).append(name)

    rows: list[DiffRow] = []
    for item in published:
        if item.ext != ext:
            continue
        key = doc_key(item.file_name)
        held = held_by_key.get(key, [])
        if not held:
            rows.append(DiffRow(key, "없음", item.file_name, ""))
            continue
        target = version_sig(item.file_name)
        match = next((h for h in held if version_sig(h) == target), None)
        rows.append(
            DiffRow(key, "같음" if match else "낡음", item.file_name, match or held[0])
        )
    return rows


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m crawler.fino_std.board_diff")
    _ = p.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    _ = p.add_argument(
        "--holdings", type=Path, action="append", required=True,
        help="보유 원본 디렉토리(여러 번 지정 가능). 하위까지 훑는다",
    )
    _ = p.add_argument("--ext", default="pdf", help="게시판 첨부 중 비교할 확장자")
    _ = p.add_argument("--only", choices=("낡음", "없음", "같음"), help="해당 판정만 출력")
    _ = p.add_argument("--board", action="append", help="게시판 코드로 좁힌다")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    holdings: list[str] = []
    for root in args.holdings:
        if not root.is_dir():
            print(f"보유 경로 없음: {root}", file=sys.stderr)
            return 2
        holdings += [p.name for p in root.rglob("*") if p.is_file()]

    registry = load_registry(args.db_path)
    if args.board:
        registry = [i for i in registry if i.board in set(args.board)]
    rows = compare(registry, holdings, ext=args.ext)
    shown = [r for r in rows if not args.only or r.verdict == args.only]
    for r in sorted(shown, key=lambda x: (x.verdict, x.key)):
        print(f"{r.verdict}  {r.key:<12} {r.published[:76]}")
        if r.verdict == "낡음":
            print(f"{'':6}{'보유':<12} {r.held[:76]}")
    tally = {v: sum(1 for r in rows if r.verdict == v) for v in ("같음", "낡음", "없음")}
    print(f"\n같음 {tally['같음']} · 낡음 {tally['낡음']} · 없음 {tally['없음']} (총 {len(rows)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

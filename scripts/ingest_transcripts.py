"""사람이 만든 전사 문서를 fino_acct 표준 마크다운으로 변환한다.

사람은 "읽을 수 있는 문서"만 만들면 된다. 메타데이터(frontmatter)는 DB에 이미 있으므로
파일명 앞의 자료번호로 매칭해 기계가 붙인다.

입력 파일명 규칙:  {작업번호}_{문서ID}_....{md|txt|docx}
    예) 01_14410.md, 04_14395_대리인의수익인식.md
    → 문서ID 14410 / 14395 로 acct_documents 를 찾아 메타데이터를 붙인다.

본문 끝의 `## 전사 확인 메모` 블록은 본문에서 분리해 frontmatter 옆 메모로 보존한다.

사용법:
    python -m scripts.ingest_transcripts --input-dir data/handoff_ocr_20260806/결과
    python -m scripts.ingest_transcripts --input-dir ... --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler.fino_acct.export_markdown import build_markdown

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path("data/fino_acct.db")
OUTPUT_DIR = Path("data/fino_acct_md_manual")
NAME_RE = re.compile(r"^(\d+)_(\d+)")
# docx 로 받으면 마크다운 '##' 이 사라지므로 제목 없는 줄도 인정한다.
MEMO_HEADING_RE = re.compile(r"^\s*#{0,6}\s*전사\s*확인\s*메모\s*$", re.MULTILINE)
UNCERTAIN_RE = re.compile(r"\[\?[^\]]*\]")
# curate_markdown 이 200자 미만을 quarantine 으로 보내므로 같은 기준으로 미리 경고한다.
MIN_BODY_CHARS = 200


def read_any(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt"}:
        return path.read_text(encoding="utf-8", errors="ignore")
    if suffix == ".docx":
        with zipfile.ZipFile(path) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
        parts: list[str] = []
        for element in root.iter():
            tag = element.tag.rsplit("}", 1)[-1]
            if tag == "t" and element.text:
                parts.append(element.text)
            elif tag == "p":
                parts.append("\n")
        return "".join(parts)
    raise ValueError(f"지원하지 않는 형식: {suffix}")


def strip_leading_frontmatter(text: str) -> str:
    """사람이 실수로 --- 블록을 붙여 왔으면 떼어낸다(기계가 다시 붙이므로)."""
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return text
    end = stripped.find("\n---", 3)
    return stripped[end + 4:].lstrip() if end != -1 else text


def split_memo(text: str) -> tuple[str, str]:
    match = MEMO_HEADING_RE.search(text)
    if match is None:
        return text.strip(), ""
    return text[: match.start()].strip(), text[match.end():].strip()


def main() -> int:
    parser = argparse.ArgumentParser(prog="ingest_transcripts")
    _ = parser.add_argument("--input-dir", type=Path, required=True)
    _ = parser.add_argument("--db-path", type=Path, default=DB_PATH)
    _ = parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    _ = parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{REPO_ROOT / args.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    files = sorted(
        p for p in args.input_dir.iterdir()
        if p.is_file() and p.suffix.lower() in {".md", ".txt", ".docx"}
    )
    if not files:
        print(f"[경고] {args.input_dir} 에 변환할 파일이 없습니다.")
        return 1

    print(f"[start] 입력 {len(files)}건 | 출력 {args.output_dir}")
    results: list[dict] = []
    problems: list[str] = []

    for path in files:
        match = NAME_RE.match(path.stem)
        if match is None:
            problems.append(f"{path.name}: 파일명에서 자료번호를 못 찾음 (규칙: 01_14410_....)")
            continue
        document_id = int(match.group(2))
        row = conn.execute(
            "SELECT id, source_priority, agency, target_name, source_url, source_type, "
            "source_subtype, index_name, external_id, title, detail_url, published_date "
            "FROM acct_documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        if row is None:
            problems.append(f"{path.name}: 자료번호 {document_id} 가 DB에 없음")
            continue

        try:
            raw = read_any(path)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{path.name}: 읽기 실패 ({exc})")
            continue

        body, memo = split_memo(strip_leading_frontmatter(raw))
        uncertain = len(UNCERTAIN_RE.findall(body))
        if len(body) < MIN_BODY_CHARS:
            problems.append(f"{path.name}: 본문 {len(body)}자 — {MIN_BODY_CHARS}자 미만이라 큐레이션에서 격리됨")

        metadata = {
            "agency": row["agency"],
            "target_name": row["target_name"],
            "source_priority": str(row["source_priority"]),
            "source_type": row["source_type"],
            "source_subtype": row["source_subtype"],
            "index_name": row["index_name"],
            "source_title": row["title"],
            "detail_url": row["detail_url"],
            "published_date": row["published_date"],
            "external_id": row["external_id"],
            "document_id": str(row["id"]),
            "provenance": "manual_transcription",
            "transcription_source": path.name,
            "uncertain_marks": str(uncertain),
        }
        markdown = build_markdown(title=row["title"], metadata=metadata, text=body)
        if memo:
            markdown += f"\n\n<!-- 전사 확인 메모\n{memo}\n-->\n"

        out_dir = REPO_ROOT / args.output_dir / f"{int(row['source_priority']):02d}"
        out_path = out_dir / f"{document_id}_{path.stem}.md"
        if not args.dry_run:
            out_dir.mkdir(parents=True, exist_ok=True)
            _ = out_path.write_text(markdown, encoding="utf-8")

        results.append({
            "input": path.name,
            "document_id": document_id,
            "priority": row["source_priority"],
            "title": row["title"],
            "body_chars": len(body),
            "uncertain_marks": uncertain,
            "has_memo": bool(memo),
            "output": str(out_path.relative_to(REPO_ROOT)),
        })
        flag = f" ⚠{uncertain}곳 불확실" if uncertain else ""
        print(f"  OK  {path.name} → 자료{document_id} 본문 {len(body):,}자{flag}")

    if not args.dry_run and results:
        manifest = REPO_ROOT / args.output_dir / "_manifest.jsonl"
        _ = manifest.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in results), encoding="utf-8"
        )

    print(f"\n[done] 변환 {len(results)} | 문제 {len(problems)}")
    if results:
        print(f"  본문 합계 {sum(r['body_chars'] for r in results):,}자 "
              f"| 불확실 표기 {sum(r['uncertain_marks'] for r in results)}곳 "
              f"| 확인메모 있음 {sum(1 for r in results if r['has_memo'])}건")
    for problem in problems:
        print(f"  ⚠ {problem}")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())

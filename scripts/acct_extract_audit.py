"""우선순위 18~23 첨부의 ZIP 전개 + 텍스트 추출 + 커버리지 감사.

단계:
  1) ZIP 전개 — data/fino_acct_docs/{NN}/_unzipped/{doc_id}/ 로 풀고 내부 파일을 목록에 편입
  2) 텍스트 추출 — 기존 export_markdown.extract_text(pdf/hwp/hwpx/docx) 우선,
     미지원 포맷(ppt/pptx/doc/xls)은 LibreOffice headless 로 변환 후 재시도
  3) 감사 — 포맷별 성공/실패/빈 텍스트 집계, 사람 개입이 필요한 목록 산출

산출물:
  data/fino_acct_text/{NN}/...txt      추출 텍스트
  data/acct_extract_audit.json         파일 단위 결과
  data/acct_extract_audit_summary.json 집계

사용법:
    python -m scripts.acct_extract_audit
    python -m scripts.acct_extract_audit --limit 50      # 스모크
    python -m scripts.acct_extract_audit --skip-unzip
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler.fino_acct.export_markdown import extract_text

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = REPO_ROOT / "data" / "fino_acct.db"
TEXT_ROOT = REPO_ROOT / "data" / "fino_acct_text"
AUDIT_PATH = REPO_ROOT / "data" / "acct_extract_audit.json"
SUMMARY_PATH = REPO_ROOT / "data" / "acct_extract_audit_summary.json"

NATIVE_EXTS = {".pdf", ".hwp", ".hwpx", ".docx"}
OFFICE_EXTS = {".ppt", ".pptx", ".doc", ".xls", ".xlsx", ".rtf"}
SKIP_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".zip", ".mp4", ".avi"}
# 이 글자 수 미만이면 "추출은 됐지만 알맹이가 없다"로 본다(스캔본·표지만 등).
THIN_TEXT_CHARS = 200


def unzip_all(rows: list[dict], limit: int | None) -> list[dict]:
    """ZIP 첨부를 전개하고 내부 파일을 가상 첨부 레코드로 반환."""
    extra: list[dict] = []
    zips = [r for r in rows if r["path"].suffix.lower() == ".zip"]
    for index, row in enumerate(zips, 1):
        target = row["path"].parent / "_unzipped" / str(row["document_id"])
        try:
            with zipfile.ZipFile(row["path"]) as archive:
                names = [n for n in archive.namelist() if not n.endswith("/")]
                target.mkdir(parents=True, exist_ok=True)
                for name in names:
                    # 경로 탈출 방지 + 한글 파일명 복원(CP437 로 잘못 디코딩된 경우)
                    safe = Path(name).name
                    try:
                        safe = safe.encode("cp437").decode("cp949")
                    except (UnicodeEncodeError, UnicodeDecodeError):
                        pass
                    if not safe:
                        continue
                    destination = target / safe
                    if not destination.exists():
                        with archive.open(name) as src, destination.open("wb") as dst:
                            shutil.copyfileobj(src, dst)
                    extra.append(
                        {
                            "document_id": row["document_id"],
                            "priority": row["priority"],
                            "path": destination,
                            "origin": "zip",
                            "parent": str(row["path"].relative_to(REPO_ROOT)),
                        }
                    )
        except Exception as exc:  # noqa: BLE001
            print(f"  [ZIP ERR] {row['path'].name}: {exc}", flush=True)
        if index % 25 == 0:
            print(f"  ZIP 전개 {index}/{len(zips)}", flush=True)
        if limit is not None and len(extra) >= limit:
            break
    return extra


def office_to_text(path: Path) -> tuple[str, str]:
    """LibreOffice headless 로 변환 후 텍스트 추출. (텍스트, 사용방법)"""
    if shutil.which("soffice") is None:
        return "", "soffice_missing"
    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess.run(
            ["soffice", "--headless", "--norestore", "--convert-to", "pdf",
             "--outdir", tmp, str(path)],
            check=False, capture_output=True, text=True, timeout=180,
        )
        produced = list(Path(tmp).glob("*.pdf"))
        if result.returncode != 0 or not produced:
            return "", "soffice_failed"
        return extract_text(produced[0]), "soffice_pdf"


def extract_one(path: Path) -> tuple[str, str, str]:
    """(텍스트, 방법, 상태) 반환."""
    suffix = path.suffix.lower()
    if suffix in SKIP_EXTS:
        return "", "skip", "unsupported_binary"
    if suffix in NATIVE_EXTS:
        try:
            text = extract_text(path)
        except Exception as exc:  # noqa: BLE001
            return "", "native", f"error:{type(exc).__name__}"
        if text.strip():
            return text, "native", "ok"
        return "", "native", "empty"
    if suffix in OFFICE_EXTS:
        try:
            text, method = office_to_text(path)
        except subprocess.TimeoutExpired:
            return "", "soffice", "timeout"
        except Exception as exc:  # noqa: BLE001
            return "", "soffice", f"error:{type(exc).__name__}"
        if text.strip():
            return text, method, "ok"
        return "", method, "empty"
    return "", "none", "unknown_ext"


def main() -> int:
    parser = argparse.ArgumentParser(prog="acct_extract_audit")
    _ = parser.add_argument("--limit", type=int, default=None)
    _ = parser.add_argument("--skip-unzip", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = [
        {
            "document_id": r["document_id"],
            "priority": r["source_priority"],
            "path": REPO_ROOT / r["local_path"],
            "origin": "attachment",
            "parent": "",
        }
        for r in conn.execute(
            "SELECT a.document_id, a.local_path, d.source_priority "
            "FROM acct_attachments a JOIN acct_documents d ON d.id = a.document_id "
            "WHERE d.source_priority >= 18"
        )
    ]
    rows = [r for r in rows if r["path"].exists()]
    print(f"[start] 첨부 {len(rows)}건", flush=True)

    if not args.skip_unzip:
        print("=== ZIP 전개 ===", flush=True)
        inner = unzip_all(rows, None)
        print(f"  내부 파일 {len(inner)}건 편입", flush=True)
        rows.extend(inner)

    if args.limit:
        rows = rows[: args.limit]

    print(f"\n=== 텍스트 추출 {len(rows)}건 ===", flush=True)
    results: list[dict] = []
    started = time.time()
    for index, row in enumerate(rows, 1):
        text, method, status = extract_one(row["path"])
        chars = len(text.strip())
        if status == "ok" and chars < THIN_TEXT_CHARS:
            status = "thin"
        if text.strip():
            out_dir = TEXT_ROOT / f"{row['priority']:02d}"
            out_dir.mkdir(parents=True, exist_ok=True)
            stem = f"{row['document_id']}_{row['path'].stem}"[:180]
            _ = (out_dir / f"{stem}.txt").write_text(text, encoding="utf-8")
        results.append(
            {
                "document_id": row["document_id"],
                "priority": row["priority"],
                "path": str(row["path"].relative_to(REPO_ROOT)),
                "ext": row["path"].suffix.lower(),
                "origin": row["origin"],
                "parent": row["parent"],
                "method": method,
                "status": status,
                "chars": chars,
            }
        )
        if index % 100 == 0:
            print(f"  {index}/{len(rows)} ({(time.time() - started) / 60:.1f}분)", flush=True)

    _ = AUDIT_PATH.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in results), encoding="utf-8"
    )

    by_status = Counter(r["status"] for r in results)
    by_ext = Counter(r["ext"] for r in results)
    ok_by_ext = Counter(r["ext"] for r in results if r["status"] == "ok")
    summary = {
        "files_total": len(results),
        "chars_total": sum(r["chars"] for r in results),
        "by_status": dict(by_status),
        "by_ext": {
            ext: {"total": count, "ok": ok_by_ext.get(ext, 0)}
            for ext, count in by_ext.most_common()
        },
        "docs_with_no_text": len(
            {r["document_id"] for r in results}
            - {r["document_id"] for r in results if r["status"] == "ok"}
        ),
    }
    _ = SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n[done] {(time.time() - started) / 60:.1f}분")
    print(f"  파일 {summary['files_total']} | 총 {summary['chars_total']:,}자")
    print(f"  상태: {summary['by_status']}")
    print("  포맷별 성공률:")
    for ext, value in summary["by_ext"].items():
        rate = value["ok"] / value["total"] * 100 if value["total"] else 0
        print(f"    {ext or '(확장자없음)':<8} {value['ok']:>4}/{value['total']:<4} {rate:5.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

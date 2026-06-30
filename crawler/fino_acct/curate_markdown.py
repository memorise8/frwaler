from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import shutil
from typing import Literal


QualityTier = Literal["include", "secondary", "quarantine", "exclude"]

ACCOUNTING_TERMS = (
    "회계",
    "외부감사",
    "감리",
    "재무제표",
    "K-IFRS",
    "IFRS",
    "감사인",
    "회계법인",
    "사업보고서",
    "증권선물위원회",
)
INCLUDE_PRIORITIES = {"2", "3"}
SECONDARY_PRIORITIES = {"5", "6", "9", "10", "13", "14", "15", "16", "17"}


@dataclass(frozen=True, slots=True)
class MarkdownDocument:
    path: Path
    title: str
    metadata: dict[str, str]
    body: str
    body_hash: str


@dataclass(frozen=True, slots=True)
class CurationRecord:
    source_markdown_path: str
    curated_markdown_path: str
    source_priority: str
    agency: str
    source_type: str
    source_subtype: str
    index_name: str
    title: str
    detail_url: str
    attachment_url: str
    body_hash: str
    body_chars: int
    quality_tier: str
    quality_flags: list[str]
    exclude_reason: str
    canonical_id: str
    duplicate_group_id: str
    alias_count: int


def parse_markdown_document(path: Path) -> MarkdownDocument:
    text = path.read_text(encoding="utf-8", errors="ignore")
    metadata, body = _split_markdown(text)
    title = metadata.get("title", path.stem)
    normalized_body = _normalize_body(body)
    return MarkdownDocument(
        path=path,
        title=title,
        metadata=metadata,
        body=normalized_body,
        body_hash=hashlib.sha256(normalized_body.encode("utf-8")).hexdigest(),
    )


def quality_tier_for(*, source_priority: str, body: str, title: str) -> QualityTier:
    clean_body = _normalize_body(body)
    if len(clean_body) < 200:
        return "quarantine"
    if source_priority == "1":
        return "quarantine"
    if source_priority == "8" and not _is_accounting_relevant(f"{title}\n{clean_body}"):
        return "exclude"
    if source_priority in INCLUDE_PRIORITIES:
        return "include"
    if source_priority in SECONDARY_PRIORITIES:
        return "secondary"
    return "exclude"


def curate_markdown(*, input_dir: Path, output_root: Path, run_id: str) -> dict[str, int]:
    run_dir = output_root / run_id
    documents = [parse_markdown_document(path) for path in sorted((input_dir / "documents").glob("**/*.md"))]
    canonical_by_hash: dict[str, MarkdownDocument] = {}
    aliases: list[tuple[MarkdownDocument, MarkdownDocument]] = []
    for document in documents:
        current = canonical_by_hash.get(document.body_hash)
        if current is None:
            canonical_by_hash[document.body_hash] = document
            continue
        winner = _choose_canonical(current, document)
        loser = document if winner == current else current
        canonical_by_hash[document.body_hash] = winner
        aliases.append((loser, winner))

    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "documents" / "include").mkdir(parents=True)
    (run_dir / "documents" / "secondary").mkdir(parents=True)

    records = [_record_for(document, run_dir) for document in sorted(canonical_by_hash.values(), key=lambda item: str(item.path))]
    alias_records = [_alias_record(loser, winner) for loser, winner in aliases]
    _write_jsonl(run_dir / "curated_manifest.jsonl", [record for record in records if record.quality_tier in {"include", "secondary"}])
    _write_jsonl(run_dir / "quarantine_manifest.jsonl", [record for record in records if record.quality_tier == "quarantine"])
    _write_jsonl(run_dir / "excluded_manifest.jsonl", [record for record in records if record.quality_tier == "exclude"])
    _write_jsonl(run_dir / "duplicate_aliases.jsonl", alias_records)
    _copy_curated_documents(records)
    summary = _summary(records, alias_records)
    _ = (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _ = (run_dir / "quality_report.md").write_text(_quality_report(summary), encoding="utf-8")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m crawler.fino_acct.curate_markdown")
    _ = parser.add_argument("--input-dir", type=Path, required=True)
    _ = parser.add_argument("--output-root", type=Path, required=True)
    _ = parser.add_argument("--run-id", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    summary = curate_markdown(input_dir=args.input_dir, output_root=args.output_root, run_id=args.run_id)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _split_markdown(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text.strip()
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text.strip()
    metadata: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip('"')
    return metadata, parts[2].strip()


def _normalize_body(value: str) -> str:
    lines = [" ".join(line.split()) for line in value.replace("\r", "\n").splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _is_accounting_relevant(value: str) -> bool:
    return any(term in value for term in ACCOUNTING_TERMS)


def _tier_rank(document: MarkdownDocument) -> int:
    tier = quality_tier_for(
        source_priority=document.metadata.get("source_priority", ""),
        body=document.body,
        title=document.title,
    )
    match tier:
        case "include":
            return 0
        case "secondary":
            return 1
        case "quarantine":
            return 2
        case "exclude":
            return 3


def _choose_canonical(left: MarkdownDocument, right: MarkdownDocument) -> MarkdownDocument:
    return min(
        (left, right),
        key=lambda document: (_tier_rank(document), -len(document.body), document.metadata.get("source_priority", ""), str(document.path)),
    )


def _record_for(document: MarkdownDocument, run_dir: Path) -> CurationRecord:
    priority = document.metadata.get("source_priority", "")
    tier = quality_tier_for(source_priority=priority, body=document.body, title=document.title)
    flags = _quality_flags(priority=priority, body=document.body)
    target = _target_path(document, run_dir, tier)
    return CurationRecord(
        source_markdown_path=str(document.path),
        curated_markdown_path=str(target) if target is not None else "",
        source_priority=priority,
        agency=document.metadata.get("agency", ""),
        source_type=document.metadata.get("source_type", ""),
        source_subtype=document.metadata.get("source_subtype", ""),
        index_name=document.metadata.get("index_name", ""),
        title=document.title,
        detail_url=document.metadata.get("detail_url", ""),
        attachment_url=document.metadata.get("attachment_url", ""),
        body_hash=document.body_hash,
        body_chars=len(document.body),
        quality_tier=tier,
        quality_flags=flags,
        exclude_reason=";".join(flags) if tier in {"exclude", "quarantine"} else "",
        canonical_id=document.body_hash[:16],
        duplicate_group_id=document.body_hash[:16],
        alias_count=0,
    )


def _alias_record(loser: MarkdownDocument, winner: MarkdownDocument) -> CurationRecord:
    record = _record_for(loser, Path("/"))
    return CurationRecord(
        source_markdown_path=record.source_markdown_path,
        curated_markdown_path=str(winner.path),
        source_priority=record.source_priority,
        agency=record.agency,
        source_type=record.source_type,
        source_subtype=record.source_subtype,
        index_name=record.index_name,
        title=record.title,
        detail_url=record.detail_url,
        attachment_url=record.attachment_url,
        body_hash=record.body_hash,
        body_chars=record.body_chars,
        quality_tier="duplicate_alias",
        quality_flags=record.quality_flags,
        exclude_reason=record.exclude_reason,
        canonical_id=winner.body_hash[:16],
        duplicate_group_id=record.duplicate_group_id,
        alias_count=1,
    )


def _quality_flags(*, priority: str, body: str) -> list[str]:
    flags: list[str] = []
    if len(body) < 200:
        flags.append("short_body")
    if priority == "1":
        flags.append("kasb_priority_01_quarantine")
    if priority == "8" and not _is_accounting_relevant(body):
        flags.append("fsc_press_release_not_accounting_relevant")
    return flags


def _target_path(document: MarkdownDocument, run_dir: Path, tier: QualityTier) -> Path | None:
    if tier not in {"include", "secondary"}:
        return None
    return run_dir / "documents" / tier / document.path.name


def _copy_curated_documents(records: list[CurationRecord]) -> None:
    for record in records:
        if not record.curated_markdown_path:
            continue
        _ = shutil.copy2(record.source_markdown_path, record.curated_markdown_path)


def _write_jsonl(path: Path, rows: list[CurationRecord]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            _ = handle.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")


def _summary(records: list[CurationRecord], aliases: list[CurationRecord]) -> dict[str, int]:
    counts = Counter(record.quality_tier for record in records)
    return {
        "raw_documents": len(records) + len(aliases),
        "include": counts["include"],
        "secondary": counts["secondary"],
        "quarantine": counts["quarantine"],
        "exclude": counts["exclude"],
        "duplicate_aliases": len(aliases),
    }


def _quality_report(summary: dict[str, int]) -> str:
    rows = "\n".join(f"- {key}: {value}" for key, value in summary.items())
    return f"# Accounting Corpus Quality Report\n\n{rows}\n"


if __name__ == "__main__":
    raise SystemExit(main())

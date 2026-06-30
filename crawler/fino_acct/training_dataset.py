from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import re

from .training_models import (
    DEFAULT_GENERIC_DB,
    DEFAULT_KICPA_ROOT,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_PUBLIC_MANIFEST,
    DEFAULT_WORKSPACE_ROOT,
    MAX_CHUNK_CHARS,
    MIN_TEXT_CHARS,
    LoadEvent,
    SourceDocument,
    SummaryValue,
    TrainingChunk,
    TrainingPair,
)
from .training_report import build_report_html
from .training_sources import (
    load_generic_paper_documents,
    load_kicpa_documents,
    load_public_markdown_documents,
    normalize_text,
)


JsonlRows = list[SourceDocument] | list[LoadEvent] | list[TrainingChunk] | list[TrainingPair]


def chunk_document(*, source_id: str, source_key: str, title: str, text: str, max_chars: int, min_chars: int) -> list[TrainingChunk]:
    chunks: list[TrainingChunk] = []
    for index, content in enumerate(_paragraph_chunks(normalize_text(text), max_chars), start=1):
        if len(content) < min_chars:
            continue
        chunks.append(
            TrainingChunk(
                chunk_id=f"{source_id}:{index:04d}",
                source_id=source_id,
                source_key=source_key,
                source_name="",
                title=title,
                content=content,
                url="",
                access_type="",
            )
        )
    return chunks


def chunk_documents(documents: list[SourceDocument]) -> list[TrainingChunk]:
    chunks: list[TrainingChunk] = []
    for document in documents:
        source_chunks = chunk_document(
            source_id=document.source_id,
            source_key=document.source_key,
            title=document.title,
            text=document.text,
            max_chars=MAX_CHUNK_CHARS,
            min_chars=MIN_TEXT_CHARS,
        )
        chunks.extend(_with_document_metadata(chunk, document) for chunk in source_chunks)
    return chunks


def make_training_pairs(chunks: list[TrainingChunk]) -> list[TrainingPair]:
    by_domain: dict[str, list[TrainingChunk]] = {}
    for chunk in chunks:
        by_domain.setdefault(chunk.source_key, []).append(chunk)
    pairs: list[TrainingPair] = []
    for chunk in chunks:
        negative = _find_negative(chunk, by_domain)
        if negative is None:
            continue
        pairs.append(
            TrainingPair(
                id=f"pair:{chunk.chunk_id}",
                anchor=_anchor_for(chunk),
                positive=chunk.content,
                hard_negatives=[negative.content],
                domain=chunk.source_key,
                bucket="template_synthetic_with_hard_negative",
                mix_source="fsc_fss_kicpa_pipeline",
                generator_tag="template-synth-v1",
                positive_chunk_id=chunk.chunk_id,
                hard_negative_chunk_id=negative.chunk_id,
            )
        )
    return pairs


def build_dataset(public_manifest: Path, generic_db: Path, kicpa_root: Path, output_dir: Path) -> dict[str, SummaryValue]:
    output_dir.mkdir(parents=True, exist_ok=True)
    documents, events = load_all_documents(public_manifest, generic_db, kicpa_root)
    chunks = chunk_documents(documents)
    pairs = make_training_pairs(chunks)
    _write_jsonl(output_dir / "documents.jsonl", documents)
    _write_jsonl(output_dir / "load_events.jsonl", events)
    _write_jsonl(output_dir / "chunks.jsonl", chunks)
    _write_jsonl(output_dir / "train_pairs.jsonl", pairs)
    summary = summarize(documents, events, chunks, pairs)
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "report.html").write_text(build_report_html(summary), encoding="utf-8")
    return summary


def load_all_documents(public_manifest: Path, generic_db: Path, kicpa_root: Path) -> tuple[list[SourceDocument], list[LoadEvent]]:
    public_docs, public_events = load_public_markdown_documents(public_manifest)
    paper_docs, paper_events = load_generic_paper_documents(generic_db)
    kicpa_docs, kicpa_events = load_kicpa_documents(kicpa_root, workspace_root=DEFAULT_WORKSPACE_ROOT)
    return public_docs + paper_docs + kicpa_docs, public_events + paper_events + kicpa_events


def summarize(
    documents: list[SourceDocument],
    events: list[LoadEvent],
    chunks: list[TrainingChunk],
    pairs: list[TrainingPair],
) -> dict[str, SummaryValue]:
    return {
        "documents": len(documents),
        "load_events": len(events),
        "chunks": len(chunks),
        "train_pairs": len(pairs),
        "documents_by_source": dict(Counter(document.source_key for document in documents)),
        "events_by_status": dict(Counter(event.status for event in events)),
        "chunks_by_source": dict(Counter(chunk.source_key for chunk in chunks)),
        "pairs_by_source": dict(Counter(pair.domain for pair in pairs)),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m crawler.fino_acct.training_dataset")
    parser.add_argument("--public-manifest", type=Path, default=DEFAULT_PUBLIC_MANIFEST)
    parser.add_argument("--generic-db", type=Path, default=DEFAULT_GENERIC_DB)
    parser.add_argument("--kicpa-root", type=Path, default=DEFAULT_KICPA_ROOT)
    default_output = DEFAULT_OUTPUT_ROOT / f"fsc_fss_kicpa_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    parser.add_argument("--output-dir", type=Path, default=default_output)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    summary = build_dataset(args.public_manifest, args.generic_db, args.kicpa_root, args.output_dir)
    print(f"output_dir={args.output_dir}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _paragraph_chunks(text: str, max_chars: int) -> list[str]:
    paragraphs = re.split(r"\n{2,}|\n(?=[가-힣A-Za-z0-9])", text)
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        clean = paragraph.strip()
        if not clean:
            continue
        if len(clean) > max_chars:
            chunks.extend(clean[start : start + max_chars] for start in range(0, len(clean), max_chars))
            continue
        next_value = f"{current}\n{clean}".strip()
        if len(next_value) <= max_chars:
            current = next_value
            continue
        if current:
            chunks.append(current)
        current = clean
    if current:
        chunks.append(current)
    return chunks


def _with_document_metadata(chunk: TrainingChunk, document: SourceDocument) -> TrainingChunk:
    return TrainingChunk(
        chunk_id=chunk.chunk_id,
        source_id=document.source_id,
        source_key=document.source_key,
        source_name=document.source_name,
        title=document.title,
        content=chunk.content,
        url=document.url,
        access_type=document.access_type,
    )


def _find_negative(chunk: TrainingChunk, by_domain: dict[str, list[TrainingChunk]]) -> TrainingChunk | None:
    for candidate in by_domain.get(chunk.source_key, []):
        if candidate.source_id != chunk.source_id:
            return candidate
    for domain_chunks in by_domain.values():
        for candidate in domain_chunks:
            if candidate.source_id != chunk.source_id:
                return candidate
    return None


def _anchor_for(chunk: TrainingChunk) -> str:
    topic = chunk.title.replace(".pdf", "").replace(".hwp", "").replace(".hwpx", "").strip()
    match chunk.source_key.split(":", 1)[0]:
        case "kicpa":
            return f"{topic}에 대해 실무자가 확인해야 할 핵심 회계·세무 쟁점은 무엇인가?"
        case "public":
            return f"{topic} 자료에서 확인할 수 있는 감독·정책상 핵심 내용은 무엇인가?"
        case unreachable:
            return f"{unreachable} {topic}의 핵심 내용은 무엇인가?"


def _write_jsonl(path: Path, rows: JsonlRows) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final


DEFAULT_PUBLIC_MANIFEST: Final[Path] = Path("/data_raid/ruci_workspace/acct_rag_data/_manifest.jsonl")
DEFAULT_GENERIC_DB: Final[Path] = Path("/data_raid/ruci_workspace/frwaler/data/data.db")
DEFAULT_KICPA_ROOT: Final[Path] = Path("/data_raid/ruci_workspace/frwaler/data/kicpa_member_20260613_085148")
DEFAULT_WORKSPACE_ROOT: Final[Path] = Path("/data_raid/ruci_workspace/frwaler")
DEFAULT_OUTPUT_ROOT: Final[Path] = Path("/data_raid/ruci_workspace/embedding-fino/synthetic_training")
MIN_TEXT_CHARS: Final[int] = 300
MAX_CHUNK_CHARS: Final[int] = 1800
SummaryValue = int | dict[str, int]


@dataclass(frozen=True, slots=True)
class SourceDocument:
    source_id: str
    source_key: str
    source_name: str
    title: str
    url: str
    text: str
    access_type: str


@dataclass(frozen=True, slots=True)
class LoadEvent:
    source_key: str
    title: str
    path: str
    status: str
    chars: int


@dataclass(frozen=True, slots=True)
class TrainingChunk:
    chunk_id: str
    source_id: str
    source_key: str
    source_name: str
    title: str
    content: str
    url: str
    access_type: str


@dataclass(frozen=True, slots=True)
class TrainingPair:
    id: str
    anchor: str
    positive: str
    hard_negatives: list[str]
    domain: str
    bucket: str
    mix_source: str
    generator_tag: str
    positive_chunk_id: str
    hard_negative_chunk_id: str

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class TargetKind(StrEnum):
    LIST = "list"
    DETAIL = "detail"
    META = "meta"


@dataclass(frozen=True, slots=True)
class Target:
    priority: int
    category: str
    agency: str
    target_name: str
    url: str
    method: str
    index_name: str
    source_type: str
    source_subtype: str
    note: str
    kind: TargetKind


@dataclass(frozen=True, slots=True)
class AttachmentLink:
    url: str
    filename: str
    method: str = "GET"
    post_file_no: str = ""
    post_file_seq: str = ""


@dataclass(frozen=True, slots=True)
class ExtractedLinks:
    details: tuple[str, ...]
    attachments: tuple[AttachmentLink, ...]


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str
    status_code: int
    content_type: str
    content: bytes


@dataclass(frozen=True, slots=True)
class DownloadResult:
    local_path: Path | None
    content_type: str
    size_bytes: int
    status: str

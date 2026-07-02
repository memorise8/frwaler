from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StdTarget:
    std_num: int
    title: str
    std_type: str  # kifrs / kifrs_interp / kifrs_etc / gaap


@dataclass(frozen=True, slots=True)
class Section:
    document_id: str
    title: str
    ref: str


@dataclass(frozen=True, slots=True)
class ParagraphRecord:
    para_num: str        # "1", "한10.1", "82A", "BC1", "IG7", 없으면 ""
    section_path: str    # "재무제표 > 일반사항 > 계속기업"
    body_html: str
    body_text: str
    seq: int
    source_url: str = ""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Revision:
    seq: int
    tax_category: str
    summary: str
    reason: str
    revision_reason: str
    registered_at: str
    keep_cases: list[tuple[str, str]]    # (번호, 날짜)
    delete_cases: list[tuple[str, str]]
    source_file: str

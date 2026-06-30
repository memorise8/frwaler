from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class LawTarget:
    name: str       # 법령명 정확명 (예: "법인세법")
    category: str   # 법률 / 시행령 / 시행규칙
    tax_scope: str  # 국세 / 지방세


@dataclass(frozen=True, slots=True)
class ArticleRecord:
    article_no: str
    article_title: str
    body_text: str
    clause_json: str
    seq: int
    source_url: str = ""


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    source_kind: str
    external_id: str
    title: str
    category: str
    org: str
    promulgated_at: str
    effective_at: str
    version_code: str
    source_url: str
    articles: tuple[ArticleRecord, ...] = field(default_factory=tuple)

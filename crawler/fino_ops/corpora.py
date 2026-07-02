from __future__ import annotations

from dataclasses import dataclass, field
import os
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PAPERS_DB = Path(os.environ.get(
    "FINO_PAPERS_DB", str(REPO_ROOT / "data" / "fino_nts.db")))  # Task 2 분리 산출물
_PY = str(REPO_ROOT / ".venv" / "bin" / "python")


@dataclass(frozen=True)
class Corpus:
    key: str
    label: str
    db_path: Path
    count_sql: str
    freshness_sql: str
    argv: tuple
    env: dict = field(default_factory=dict)


def _nts(key: str, label: str, site_id: str) -> Corpus:
    return Corpus(
        key=key, label=label, db_path=PAPERS_DB,
        count_sql=f"SELECT COUNT(*) FROM papers WHERE site_id='{site_id}'",
        freshness_sql=f"SELECT MAX(crawled_at) FROM papers WHERE site_id='{site_id}'",
        argv=(_PY, "-m", "crawler.main", "crawl", site_id, "--incremental"),
        env={"FINOLAW_DB_PATH": str(PAPERS_DB)},
    )


CORPORA: dict[str, Corpus] = {
    "law": Corpus(
        key="law", label="세법 법령", db_path=REPO_ROOT / "data" / "fino_law.db",
        count_sql=("SELECT COUNT(*) FROM articles a JOIN documents d ON d.id=a.document_id "
                   "WHERE d.source_kind='law'"),
        freshness_sql="SELECT MAX(collected_at) FROM documents WHERE source_kind='law'",
        argv=(_PY, "-m", "crawler.fino_law.collect", "--law"),
    ),
    "exec": Corpus(
        key="exec", label="세법 집행기준", db_path=REPO_ROOT / "data" / "fino_law.db",
        count_sql=("SELECT COUNT(*) FROM articles a JOIN documents d ON d.id=a.document_id "
                   "WHERE d.source_kind='exec_standard'"),
        freshness_sql="SELECT MAX(collected_at) FROM documents WHERE source_kind='exec_standard'",
        argv=(_PY, "-m", "crawler.fino_law.collect", "--exec"),
    ),
    "acct": Corpus(
        key="acct", label="회계 질의회신", db_path=REPO_ROOT / "data" / "fino_acct.db",
        count_sql="SELECT COUNT(*) FROM acct_documents",
        freshness_sql="SELECT MAX(fetched_at) FROM acct_documents",
        argv=(_PY, "-m", "crawler.fino_acct.collect", "--priorities", "1,2,3,4,5,6"),
    ),
    "std": Corpus(
        key="std", label="회계 기준서(K-IFRS/GAAP)", db_path=REPO_ROOT / "data" / "fino_std.db",
        count_sql="SELECT COUNT(*) FROM paragraphs",
        freshness_sql="SELECT MAX(collected_at) FROM documents",
        argv=(_PY, "-m", "crawler.fino_std.collect"),
    ),
    "nts_qt": _nts("nts_qt", "NTS 예규·질의", "nts-taxlaw-qt"),
    "nts_pd": _nts("nts_pd", "NTS 판례", "nts-taxlaw-pd"),
}


def corpus_stats(c: Corpus) -> dict:
    if not c.db_path.exists():
        return {"total": None, "last_collected": None, "db_exists": False}
    conn = sqlite3.connect(f"file:{c.db_path}?mode=ro", uri=True)
    try:
        total = conn.execute(c.count_sql).fetchone()[0]
        last = conn.execute(c.freshness_sql).fetchone()[0]
    finally:
        conn.close()
    return {"total": total, "last_collected": last, "db_exists": True}

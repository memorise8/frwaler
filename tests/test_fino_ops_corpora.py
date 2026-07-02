import sqlite3
from pathlib import Path

from crawler.fino_ops.corpora import CORPORA, Corpus, corpus_stats


def test_registry_has_six_corpora_with_expected_wiring() -> None:
    assert set(CORPORA) == {"law", "exec", "acct", "std", "nts_qt", "nts_pd"}
    assert "--law" in CORPORA["law"].argv and "--exec" in CORPORA["exec"].argv
    assert "--incremental" in CORPORA["nts_qt"].argv
    assert "FINOLAW_DB_PATH" in CORPORA["nts_pd"].env       # papers.db 주입
    assert CORPORA["std"].argv[-1].endswith("collect")


def _mini_std_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE documents (id INTEGER PRIMARY KEY, collected_at TEXT);
        CREATE TABLE paragraphs (id INTEGER PRIMARY KEY, document_id INTEGER);
        INSERT INTO documents VALUES (1, '2026-07-02 10:00:00');
        INSERT INTO paragraphs VALUES (1, 1), (2, 1), (3, 1);
        """
    )
    conn.commit()
    conn.close()


def test_corpus_stats_reads_total_and_freshness(tmp_path: Path) -> None:
    db = tmp_path / "mini.db"
    _mini_std_db(db)
    c = Corpus(
        key="std", label="테스트", db_path=db,
        count_sql="SELECT COUNT(*) FROM paragraphs",
        freshness_sql="SELECT MAX(collected_at) FROM documents",
        argv=("true",),
    )
    s = corpus_stats(c)
    assert s == {"total": 3, "last_collected": "2026-07-02 10:00:00", "db_exists": True}


def test_corpus_stats_missing_db(tmp_path: Path) -> None:
    c = Corpus(key="x", label="없음", db_path=tmp_path / "nope.db",
               count_sql="SELECT 1", freshness_sql="SELECT 1", argv=("true",))
    assert corpus_stats(c) == {"total": None, "last_collected": None, "db_exists": False}

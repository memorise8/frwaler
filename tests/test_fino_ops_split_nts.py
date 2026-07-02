import sqlite3
from pathlib import Path

import pytest

from crawler.fino_ops.split_nts import NTS_SITE_IDS, split_nts


def _make_src(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sites (id TEXT PRIMARY KEY, name TEXT, base_url TEXT);
        CREATE TABLE papers (
            id TEXT PRIMARY KEY, site_id TEXT NOT NULL, external_id TEXT,
            title TEXT, abstract TEXT, metadata TEXT
        );
        CREATE INDEX idx_papers_site ON papers(site_id);
        CREATE TABLE doc_index (site_id TEXT, doc_id TEXT, title TEXT);
        CREATE VIRTUAL TABLE papers_fts USING fts5(
            title, abstract, metadata, content='papers', content_rowid='rowid');
        CREATE TRIGGER papers_ai AFTER INSERT ON papers BEGIN
            INSERT INTO papers_fts(rowid, title, abstract, metadata)
            VALUES (new.rowid, new.title, new.abstract, new.metadata);
        END;
        INSERT INTO sites VALUES ('nts-taxlaw-pd','판례',''),('nts-taxlaw-qt','예규',''),('mohw','복지부','');
        INSERT INTO papers VALUES
            ('a','nts-taxlaw-pd','1','판례 하나','본문A','{}'),
            ('b','nts-taxlaw-qt','2','예규 하나','본문B','{}'),
            ('c','mohw','3','무관 문서','본문C','{}');
        INSERT INTO doc_index VALUES ('nts-taxlaw-pd','1','판례 하나'),('mohw','3','무관');
        """
    )
    conn.commit()
    conn.close()


def test_split_copies_only_nts_and_rebuilds_fts(tmp_path: Path) -> None:
    src = tmp_path / "papers.db"
    dest = tmp_path / "fino_nts.db"
    _make_src(src)
    counts = split_nts(src, dest)
    assert counts == {"nts-taxlaw-pd": 1, "nts-taxlaw-qt": 1}
    conn = sqlite3.connect(dest)
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM papers WHERE site_id='mohw'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM doc_index").fetchone()[0] == 1
    assert {r[0] for r in conn.execute("SELECT id FROM sites")} == set(NTS_SITE_IDS)
    assert len(conn.execute("SELECT title FROM papers_fts WHERE papers_fts MATCH '판례'").fetchall()) == 1
    # 트리거 생존: 분리 후 신규 insert도 FTS 반영
    conn.execute("INSERT INTO papers VALUES ('d','nts-taxlaw-pd','4','신규 판례','본문D','{}')")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM papers_fts WHERE papers_fts MATCH '신규'").fetchone()[0] == 1
    # 소스 무수정
    s = sqlite3.connect(src)
    assert s.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 3


def test_split_refuses_overwrite(tmp_path: Path) -> None:
    src = tmp_path / "papers.db"
    dest = tmp_path / "fino_nts.db"
    _make_src(src)
    dest.write_text("existing")
    with pytest.raises(SystemExit):
        split_nts(src, dest)

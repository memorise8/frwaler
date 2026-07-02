import json
from pathlib import Path

import crawler.fino_std.collect as collect_mod
from crawler.fino_std.collect import collect_standards
from crawler.fino_std.db import connect_db

_FX = Path("tests/fixtures/fino_std")


def _install_fake_fetch(monkeypatch) -> None:
    titles = json.load(open(_FX / "title_1001.json"))
    content = json.load(open(_FX / "content_1001_1f0730.json"))

    def fake_titles(client, std_num, delay=0.4):
        return titles if std_num == 1001 else None  # 1001 외 전부 미지원 시늉

    def fake_content(client, std_num, document_id, delay=0.4):
        return content

    monkeypatch.setattr(collect_mod, "fetch_titles", fake_titles)
    monkeypatch.setattr(collect_mod, "fetch_content", fake_content)


def test_collect_standards_end_to_end(tmp_path: Path, monkeypatch) -> None:
    _install_fake_fetch(monkeypatch)
    db = tmp_path / "std.db"
    docs, paras = collect_standards(db_path=db, types={"kifrs"}, std_num=None, delay=0)
    assert docs == 1                    # 1001만 성공, 나머지 kifrs는 skip
    assert paras == 10 * 7              # big 10개 × 픽스처 content 문단 7개
    with connect_db(db) as conn:
        row = conn.execute("SELECT std_type, title FROM documents WHERE std_num = 1001").fetchone()
        assert row["std_type"] == "kifrs" and row["title"] == "재무제표 표시"
        n = conn.execute("SELECT COUNT(*) AS c FROM paragraphs").fetchone()["c"]
        assert n == 70


def test_collect_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    _install_fake_fetch(monkeypatch)
    db = tmp_path / "std.db"
    collect_standards(db_path=db, types={"kifrs"}, std_num=1001, delay=0)
    collect_standards(db_path=db, types={"kifrs"}, std_num=1001, delay=0)  # 재실행
    with connect_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM documents").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) AS c FROM paragraphs").fetchone()["c"] == 70


def test_collect_single_std_filter(tmp_path: Path, monkeypatch) -> None:
    _install_fake_fetch(monkeypatch)
    db = tmp_path / "std.db"
    docs, _ = collect_standards(db_path=db, types={"kifrs", "gaap"}, std_num=1001, delay=0)
    assert docs == 1


def test_collect_preserves_existing_paragraphs_on_total_section_failure(tmp_path: Path, monkeypatch) -> None:
    _install_fake_fetch(monkeypatch)
    db = tmp_path / "std.db"
    collect_standards(db_path=db, types={"kifrs"}, std_num=1001, delay=0)  # 정상 수집(70문단)
    # 재수집: titles는 성공하지만 모든 섹션 content가 실패하는 상황
    monkeypatch.setattr(collect_mod, "fetch_content", lambda client, std_num, document_id, delay=0.4: None)
    docs, paras = collect_standards(db_path=db, types={"kifrs"}, std_num=1001, delay=0)
    assert docs == 0 and paras == 0                     # 수집으로 집계되지 않음
    with connect_db(db) as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM paragraphs").fetchone()["c"]
        assert n == 70                                  # 기존 문단 보존

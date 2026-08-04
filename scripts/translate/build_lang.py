# -*- coding: utf-8 -*-
"""외국어 문서의 원문 언어 감지 → document_lang 테이블 (재개 가능).

번역 대상(FOREIGN) 문서마다 초록(있으면)·없으면 제목으로 언어를 감지해 저장.
qwen 은 안전언어만, GPT 는 나머지를 담당하도록 필터링하는 데 사용.
"""
import sqlite3, sys
sys.path.insert(0, "/data_raid/ruci_workspace/frwaler_job")
from langdetect import detect, DetectorFactory
DetectorFactory.seed = 0
from scripts.translate.run_translation import FOREIGN

DB = "/data_raid/ruci_workspace/frwaler_job/libertree-app/data/libertree.db"
DDL = "CREATE TABLE IF NOT EXISTS document_lang (seq_id INTEGER PRIMARY KEY, lang TEXT NOT NULL)"


def main():
    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    con.execute(DDL); con.commit()
    done = {r[0] for r in con.execute("SELECT seq_id FROM document_lang")}
    rows = con.execute(
        f"SELECT seq_id, title, abstract FROM documents WHERE {FOREIGN}").fetchall()
    todo = [(s, ti, ab) for (s, ti, ab) in rows if s not in done]
    print(f"[lang] 대상 {len(todo):,} (기존 {len(done):,})", flush=True)
    n = 0
    for seq, ti, ab in todo:
        text = (ab or "").strip()
        if len(text) < 20:
            text = (ti or "").strip()
        try:
            lang = detect(text[:500]) if text else "??"
        except Exception:
            lang = "??"
        con.execute("INSERT OR IGNORE INTO document_lang(seq_id, lang) VALUES(?,?)", (seq, lang))
        n += 1
        if n % 5000 == 0:
            con.commit(); print(f"  {n:,}/{len(todo):,}", flush=True)
    con.commit()
    print("\n[lang] 언어 분포:")
    for lang, c in con.execute("SELECT lang, COUNT(*) FROM document_lang GROUP BY lang ORDER BY COUNT(*) DESC LIMIT 25"):
        print(f"  {lang}: {c:,}")
    con.close()


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""번역 배치 완료를 감지하면 전수 이상조사를 자동 실행 (상주 감시).

- document_translations 의 처리분(completed+failed)이 번역 대상 총수에 도달하면
  → scripts/audit/anomaly_survey.py 를 백그라운드로 기동.
- 프로세스가 아니라 DB 상태를 보므로 번역이 재시작돼도 견고함.

nohup 으로 상주 실행:
    nohup .venv-embed/bin/python scripts/translate/watch_then_survey.py \
          > scripts/translate/watch.log 2>&1 &
"""
from __future__ import annotations
import sqlite3, subprocess, sys, time

REPO = "/data_raid/ruci_workspace/frwaler_job"
sys.path.insert(0, REPO)
from scripts.translate.run_translation import FOREIGN, DESC_OK, LOCALE, MODEL_VERSION, PROMPT_VERSION  # noqa

DB = f"{REPO}/libertree-app/data/libertree.db"
POLL_S = 300


def main():
    con = sqlite3.connect(DB, timeout=60)
    q = con.cursor().execute
    nt = q(f"SELECT COUNT(*) FROM documents WHERE {FOREIGN}").fetchone()[0]
    nd = q(f"SELECT COUNT(*) FROM documents WHERE {FOREIGN} AND {DESC_OK}").fetchone()[0]
    target = nt + nd
    print(f"[watch] 번역 대상 총 {target:,} 도달 대기 (title {nt:,} + desc {nd:,})", flush=True)

    while True:
        processed = q(
            "SELECT COUNT(*) FROM document_translations "
            "WHERE state IN ('completed','failed') AND target_locale=? AND model_version=? AND prompt_version=?",
            (LOCALE, MODEL_VERSION, PROMPT_VERSION),
        ).fetchone()[0]
        pct = processed / target * 100 if target else 100
        print(f"[watch] 번역 처리 {processed:,}/{target:,} ({pct:.1f}%)", flush=True)
        if processed >= target:
            break
        time.sleep(POLL_S)
    con.close()

    print("[watch] 번역 완료 감지 → 전수 이상조사 기동", flush=True)
    log = open(f"{REPO}/scripts/audit/out/anomaly_survey.log", "w")
    subprocess.Popen(
        [f"{REPO}/.venv-embed/bin/python", f"{REPO}/scripts/audit/anomaly_survey.py"],
        cwd=REPO, stdout=log, stderr=subprocess.STDOUT,
    )
    print("[watch] 이상조사 기동 완료. 감시 종료.", flush=True)


if __name__ == "__main__":
    main()

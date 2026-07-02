from __future__ import annotations

import argparse

from .corpora import CORPORA, corpus_stats
from .db import DEFAULT_OPS_DB, connect_ops, init_ops_schema, last_run, mark_stale_running
from .runner import BusyError, refresh_corpus


def cmd_status() -> int:
    conn = connect_ops(DEFAULT_OPS_DB)
    init_ops_schema(conn)
    print(f"{'corpus':<8} {'total':>10}  {'last_collected':<20} {'last_run'}")
    for key, c in CORPORA.items():
        s = corpus_stats(c)
        lr = last_run(conn, key)
        lr_txt = f"{lr['status']}({lr['new_count']}) {lr['finished_at'] or ''}" if lr else "-"
        print(f"{key:<8} {str(s['total'] or '-'):>10}  {str(s['last_collected'] or '-'):<20} {lr_txt}")
    conn.close()
    return 0


def cmd_refresh(corpus: str | None, run_all: bool) -> int:
    keys = list(CORPORA) if run_all else [corpus]
    rc = 0
    for key in keys:
        if key not in CORPORA:
            print(f"알 수 없는 코퍼스: {key}")
            return 1
        try:
            rid = refresh_corpus(key)
        except BusyError as exc:
            print(f"[busy] {exc}")
            return 1
        conn = connect_ops(DEFAULT_OPS_DB)
        lr = last_run(conn, key)
        conn.close()
        print(f"[{key}] run#{rid} {lr['status']} new={lr['new_count']} total={lr['total_after']}")
        if lr["status"] != "ok":
            rc = 1
    return rc


def cmd_unlock() -> int:
    conn = connect_ops(DEFAULT_OPS_DB)
    init_ops_schema(conn)
    n = mark_stale_running(conn)
    conn.close()
    print(f"unlocked: running {n}건을 error(stale)로 정리")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m crawler.fino_ops")
    sub = p.add_subparsers(dest="cmd", required=True)
    _ = sub.add_parser("status")
    rp = sub.add_parser("refresh")
    _ = rp.add_argument("--corpus", type=str, default=None)
    _ = rp.add_argument("--all", action="store_true")
    _ = sub.add_parser("unlock")
    args = p.parse_args()
    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "unlock":
        return cmd_unlock()
    if not args.all and not args.corpus:
        p.error("refresh는 --corpus KEY 또는 --all 필요")
    return cmd_refresh(args.corpus, args.all)

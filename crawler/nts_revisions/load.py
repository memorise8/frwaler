from __future__ import annotations

import argparse
import json
from pathlib import Path

from .db import (
    connect_db, count_excluded, delete_by_source, init_schema, insert_case,
    insert_revision, iter_excluded, iter_history, upsert_excluded,
)
from .match import resolve_delete_case
from .models import Revision
from .parse import parse_workbook

DEFAULT_DB = Path("data/fino_nts.db")
DEFAULT_EXCLUDED = Path("data/export/nts_excluded_ids.ndjson")
DEFAULT_HISTORY = Path("data/export/nts_deletion_history.ndjson")


def load(conn, revisions: list[Revision]) -> dict:
    n_rev = n_case = 0
    try:
        if revisions:
            delete_by_source(conn, revisions[0].source_file, commit=False)   # 멱등: 해당 source 교체
        for rev in revisions:
            rid = insert_revision(conn, seq=rev.seq, tax_category=rev.tax_category,
                                  summary=rev.summary, revision_reason=rev.revision_reason,
                                  registered_at=rev.registered_at, source_file=rev.source_file,
                                  commit=False)
            n_rev += 1
            for number, date in rev.keep_cases:
                insert_case(conn, revision_id=rid, role="keep", case_number=number,
                            case_date=date, matched_doc_id=None, commit=False)
                n_case += 1
            for number, date in rev.delete_cases:
                matches = resolve_delete_case(conn, number, date)
                doc_id = matches[0][0] if matches else None
                insert_case(conn, revision_id=rid, role="delete", case_number=number,
                            case_date=date, matched_doc_id=doc_id, commit=False)
                n_case += 1
                for external_id, title in matches:
                    upsert_excluded(conn, external_id=external_id, revision_id=rid,
                                    doc_number=number, title=title,
                                    revision_reason=rev.revision_reason,
                                    registered_at=rev.registered_at, commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"revisions": n_rev, "cases": n_case, "excluded": count_excluded(conn)}


def export_excluded(conn, out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for r in iter_excluded(conn):
            fh.write(json.dumps({
                "external_id": r["external_id"], "doc_number": r["doc_number"],
                "title": r["title"], "revision_reason": r["revision_reason"],
                "registered_at": r["registered_at"],
            }, ensure_ascii=False) + "\n")
            n += 1
    return n


def export_history(conn, out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for r in iter_history(conn):
            fh.write(json.dumps(dict(r), ensure_ascii=False) + "\n")
            n += 1
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m crawler.nts_revisions.load")
    _ = p.add_argument("--xlsx", type=str, required=True)
    _ = p.add_argument("--db-path", type=Path, default=DEFAULT_DB)
    _ = p.add_argument("--export", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    source_file = Path(args.xlsx).name
    revisions = parse_workbook(args.xlsx, source_file)
    conn = connect_db(args.db_path)
    init_schema(conn)
    stats = load(conn, revisions)
    print(f"load: {stats}")
    if args.export:
        ne = export_excluded(conn, DEFAULT_EXCLUDED)
        nh = export_history(conn, DEFAULT_HISTORY)
        print(f"export: excluded={ne} history={nh}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

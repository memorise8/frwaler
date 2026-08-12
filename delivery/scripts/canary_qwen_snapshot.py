"""Guarded 100-document canary launcher for a disposable snapshot DB only."""
from __future__ import annotations
import argparse,os
from urllib.parse import urlparse

from crawler import db_pg
from delivery.translation.jobs import enqueue_targets

def main()->int:
    parser=argparse.ArgumentParser();parser.add_argument("--dsn",required=True);parser.add_argument("--limit",type=int,default=100)
    parser.add_argument("--lang");parser.add_argument("--site-id");args=parser.parse_args()
    if os.environ.get("QWEN_CANARY_CONFIRM")!="WRITE_TO_DISPOSABLE_SNAPSHOT":parser.error("confirmation token required")
    dbname=urlparse(args.dsn).path.lstrip("/").lower()
    if not any(word in dbname for word in ("snapshot","rehearsal","staging")):parser.error("target DB name must identify a snapshot")
    if args.limit!=100:parser.error("canary limit is fixed at 100")
    model=os.environ.get("TRANSLATION_INTERNAL_MODEL");endpoint=os.environ.get("TRANSLATION_INTERNAL_ENDPOINT")
    if not model or not endpoint:parser.error("internal model endpoint configuration required")
    conn=db_pg.open_db(args.dsn)
    try:
        result=enqueue_targets(conn,tasks=["title_translation","abstract_summary"],provider="internal",
          model_version=model,prompt_version="title-summary-ko-v1",lang=args.lang,site_id=args.site_id,
          limit=100,requested_by="snapshot-canary")
        print(f"snapshot canary batch={result['batch_id']} created={result['created']} existing={result['existing']}")
    finally:conn.close()
    return 0
if __name__=="__main__":raise SystemExit(main())

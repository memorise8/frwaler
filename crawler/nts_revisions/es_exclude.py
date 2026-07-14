"""ES delete-by-query exclusion for NTS 정비내역(삭제) 해석사례.

Reads the exclusion list produced by ``crawler.nts_revisions.load`` (export)
and removes the matching documents from the NTS Elasticsearch index(es) so the
deleted 해석사례 never surface in RAG search.

Safety:
- ``--dry-run`` (default) only counts matches; nothing is deleted.
- ``--apply`` performs the delete_by_query, then re-counts to verify 0 remain.
- The deleted docs remain in the source (``nts_index/nts_records.jsonl``) so a
  full reindex can restore them if ever needed.

Usage:
    .venv/bin/python -m crawler.nts_revisions.es_exclude              # dry-run, default indices
    .venv/bin/python -m crawler.nts_revisions.es_exclude --apply      # actually delete
    .venv/bin/python -m crawler.nts_revisions.es_exclude --index fino-nts-synthv4-v1 --apply

Credentials: ES_USER (default 'fino') and ES_PASSWORD are read from the
environment or a project-root .env file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EXCLUDE_FILE = REPO_ROOT / "data" / "export" / "nts_excluded_ids.ndjson"
DEFAULT_ES_URL = "http://localhost:9200"
# fino-nts-synthv4-v1 is the served index (alias fino-nts-current); fino-nts-v2
# is the parallel full copy. Both carry the same external_id docs.
DEFAULT_INDICES = ["fino-nts-synthv4-v1", "fino-nts-v2"]


def _load_dotenv(path: Path) -> None:
    """Populate os.environ from a .env file without overriding existing vars."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if key and key not in os.environ:
            os.environ[key] = val


def load_exclusion_ids(path: Path) -> list[str]:
    ids = {json.loads(line)["external_id"] for line in path.read_text().splitlines() if line.strip()}
    return sorted(ids)


def _client(es_url: str) -> httpx.Client:
    user = os.environ.get("ES_USER", "fino")
    password = os.environ.get("ES_PASSWORD")
    if not password:
        sys.exit("ES_PASSWORD not set (env or .env). Aborting.")
    return httpx.Client(base_url=es_url, auth=(user, password), timeout=120.0)


def count_matches(client: httpx.Client, index: str, ids: list[str]) -> int:
    body = {"query": {"terms": {"external_id": ids}}}
    r = client.post(f"/{index}/_count", json=body)
    r.raise_for_status()
    return r.json()["count"]


def delete_matches(client: httpx.Client, index: str, ids: list[str]) -> dict:
    body = {"query": {"terms": {"external_id": ids}}}
    r = client.post(
        f"/{index}/_delete_by_query",
        params={"refresh": "true", "conflicts": "proceed", "wait_for_completion": "true"},
        json=body,
    )
    r.raise_for_status()
    return r.json()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Exclude NTS 삭제 해석사례 from ES via delete_by_query")
    ap.add_argument("--exclude-file", type=Path, default=DEFAULT_EXCLUDE_FILE)
    ap.add_argument("--es-url", default=os.environ.get("ES_URL", DEFAULT_ES_URL))
    ap.add_argument("--index", action="append", dest="indices",
                    help="target index (repeatable); defaults to both full NTS indices")
    ap.add_argument("--apply", action="store_true", help="actually delete (default is dry-run count only)")
    args = ap.parse_args(argv)

    _load_dotenv(REPO_ROOT / ".env")
    indices = args.indices or DEFAULT_INDICES
    ids = load_exclusion_ids(args.exclude_file)
    print(f"Exclusion list: {len(ids)} external_ids ({args.exclude_file})")
    print(f"ES: {args.es_url}  indices: {indices}")
    print(f"Mode: {'APPLY (delete)' if args.apply else 'DRY-RUN (count only)'}\n")

    with _client(args.es_url) as client:
        for index in indices:
            before = count_matches(client, index, ids)
            if not args.apply:
                print(f"[dry-run] {index}: {before} docs would be deleted")
                continue
            result = delete_matches(client, index, ids)
            after = count_matches(client, index, ids)
            print(f"[apply]   {index}: deleted={result.get('deleted')} "
                  f"took={result.get('took')}ms failures={len(result.get('failures', []))} "
                  f"| remaining_matches={after}")
            if after != 0:
                print(f"  WARNING: {after} matches still present in {index}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

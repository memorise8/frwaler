"""CLI and optional periodic service for the delivered crawler catalogue."""
from __future__ import annotations

import argparse
import csv
import fcntl
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .inventory import inventory
from .probe import run_probe
from .results import atomic_json, report_dir, site_path, utc_now


def reusable_result(root, site, profile):
    """Resume only the same source and explicitly frozen execution profile.

    Failed attempts are retained too; a later targeted retry uses a new profile
    or omits resume rather than erasing the initial survey's failures.
    """
    from .results import STATES
    try:
        result=json.loads(site_path(root,site['site_id']).read_text())
        if not isinstance(result,dict):
            return None
        if (result.get('site_id')==site['site_id'] and result.get('source_hash')==site['source_hash']
                and result.get('probe_profile')==profile and result.get('state') in STATES
                and result.get('checked_at')):
            return result
    except (OSError,ValueError,TypeError):
        pass
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--audit", action="store_true", help="inspect all code; no network requests")
    selection.add_argument("--all", action="store_true", help="probe every delivered site")
    selection.add_argument("--all-registered", action="store_true", help="probe catalogue plus all additional registered sites")
    selection.add_argument("--site", action="append", help="probe one site (repeatable)")
    parser.add_argument("--report-dir", type=Path, default=report_dir())
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--delay", type=float, default=1)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--interval", type=int, default=0, help="seconds between scans; 0 runs once")
    parser.add_argument("--resume", action="store_true", help="retain completed attempts for the same frozen profile")
    parser.add_argument("--profile-id", help="identifier for frozen image, credentials and environment (no secret values)")
    args = parser.parse_args()
    if not (1 <= args.timeout <= 600 and 1 <= args.limit <= 10 and
            0.1 <= args.delay <= 10 and 1 <= args.workers <= 4 and
            (args.interval == 0 or args.interval >= 300)):
        parser.error("timeout 1..600, limit 1..10, delay 0.1..10, workers 1..4, interval 0 or >=300")
    if args.resume and (not args.profile_id or args.interval):
        parser.error('--resume requires --profile-id and a one-off scan')
    profile={'id':args.profile_id,'timeout':args.timeout,'limit':args.limit,'delay':args.delay}
    data = inventory(include_registry=args.all_registered or bool(args.site))
    args.report_dir.mkdir(parents=True, exist_ok=True)
    if args.audit:
        atomic_json(args.report_dir / "inventory.json", {**data, "checked_at": utc_now()})
        with (args.report_dir / "inventory.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(data["sites"][0]))
            writer.writeheader()
            writer.writerows(data["sites"])
        print(json.dumps(data["summary"], ensure_ascii=False))
        return 0 if all(r["code_status"] == "ready" for r in data["sites"]) else 1
    by_id = {site["site_id"]: site for site in data["sites"]}
    unknown = set(args.site or []) - by_id.keys()
    if unknown:
        parser.error("unknown catalogue site: " + ", ".join(sorted(unknown)))
    selected = data["sites"] if args.all or args.all_registered else [by_id[s] for s in dict.fromkeys(args.site)]
    with (args.report_dir / ".scan.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(2, "another health scan is running\n")
        while True:
            counts = Counter()
            pending=[]
            for site in selected:
                cached=reusable_result(args.report_dir,site,profile) if args.resume else None
                if cached:
                    counts[cached['state']]+=1
                else:
                    pending.append(site)
            scan = {"started_at": utc_now(), "finished_at": None, "total": len(selected),
                    "completed":sum(counts.values()),"reused":sum(counts.values()),'summary':dict(counts),
                    'scope':'metadata_sample_only','probe_profile':profile}
            atomic_json(args.report_dir / "scan.json", scan)
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {executor.submit(run_probe, site, timeout=args.timeout, limit=args.limit, delay=args.delay): site
                           for site in pending}
                for future in as_completed(futures):
                    site = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {"site_id": site["site_id"], "source_hash": site["source_hash"],
                                  "state": "error", "checked_at": utc_now(), "sample_count": 0,
                                  "exception_type": type(exc).__name__, "scope": "metadata_sample_only"}
                    result['probe_profile']=profile
                    atomic_json(site_path(args.report_dir, site["site_id"]), result)
                    counts[result["state"]] += 1
                    scan["completed"] += 1
                    scan["updated_at"] = utc_now()
                    scan['summary']=dict(counts)
                    atomic_json(args.report_dir / "scan.json", scan)
                    print(json.dumps(result, ensure_ascii=False), flush=True)
            scan.update(finished_at=utc_now(), summary=dict(counts))
            atomic_json(args.report_dir / "scan.json", scan)
            if not args.interval:
                return 0 if counts["healthy"] == len(selected) else 1
            time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())

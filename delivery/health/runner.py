"""Code inventory (--audit) for the delivered crawler catalogue. Live probes: delivery.health.plan_probe."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from .inventory import inventory
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


LIVE_PROBE_MOVED = ("live probing with legacy crawlers is disabled (it did not follow the request rules);\n"
                    "use: python -m delivery.health.plan_probe --plans-dir DIR --report-dir DIR --gate-dir DIR\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--audit", action="store_true", help="inspect all code; no network requests")
    selection.add_argument("--all", action="store_true", help="disabled: use delivery.health.plan_probe")
    selection.add_argument("--all-registered", action="store_true", help="disabled: use delivery.health.plan_probe")
    selection.add_argument("--site", action="append", help="disabled: use delivery.health.plan_probe")
    parser.add_argument("--report-dir", type=Path, default=report_dir())
    args, _ = parser.parse_known_args(argv)
    if not args.audit:
        # Refused before any inventory, crawler import or request.
        parser.exit(2, LIVE_PROBE_MOVED)
    data = inventory()
    args.report_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(args.report_dir / "inventory.json", {**data, "checked_at": utc_now()})
    with (args.report_dir / "inventory.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(data["sites"][0]))
        writer.writeheader()
        writer.writerows(data["sites"])
    print(json.dumps(data["summary"], ensure_ascii=False))
    return 0 if all(r["code_status"] == "ready" for r in data["sites"]) else 1

if __name__ == "__main__":
    raise SystemExit(main())

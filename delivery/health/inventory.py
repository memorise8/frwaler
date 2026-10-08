"""Inspect every delivered catalogue entry; source signals are not runtime proof."""
from __future__ import annotations

import ast
import csv
import hashlib
import inspect
from collections import Counter
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CATALOGUE = ROOT / "delivery/vendor/crawler_status_final.csv"


@lru_cache(maxsize=1)
def inventory() -> dict:
    from crawler.sites import CRAWLERS, REGISTRY_ERRORS

    rows = []
    with CATALOGUE.open(encoding="utf-8-sig") as handle:
        catalogue = list(csv.DictReader(handle))
    for entry in catalogue:
        site_id = entry["site_id"].strip()
        cls = CRAWLERS.get(site_id)
        row = {"site_id": site_id, "site_name": entry.get("site_name") or site_id,
               "audit_status": entry.get("status", ""), "audit_reason": entry.get("reason", ""),
               "code_status": "missing", "source": None, "source_hash": None,
               "resume_signals": False, "time_budget_signal": False,
               "order": getattr(cls, "DELIVERY_ORDER", "arbitrary"), "notes": []}
        if cls is not None:
            try:
                path = Path(cls.crawl.__code__.co_filename).resolve()
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source)
                # Include shared save/fetch code in probe provenance, not only
                # the site module. A code update invalidates old live evidence.
                digest = hashlib.sha256(source.encode())
                for shared in ("crawler/base_crawler.py", "crawler/generic_crawler.py",
                               "crawler/search_credentials.py", "delivery/capabilities.py",
                               "crawler/stealth_fetcher.py", "crawler/playwright_fetcher.py",
                               "crawler/libertree_adapter.py",
                               "delivery/health/probe.py"):
                    digest.update((ROOT / shared).read_bytes())
                config = getattr(cls, "_config_path", None)
                if config:
                    digest.update(Path(config).read_bytes())
                names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
                constants = {node.value for node in ast.walk(tree)
                             if isinstance(node, ast.Constant) and isinstance(node.value, str)}
                row.update(source=str(path.relative_to(ROOT)), source_hash=digest.hexdigest(),
                           code_status="abstract" if inspect.isabstract(cls) else "ready",
                           resume_signals={"delivery_cursor", "_advance_cursor", "_mark_exhausted"} <= names,
                           time_budget_signal="LIBERTREE_MAX_WALL_S" in constants)
                if not row["resume_signals"]:
                    row["notes"].append("resume_not_verified")
                if not row["time_budget_signal"]:
                    row["notes"].append("no_local_time_budget_signal")
                if any(isinstance(n, ast.ExceptHandler) and (n.type is None or
                       isinstance(n.type, ast.Name) and n.type.id == "Exception") for n in ast.walk(tree)):
                    row["notes"].append("broad_exception_handler_review")
            except (OSError, ValueError, SyntaxError, AttributeError, TypeError) as exc:
                row.update(code_status="invalid", notes=[type(exc).__name__])
        rows.append(row)
    return {"sites": rows, "registry_errors": REGISTRY_ERRORS,
            "summary": {"catalogue": len(rows), "registry_keys": len(CRAWLERS),
                        "code": dict(Counter(r["code_status"] for r in rows)),
                        "resume_signals": sum(r["resume_signals"] for r in rows),
                        "time_budget_signals": sum(r["time_budget_signal"] for r in rows)}}

#!/usr/bin/env python3
"""Retry Smart Finder with use_ai=True on all failed sites from DE/UK/AU reports."""
import json, sys, time
from datetime import datetime
from collections import defaultdict
sys.path.insert(0, ".")

REPORT_FILES = {
    "de": "reports/de_smartfind_20260407_100732.json",
    "uk": "reports/uk_smartfind_20260407_103753.json",
    "au": "reports/au_smartfind_20260407_105121.json",
}

def load_failed_sites():
    failed = []
    for country, path in REPORT_FILES.items():
        with open(path) as f:
            data = json.load(f)
        entries = data.get("failed", [])
        for entry in entries:
            failed.append({"country": country, **entry})
        print(f"  {country.upper()}: {len(entries)} failed sites loaded from {path}")
    return failed

def main():
    print("Loading failed sites from 3 country reports...")
    failed_sites = load_failed_sites()
    total = len(failed_sites)
    print(f"\nTotal sites to retry: {total}\n")

    from crawler.smart_finder import SmartDocumentFinder

    results = {
        "newly_succeeded": [],
        "still_failed": [],
        "total_new_files": 0,
    }

    for i, site in enumerate(failed_sites):
        country = site["country"]
        name = site["name"]
        url = site["url"]

        print(f"\n[{i+1}/{total}] [{country.upper()}] {name}")
        print(f"  URL: {url[:80]}{'...' if len(url) > 80 else ''}")

        try:
            finder = SmartDocumentFinder(delay=0.5)
            result = finder.find(url, max_pages=3, max_depth=1, use_ai=True)

            file_count = len(result.documents)
            file_types = {}
            for d in result.documents:
                file_types[d.file_type] = file_types.get(d.file_type, 0) + 1

            if file_count > 0:
                results["newly_succeeded"].append({
                    "country": country,
                    "name": name,
                    "url": url,
                    "files": file_count,
                    "types": file_types,
                    "method": result.fetch_method,
                    "pages": result.pages_scanned,
                    "details": result.detail_pages_visited,
                })
                results["total_new_files"] += file_count
                type_str = ", ".join(f"{t}:{c}" for t, c in file_types.items())
                print(f"  OK: {file_count} files ({type_str}) [{result.fetch_method}]")
            else:
                reason = "unknown"
                if result.errors:
                    reason = result.errors[0][:100]
                elif result.fetch_method == "requests":
                    reason = "JS rendering may be required (browser mode needed)"
                elif result.detail_pages_visited == 0:
                    reason = "No detail pages found"
                elif result.detail_pages_visited > 0:
                    reason = f"Visited {result.detail_pages_visited} detail pages but no file links"

                results["still_failed"].append({
                    "country": country,
                    "name": name,
                    "url": url,
                    "reason": reason,
                    "method": result.fetch_method,
                    "pages": result.pages_scanned,
                    "details": result.detail_pages_visited,
                })
                print(f"  FAIL: {reason}")

        except Exception as e:
            results["still_failed"].append({
                "country": country,
                "name": name,
                "url": url,
                "reason": str(e)[:100],
                "method": "error",
                "pages": 0,
                "details": 0,
            })
            print(f"  ERROR: {str(e)[:80]}")

    results["total_retried"] = total

    # Save report
    report_path = f"reports/retry_ai_smartfind_{datetime.now():%Y%m%d_%H%M%S}.json"
    with open(report_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    # Summary
    n_success = len(results["newly_succeeded"])
    n_failed = len(results["still_failed"])

    print(f"\n{'='*60}")
    print(f"RETRY COMPLETE (use_ai=True)")
    print(f"  Retried:         {total}")
    print(f"  Newly succeeded: {n_success}")
    print(f"  Still failed:    {n_failed}")
    print(f"  Total new files: {results['total_new_files']}")
    print(f"  Report:          {report_path}")
    print(f"{'='*60}")

    # Per-country breakdown
    print("\nPer-country breakdown:")
    by_country_ok = defaultdict(int)
    by_country_fail = defaultdict(int)
    by_country_files = defaultdict(int)
    for s in results["newly_succeeded"]:
        by_country_ok[s["country"]] += 1
        by_country_files[s["country"]] += s["files"]
    for s in results["still_failed"]:
        by_country_fail[s["country"]] += 1

    for country in sorted(REPORT_FILES.keys()):
        ok = by_country_ok[country]
        fail = by_country_fail[country]
        files = by_country_files[country]
        print(f"  {country.upper()}: {ok} succeeded ({files} files), {fail} still failed")

    # Failure reasons
    if results["still_failed"]:
        reasons = {}
        for s in results["still_failed"]:
            r = s["reason"][:60]
            reasons[r] = reasons.get(r, 0) + 1
        print("\nRemaining failure reasons:")
        for r, c in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"  [{c}] {r}")

if __name__ == "__main__":
    main()

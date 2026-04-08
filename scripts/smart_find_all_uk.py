#!/usr/bin/env python3
"""Smart find all UK URLs and report results."""
import json, sys, time
from datetime import datetime
sys.path.insert(0, ".")

def main():
    # Load UK test results
    report = "reports/uk_test_20260331_073303.json"
    with open(report) as f:
        data = json.load(f)

    urls = [(r["name"], r["url"]) for r in data["results"] if r["best_method"]]
    print(f"Smart finding {len(urls)} UK URLs...\n")

    from crawler.smart_finder import SmartDocumentFinder

    results = {"success": [], "failed": [], "total_files": 0}

    for i, (name, url) in enumerate(urls):
        print(f"\n[{i+1}/{len(urls)}] {name}")
        print(f"  URL: {url[:80]}{'...' if len(url)>80 else ''}")

        try:
            finder = SmartDocumentFinder(delay=0.5)
            result = finder.find(url, max_pages=3, max_depth=1, use_ai=False)

            file_count = len(result.documents)
            file_types = {}
            for d in result.documents:
                file_types[d.file_type] = file_types.get(d.file_type, 0) + 1

            if file_count > 0:
                results["success"].append({
                    "name": name,
                    "url": url,
                    "files": file_count,
                    "types": file_types,
                    "method": result.fetch_method,
                    "pages": result.pages_scanned,
                    "details": result.detail_pages_visited,
                })
                results["total_files"] += file_count
                type_str = ", ".join(f"{t}:{c}" for t,c in file_types.items())
                print(f"  OK: {file_count} files ({type_str}) [{result.fetch_method}]")
            else:
                reason = "unknown"
                if result.errors:
                    reason = result.errors[0][:100]
                elif result.fetch_method == "requests":
                    reason = "May require JS rendering (browser mode needed)"
                elif result.detail_pages_visited == 0:
                    reason = "No detail pages found"
                elif result.detail_pages_visited > 0:
                    reason = f"Visited {result.detail_pages_visited} detail pages but no file links found"

                results["failed"].append({
                    "name": name,
                    "url": url,
                    "reason": reason,
                    "method": result.fetch_method,
                    "pages": result.pages_scanned,
                    "details": result.detail_pages_visited,
                })
                print(f"  FAIL: {reason}")
        except Exception as e:
            results["failed"].append({
                "name": name,
                "url": url,
                "reason": str(e)[:100],
                "method": "error",
                "pages": 0,
                "details": 0,
            })
            print(f"  ERROR: {str(e)[:80]}")

    # Save report
    report_path = f"reports/uk_smartfind_{datetime.now():%Y%m%d_%H%M%S}.json"
    with open(report_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    # Summary
    print(f"\n{'='*60}")
    print(f"DONE: {len(results['success'])} succeeded, {len(results['failed'])} failed")
    print(f"Total files: {results['total_files']}")
    print(f"Report: {report_path}")
    print(f"{'='*60}")

    # Failure reasons summary
    if results["failed"]:
        reasons = {}
        for f in results["failed"]:
            r = f["reason"][:50]
            reasons[r] = reasons.get(r, 0) + 1
        print(f"\nFailure reasons:")
        for r, c in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"  [{c}] {r}")

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Retry failed smart find sites with improved finder."""
import json, glob, sys, time
from datetime import datetime
sys.path.insert(0, ".")

def main():
    with open("reports/kr_failure_analysis.json") as f:
        data = json.load(f)

    failed_sites = [(r["name"], r["url"]) for r in data["results"]]
    print(f"Retrying {len(failed_sites)} failed sites with improved finder...\n")

    from crawler.smart_finder import SmartDocumentFinder

    results = {"success": [], "still_failed": [], "total_files": 0}

    for i, (name, url) in enumerate(failed_sites):
        print(f"\n[{i+1}/{len(failed_sites)}] {name}")
        try:
            finder = SmartDocumentFinder(delay=0.5)
            result = finder.find(url, max_pages=2, max_depth=3, use_ai=False)
            
            if result.documents:
                types = {}
                for d in result.documents:
                    types[d.file_type] = types.get(d.file_type, 0) + 1
                type_str = ", ".join(f"{t}:{c}" for t, c in types.items())
                print(f"  OK: {len(result.documents)}개 파일 ({type_str})")
                results["success"].append({"name": name, "url": url, "files": len(result.documents), "types": types})
                results["total_files"] += len(result.documents)
            else:
                print(f"  STILL FAIL: 0 files")
                results["still_failed"].append({"name": name, "url": url})
        except Exception as e:
            print(f"  ERROR: {str(e)[:80]}")
            results["still_failed"].append({"name": name, "url": url, "error": str(e)[:100]})

    report_path = f"reports/kr_retry_smartfind_{datetime.now():%Y%m%d_%H%M%S}.json"
    with open(report_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*60}")
    print(f"DONE: {len(results['success'])} 새로 성공, {len(results['still_failed'])} 여전히 실패")
    print(f"추가 파일: {results['total_files']}개")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Smart find all Korean URLs and report results."""
import json, glob, sys, time
from datetime import datetime
sys.path.insert(0, ".")

def main():
    # Load Korean test results
    report = sorted(glob.glob("reports/kr_test_*.json"))[-1]
    with open(report) as f:
        data = json.load(f)
    
    urls = [(r["name"], r["url"]) for r in data["results"] if r["best_method"]]
    print(f"Smart finding {len(urls)} Korean URLs...\n")
    
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
                print(f"  OK: {file_count}개 파일 ({type_str}) [{result.fetch_method}]")
            else:
                reason = "unknown"
                if result.errors:
                    reason = result.errors[0][:100]
                elif result.fetch_method == "requests":
                    reason = "JS 렌더링 필요할 수 있음 (browser 모드 필요)"
                elif result.detail_pages_visited == 0:
                    reason = "상세 페이지를 찾지 못함 (view.do 패턴 없음)"
                elif result.detail_pages_visited > 0:
                    reason = f"{result.detail_pages_visited}개 상세 페이지 방문했으나 파일 링크 없음"
                
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
    report_path = f"reports/kr_smartfind_{datetime.now():%Y%m%d_%H%M%S}.json"
    with open(report_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    
    # Summary
    print(f"\n{'='*60}")
    print(f"DONE: {len(results['success'])} 성공, {len(results['failed'])} 실패")
    print(f"총 파일: {results['total_files']}개")
    print(f"Report: {report_path}")
    print(f"{'='*60}")
    
    # Failure reasons summary
    if results["failed"]:
        reasons = {}
        for f in results["failed"]:
            r = f["reason"][:50]
            reasons[r] = reasons.get(r, 0) + 1
        print(f"\n실패 원인 분류:")
        for r, c in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"  [{c}개] {r}")

if __name__ == "__main__":
    main()

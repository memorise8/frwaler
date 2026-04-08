#!/usr/bin/env python3
"""Batch auto-add Korean sites - one per domain, then clone for remaining."""
import json, glob, time, sys
from urllib.parse import urlparse

API = "http://localhost:30004"

def main():
    import requests
    
    # Load test results
    report = sorted(glob.glob("reports/kr_test_*.json"))[-1]
    with open(report) as f:
        data = json.load(f)
    
    # Group by domain, pick first URL per domain
    domains = {}
    for r in data["results"]:
        if r["best_method"] is None:
            continue
        domain = urlparse(r["url"]).netloc
        if domain not in domains:
            domains[domain] = {"representative": r, "others": []}
        else:
            domains[domain]["others"].append(r)
    
    reps = [(d, info["representative"]) for d, info in domains.items()]
    print(f"Auto-adding {len(reps)} representative URLs (one per domain)...\n")
    
    results = {"success": [], "failed": [], "jobs": []}
    
    for i, (domain, rep) in enumerate(reps):
        name = rep["name"]
        url = rep["url"]
        site_id = name.replace("/", "-")
        use_browser = rep["best_method"] == "browser"
        
        print(f"[{i+1}/{len(reps)}] {name} ({domain})")
        print(f"  URL: {url[:80]}{'...' if len(url)>80 else ''}")
        print(f"  Method: {rep['best_method']}, Browser: {use_browser}")
        
        try:
            resp = requests.post(f"{API}/api/auto-add", json={
                "url": url,
                "site_id": site_id,
                "browser": use_browser
            }, timeout=10)
            if resp.ok:
                job = resp.json()
                results["jobs"].append({"domain": domain, "name": name, "site_id": site_id, "job_id": job["id"]})
                print(f"  -> Job started: {job['id']}")
            else:
                print(f"  -> FAILED: {resp.status_code} {resp.text[:100]}")
                results["failed"].append({"domain": domain, "name": name, "error": resp.text[:100]})
        except Exception as e:
            print(f"  -> ERROR: {e}")
            results["failed"].append({"domain": domain, "name": name, "error": str(e)[:100]})
        
        # Wait between requests to not overwhelm
        time.sleep(2)
    
    print(f"\n{'='*60}")
    print(f"Started {len(results['jobs'])} jobs, {len(results['failed'])} immediate failures")
    print(f"{'='*60}\n")
    print("Waiting for jobs to complete...")
    
    # Poll jobs until all done
    import requests as req
    completed = 0
    failed = 0
    while True:
        pending = []
        for j in results["jobs"]:
            if j.get("done"):
                continue
            try:
                resp = req.get(f"{API}/api/jobs/{j['job_id']}").json()
                if resp["status"] == "running":
                    pending.append(j)
                elif resp["status"] == "completed":
                    j["done"] = True
                    j["result"] = resp.get("result", "")
                    completed += 1
                    results["success"].append(j)
                    print(f"  OK [{completed+failed}/{len(results['jobs'])}] {j['name']}: {j['result'][:80]}")
                elif resp["status"] == "failed":
                    j["done"] = True
                    j["result"] = resp.get("result", "")
                    failed += 1
                    results["failed"].append(j)
                    print(f"  FAIL [{completed+failed}/{len(results['jobs'])}] {j['name']}: {j['result'][:80]}")
            except:
                pending.append(j)
        
        if not pending:
            break
        
        sys.stdout.write(f"\r  Pending: {len(pending)} | OK: {completed} | FAIL: {failed}  ")
        sys.stdout.flush()
        time.sleep(5)
    
    # Save report
    from datetime import datetime
    report_path = f"reports/kr_autoadd_{datetime.now():%Y%m%d_%H%M%S}.json"
    with open(report_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    
    print(f"\n\n{'='*60}")
    print(f"DONE: {completed} success, {failed + len([r for r in results['failed'] if 'job_id' not in r])} failed")
    print(f"Report: {report_path}")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()

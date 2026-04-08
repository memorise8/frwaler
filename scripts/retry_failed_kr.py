#!/usr/bin/env python3
"""Retry failed Korean auto-add with browser=true."""
import json, time, sys, requests, glob
from urllib.parse import urlparse

API = "http://localhost:30004"

def main():
    with open("/tmp/kr_retry_urls.json") as f:
        urls = json.load(f)
    
    # Map URL back to name from test report
    report = sorted(glob.glob("reports/kr_test_*.json"))[-1]
    with open(report) as f:
        data = json.load(f)
    url_to_name = {r["url"]: r["name"] for r in data["results"]}
    
    print(f"Retrying {len(urls)} failed sites with browser=true...\n")
    
    jobs = []
    for i, url in enumerate(urls):
        name = url_to_name.get(url, urlparse(url).netloc)
        site_id = name.replace("/", "-")
        
        print(f"[{i+1}/{len(urls)}] {name} (browser=true)")
        try:
            resp = requests.post(f"{API}/api/auto-add", json={
                "url": url, "site_id": site_id, "browser": True
            }, timeout=10)
            if resp.ok:
                job = resp.json()
                jobs.append({"name": name, "job_id": job["id"]})
                print(f"  -> Job: {job['id']}")
            else:
                print(f"  -> FAILED: {resp.text[:80]}")
        except Exception as e:
            print(f"  -> ERROR: {e}")
        time.sleep(2)
    
    print(f"\nStarted {len(jobs)} jobs. Waiting...")
    
    completed = 0
    failed = 0
    while True:
        pending = []
        for j in jobs:
            if j.get("done"):
                continue
            try:
                resp = requests.get(f"{API}/api/jobs/{j['job_id']}").json()
                if resp["status"] == "running":
                    pending.append(j)
                elif resp["status"] == "completed":
                    j["done"] = True
                    completed += 1
                    print(f"  OK [{completed+failed}/{len(jobs)}] {j['name']}: {resp.get('result','')[:80]}")
                elif resp["status"] == "failed":
                    j["done"] = True
                    failed += 1
                    print(f"  FAIL [{completed+failed}/{len(jobs)}] {j['name']}: {resp.get('result','')[:80]}")
            except:
                pending.append(j)
        
        if not pending:
            break
        sys.stdout.write(f"\r  Pending: {len(pending)} | OK: {completed} | FAIL: {failed}  ")
        sys.stdout.flush()
        time.sleep(5)
    
    print(f"\n\nDONE: {completed} success, {failed} failed out of {len(jobs)}")

if __name__ == "__main__":
    main()

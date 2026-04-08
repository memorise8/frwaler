#!/usr/bin/env python3
"""Batch crawl all non-Korean sites with working configs."""
import requests
import time
import sys

API = "http://localhost:30004"
LIMIT = 9999  # Collect all available items

def main():
    # Get all sites
    sites = requests.get(f"{API}/api/sites").json()
    targets = [s for s in sites if s["country"] != "KR" and s["crawl_status"] == "success"]
    
    print(f"Crawling {len(targets)} sites with limit={LIMIT}...\n")
    
    jobs = []
    for i, site in enumerate(targets):
        sid = site["id"]
        try:
            resp = requests.post(f"{API}/api/crawl/{sid}", json={"limit": LIMIT})
            if resp.ok:
                job = resp.json()
                jobs.append({"site_id": sid, "job_id": job["id"]})
                print(f"[{i+1}/{len(targets)}] Started: {sid} (job={job['id']})")
            else:
                print(f"[{i+1}/{len(targets)}] FAILED to start: {sid} - {resp.status_code}")
        except Exception as e:
            print(f"[{i+1}/{len(targets)}] ERROR: {sid} - {e}")
        
        # Small delay to avoid overwhelming
        time.sleep(0.5)
    
    print(f"\n{'='*60}")
    print(f"Started {len(jobs)} crawl jobs. Waiting for completion...")
    print(f"{'='*60}\n")
    
    # Poll until all done
    completed = 0
    failed = 0
    while True:
        pending = []
        for j in jobs:
            try:
                resp = requests.get(f"{API}/api/jobs/{j['job_id']}").json()
                if resp["status"] == "running":
                    pending.append(j)
                elif resp["status"] == "completed":
                    if j.get("reported"):
                        continue
                    j["reported"] = True
                    completed += 1
                    print(f"  OK [{completed+failed}/{len(jobs)}] {j['site_id']}: {resp.get('result','')}")
                elif resp["status"] == "failed":
                    if j.get("reported"):
                        continue
                    j["reported"] = True
                    failed += 1
                    print(f"  FAIL [{completed+failed}/{len(jobs)}] {j['site_id']}: {resp.get('result','')}")
            except:
                pending.append(j)
        
        if not pending and (completed + failed) >= len(jobs):
            break
        
        sys.stdout.write(f"\r  Pending: {len(pending)} | Completed: {completed} | Failed: {failed}  ")
        sys.stdout.flush()
        time.sleep(3)
    
    print(f"\n\n{'='*60}")
    print(f"DONE: {completed} completed, {failed} failed out of {len(jobs)} jobs")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()

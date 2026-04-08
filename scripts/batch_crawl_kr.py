#!/usr/bin/env python3
"""Batch crawl all Korean sites with configs."""
import requests, time, sys, glob, json

API = "http://localhost:30004"
LIMIT = 9999

def main():
    # Get all Korean config files
    configs = glob.glob("crawler/sites/configs/*.json")
    kr_sites = []
    kr_prefixes = (
        "mohw-","moef-","nts-","pps-","kostat-","moe-","msit-","kasa-","mofa-",
        "oka-","unikorea-","moj-","mnd-","mma-","dapa-","mois-","police-","nfa-",
        "mpva-","khs-","mafra-","forest-","motie-","kipo-","kdca-","kma-","mogef-",
        "naacc-","mof-","kcg-","mss-","opm-","mfds-","ftc-","fsc-","fss-","bok-",
        "kinfa-","kamco-","comwel-","nps-","nhis-","kipf-","nrc-","kiep-","kinu-",
        "kicj-","kipa-","kice-","keei-","kisdi-","kihasa-","kicce-","kli-","krivet-",
        "kmi-","klri-","kwdi-","nypi-","koti-","kei-","kedi-","krei-","krihs-",
        "auri-","stepi-","korail-","keis-","kepco-","seoul-","busan-","daegu-",
        "incheon-","gwangju-","daejeon-","ulsan-","sejong-","gyeonggi-","chungnam-",
        "jeonnam-","gb-","gyeongnam-","gangwon-","jeju-","kcc-","kmcc-","nhrck-",
        "nec-","bai-","scourt-","moleg-","si-","gri-","ii-","gi-","test-mohw"
    )
    
    for cfg in configs:
        sid = cfg.split("/")[-1].replace(".json", "")
        if any(sid.startswith(p) for p in kr_prefixes):
            kr_sites.append(sid)
    
    # Remove test-mohw2
    kr_sites = [s for s in kr_sites if s != "test-mohw2"]
    kr_sites.sort()
    
    print(f"Crawling {len(kr_sites)} Korean sites (limit={LIMIT})...\n")
    
    jobs = []
    for i, sid in enumerate(kr_sites):
        try:
            resp = requests.post(f"{API}/api/crawl/{sid}", json={"limit": LIMIT})
            if resp.ok:
                job = resp.json()
                jobs.append({"site_id": sid, "job_id": job["id"]})
                print(f"[{i+1}/{len(kr_sites)}] Started: {sid}")
            else:
                print(f"[{i+1}/{len(kr_sites)}] FAILED: {sid} - {resp.status_code}")
        except Exception as e:
            print(f"[{i+1}/{len(kr_sites)}] ERROR: {sid} - {e}")
        time.sleep(0.5)
    
    print(f"\n{'='*60}")
    print(f"Started {len(jobs)} crawl jobs. Waiting...")
    print(f"{'='*60}\n")
    
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
                    j["result"] = resp.get("result", "")
                    completed += 1
                    print(f"  OK [{completed+failed}/{len(jobs)}] {j['site_id']}: {j['result'][:80]}")
                elif resp["status"] == "failed":
                    j["done"] = True
                    j["result"] = resp.get("result", "")
                    failed += 1
                    print(f"  FAIL [{completed+failed}/{len(jobs)}] {j['site_id']}: {j['result'][:80]}")
            except:
                pending.append(j)
        
        if not pending:
            break
        sys.stdout.write(f"\r  Pending: {len(pending)} | OK: {completed} | FAIL: {failed}  ")
        sys.stdout.flush()
        time.sleep(3)
    
    print(f"\n\n{'='*60}")
    print(f"DONE: {completed} completed, {failed} failed out of {len(jobs)}")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()

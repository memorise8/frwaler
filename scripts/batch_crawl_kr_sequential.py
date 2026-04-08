#!/usr/bin/env python3
"""Sequential crawl Korean sites - one at a time to avoid DB locking."""
import json, glob, sys, time
sys.path.insert(0, ".")
from crawler import db as db_module
from crawler.generic_crawler import GenericCrawler

def main():
    configs = glob.glob("crawler/sites/configs/*.json")
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
        "nec-","bai-","scourt-","moleg-","si-","gri-","ii-","gi-"
    )
    
    # Filter valid Korean configs
    kr_configs = []
    for c in sorted(configs):
        sid = c.split("/")[-1].replace(".json", "")
        if not any(sid.startswith(p) for p in kr_prefixes):
            continue
        with open(c) as f:
            data = json.load(f)
        if "list_page" in data and isinstance(data["list_page"], dict) and "url" in data["list_page"]:
            kr_configs.append((sid, c, data))
    
    print(f"Crawling {len(kr_configs)} valid Korean sites sequentially...\n")
    
    conn = db_module.get_db()
    db_module.init_db(conn)
    
    success = 0
    failed = 0
    total_papers = 0
    
    for i, (sid, path, config) in enumerate(kr_configs):
        print(f"\n[{i+1}/{len(kr_configs)}] {sid}...")
        
        # Register site in DB first
        try:
            conn.execute(
                "INSERT OR IGNORE INTO sites (id, name, base_url) VALUES (?, ?, ?)",
                (sid, config.get("site_name", sid), config.get("base_url", ""))
            )
            conn.commit()
        except Exception as e:
            print(f"  Site register error: {e}")
        
        # Crawl
        try:
            crawler = GenericCrawler(path, conn)
            saved = crawler.crawl(limit=9999)
            if saved and saved > 0:
                print(f"  OK: Saved {saved} papers")
                success += 1
                total_papers += saved
            else:
                print(f"  OK: Saved 0 papers")
                success += 1
        except Exception as e:
            err = str(e)[:100]
            print(f"  FAIL: {err}")
            failed += 1
    
    conn.close()
    print(f"\n{'='*60}")
    print(f"DONE: {success} success, {failed} failed")
    print(f"Total papers saved: {total_papers}")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()

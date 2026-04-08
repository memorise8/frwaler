#!/usr/bin/env python3
"""Test German government/research URLs with multiple fetch methods.

Usage:
    PYTHONUNBUFFERED=1 python scripts/test_de_sites.py [timeout_seconds]
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import cloudscraper
from playwright.sync_api import sync_playwright

DE_URLS = [
    ("daten.berlin.de", "https://daten.berlin.de/datensaetze?res_format=PDF&sort=score+desc%2C+metadata_modified+desc"),
    ("publikationen-bund/search", "https://www.publikationen-bundesregierung.de/pp-en/search-for-publications"),
    ("publikationen-bund/aa", "https://www.publikationen-bundesregierung.de/pp-en/publishers/federal-foreign-office-aa-"),
    ("auswaertiges-amt/brochures", "https://www.auswaertiges-amt.de/en/newsroom/brochures"),
    ("auswaertiges-amt/news", "https://www.auswaertiges-amt.de/en/newsroom/news/609204-609204"),
    ("publikationen-bund/bmas", "https://www.publikationen-bundesregierung.de/pp-en/publishers/federal-ministry-of-labour-and-social-affairs-bmas-"),
    ("publikationen-bund/bmbfsfj", "https://www.publikationen-bundesregierung.de/pp-en/publishers/federal-ministry-for-education-family-affairs-senior-citizens-women-and-youth-bmbfsfj-"),
    ("publikationen-bund/bmf", "https://www.publikationen-bundesregierung.de/pp-en/publishers/federal-ministry-of-finance-bmf-"),
    ("publikationen-bund/bmftr", "https://www.publikationen-bundesregierung.de/pp-en/publishers/federal-ministry-of-research-technology-and-space-bmftr-"),
    ("publikationen-bund/bmg", "https://www.publikationen-bundesregierung.de/pp-en/publishers/federal-ministry-of-health-bmg-"),
    ("publikationen-bund/bmi", "https://www.publikationen-bundesregierung.de/pp-en/publishers/federal-ministry-of-the-interior-bmi-"),
    ("publikationen-bund/bmjv", "https://www.publikationen-bundesregierung.de/pp-en/publishers/federal-ministry-of-justice-and-consumer-protection-bmjv-"),
    ("publikationen-bund/bmleh", "https://www.publikationen-bundesregierung.de/pp-en/publishers/federal-ministry-of-agriculture-food-and-regional-identity-bmleh-"),
    ("publikationen-bund/bmukn", "https://www.publikationen-bundesregierung.de/pp-en/publishers/federal-ministry-for-the-environment-climate-action-nature-conservation-and-nuclear-safety-bmukn-"),
    ("publikationen-bund/bmv", "https://www.publikationen-bundesregierung.de/pp-en/publishers/federal-ministry-of-transport-bmv-"),
    ("publikationen-bund/bmvg", "https://www.publikationen-bundesregierung.de/pp-en/publishers/federal-ministry-of-defence-bmvg-"),
    ("publikationen-bund/bmwe", "https://www.publikationen-bundesregierung.de/pp-en/publishers/federal-ministry-for-economic-affairs-and-energy-bmwe-"),
    ("publikationen-bund/bmz", "https://www.publikationen-bundesregierung.de/pp-en/publishers/federal-ministry-for-economic-cooperation-and-development-bmz-"),
    ("publikationen-bund/bmwsb", "https://www.publikationen-bundesregierung.de/pp-en/publishers/federal-ministry-for-housing-urban-development-and-building-bmwsb-"),
    ("publikationen-bund/bk", "https://www.publikationen-bundesregierung.de/pp-en/publishers/federal-chancellery-bk-"),
    ("publikationen-bund/bpa", "https://www.publikationen-bundesregierung.de/pp-en/publishers/press-and-information-office-of-the-federal-government-bpa-"),
    ("publikationen-bund/bbmb", "https://www.publikationen-bundesregierung.de/pp-en/publishers/federal-government-commissioner-for-matters-relating-to-persons-with-disabilities-bbmb-"),
    ("publikationen-bund/bmg-d", "https://www.publikationen-bundesregierung.de/pp-en/publishers/commissioner-of-the-federal-government-for-drug-and-addiction-policy-bmg-d-"),
    ("publikationen-bund/ib", "https://www.publikationen-bundesregierung.de/pp-en/publishers/federal-government-commissioner-for-migration-refugees-and-integration-ib-"),
    ("publikationen-bund/bkm", "https://www.publikationen-bundesregierung.de/pp-en/publishers/federal-government-commissioner-for-culture-and-the-media-bkm-"),
    ("publikationen-bund/ubskm", "https://www.publikationen-bundesregierung.de/pp-en/publishers/independent-commissioner-for-child-sexual-abuse-issues-ubskm-"),
    ("bundesfinanz/brochures", "https://www.bundesfinanzministerium.de/Web/EN/Resources/Publications/Brochures/brochures.html"),
    ("bundesfinanz/monthly", "https://www.bundesfinanzministerium.de/Web/EN/Resources/Publications/Monthly_report/monthly_report.html"),
    ("bmi.bund.de/news", "https://www.bmi.bund.de/SiteGlobals/Forms/suche/EN/expertensuche-formular.html?gts=9398922_list%253DunifiedDate_dt%2Bdesc&documentType_=news"),
    ("bmleh/advisory", "https://www.bmleh.de/EN/ministry/organisation/advisory-boards/AgriculturalPolicyPublications.html"),
    ("bmleh/goodpractices", "https://www.bmleh.de/SiteGlobals/Forms/Suche/DE/GoodPracticessuche/GoodPracticessuche_Formular.html?view=processForm&nn=902"),
    ("bmleh/presse", "https://www.bmleh.de/SiteGlobals/Forms/Suche/DE/Pressemitteilungssuche/Pressemitteilungssuche_Formular.html?view=processForm&nn=902"),
    ("bmwe/publications", "https://www.bundeswirtschaftsministerium.de/Navigation/EN/Service/Publications/publications.html"),
    ("bmwe/medienraum", "https://www.bundeswirtschaftsministerium.de/SiteGlobals/BMWI/Forms/Listen/EN/Medienraum/Medienraum_Formular.html"),
    ("bmbfsfj/publikationen", "https://www.bmbfsfj.bund.de/bmbfsfj/service/publikationen/72642!search?state=H4sIAAAAAAAA_1WOuw7CMAxFfwV5zgBrNlToXKRuqEPUuBApJMV2eVX9d9LA0G6-D-vcEawRLCneQIfBe5V1HZdqTmt8ybqxdAbG_dOQPaQEdGc8YzaPDwyyMntvXCidF6TTgOSQQZ8bBZ1pUdI9TgquTrhCqswl_e22Cu6p-QYNoODj-iJa_AmOlCbkPRuL3EKGzrwiBhZKKPmTpy-XGJ7j6QAAAA%3D%3D&tfs=37944%3A%24reset%24&tfs=37948%3A%24reset%24&_useDateConstraint=false&dateFrom=&dateTo=&tfs=37964%3A38810&tfs=37966%3A%24reset%24&tfs=37968%3A%24reset%24&tfs=37970%3A%24reset%24#search72642"),
    ("bmbfsfj/presse", "https://www.bmbfsfj.bund.de/bmbfsfj/aktuelles/pressemitteilungen"),
    ("bmg/pflege", "https://www.bundesgesundheitsministerium.de/service/publikationen/pflege.html"),
    ("bmg/drogen", "https://www.bundesgesundheitsministerium.de/service/publikationen/drogen-und-sucht.html"),
    ("bmg/gesundheit", "https://www.bundesgesundheitsministerium.de/service/publikationen/gesundheit.html"),
    ("bmg/praevention", "https://www.bundesgesundheitsministerium.de/service/publikationen/praevention.html"),
    ("bmg/ministerium", "https://www.bundesgesundheitsministerium.de/service/publikationen/ministerium.html"),
    ("bmg/forschung", "https://www.bundesgesundheitsministerium.de/service/publikationen/forschung.html"),
    ("bmg/presse", "https://www.bundesgesundheitsministerium.de/presse/pressemitteilungen"),
    ("bmv/publications", "https://www.bmv.de/EN/Services/Publications/publications.html"),
    ("bmv/mobility", "https://www.bmv.de/EN/Services/Statistics/Mobility-in-Germany/mobility-in-germany.html"),
    ("bmukn/presse", "https://www.bundesumweltministerium.de/presse/pressemitteilungen"),
    ("bmftr/publikationen", "https://www.bmftr.bund.de/SiteGlobals/Forms/Suche/Publikationssuche/Publikationssuche_Formular.html"),
    ("datenportal.bmbf", "https://www.datenportal.bmbf.de/portal/de/bufi.html"),
    ("bmz/publications", "https://www.bmz.de/en/news/publications"),
    ("bmwsb/meldungen", "https://www.bmwsb.bund.de/DE/ministerium/aktuelle-meldungen/aktuelle-meldungen_node.html"),
    ("statistischebibliothek", "https://www.statistischebibliothek.de/mir/receive/DESerie_mods_00000012?list=all"),
    ("fraunhofer/journal", "https://publica.fraunhofer.de/search?page=1&configuration=researchoutputs&query=&f.oavisible=Open%20Access,equals&spc.page=1&f.types=Journal%20Article,equals"),
    ("fraunhofer/conference", "https://publica.fraunhofer.de/search?page=1&configuration=researchoutputs&query=&f.oavisible=Open%20Access,equals&spc.page=1&f.types=Conference%20Paper,equals"),
    ("fraunhofer/thesis", "https://publica.fraunhofer.de/search?page=1&configuration=researchoutputs&query=&f.oavisible=Open%20Access,equals&spc.page=1&f.types=Doctoral%20Thesis,equals"),
    ("fraunhofer/report", "https://publica.fraunhofer.de/search?page=1&configuration=researchoutputs&query=&f.oavisible=Open%20Access,equals&spc.page=1&f.types=Report,equals"),
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def is_success(status, size):
    if not status or status >= 400:
        return False
    if size < 500:
        return False
    return True


def test_requests_method(url, timeout=60):
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    })
    start = time.time()
    try:
        resp = session.get(url, timeout=timeout)
        elapsed = time.time() - start
        content = resp.text[:500].lower()
        has_captcha = "captcha" in content or "challenge" in content
        return {
            "method": "requests",
            "status": resp.status_code,
            "size": len(resp.content),
            "time": round(elapsed, 2),
            "captcha": has_captcha,
            "error": None,
        }
    except Exception as e:
        return {
            "method": "requests",
            "status": None,
            "size": 0,
            "time": round(time.time() - start, 2),
            "captcha": False,
            "error": str(e)[:120],
        }


def test_cloudscraper_method(url, timeout=60):
    scraper = cloudscraper.create_scraper()
    start = time.time()
    try:
        resp = scraper.get(url, timeout=timeout)
        elapsed = time.time() - start
        content = resp.text[:500].lower()
        has_captcha = "captcha" in content or "challenge" in content
        return {
            "method": "cloudscraper",
            "status": resp.status_code,
            "size": len(resp.content),
            "time": round(elapsed, 2),
            "captcha": has_captcha,
            "error": None,
        }
    except Exception as e:
        return {
            "method": "cloudscraper",
            "status": None,
            "size": 0,
            "time": round(time.time() - start, 2),
            "captcha": False,
            "error": str(e)[:120],
        }


def test_browser_method(url, browser, timeout=60):
    start = time.time()
    page = browser.new_page(user_agent=USER_AGENT)
    try:
        resp = page.goto(url, timeout=timeout * 1000, wait_until="networkidle")
        status = resp.status if resp else None
        content = page.content()
        elapsed = time.time() - start
        has_captcha = "captcha" in content[:500].lower() or "challenge" in content[:500].lower()
        return {
            "method": "browser",
            "status": status,
            "size": len(content),
            "time": round(elapsed, 2),
            "captcha": has_captcha,
            "error": None,
        }
    except Exception as e:
        return {
            "method": "browser",
            "status": None,
            "size": 0,
            "time": round(time.time() - start, 2),
            "captcha": False,
            "error": str(e)[:120],
        }
    finally:
        page.close()


def main():
    timeout = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    print(f"Testing {len(DE_URLS)} German URLs with {timeout}s timeout...\n")

    results = []
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)

    try:
        for name, url in DE_URLS:
            print(f"\n--- [{name}] {url[:80]}{'...' if len(url)>80 else ''} ---")
            row = {"name": name, "url": url, "results": [], "best_method": None}

            for test_fn in [test_requests_method, test_cloudscraper_method]:
                r = test_fn(url, timeout)
                row["results"].append(r)
                ok = is_success(r["status"], r["size"]) and not r["captcha"]
                icon = "OK" if ok else "FAIL"
                print(f"  {r['method']:15s} {icon:4s}  status={r['status']}  size={r['size']:>8,}  time={r['time']}s"
                      + (f"  err={r['error'][:60]}" if r["error"] else "")
                      + ("  [CAPTCHA]" if r["captcha"] else ""))
                if ok:
                    row["best_method"] = r["method"]
                    break
            else:
                r = test_browser_method(url, browser, timeout)
                row["results"].append(r)
                ok = is_success(r["status"], r["size"]) and not r["captcha"]
                icon = "OK" if ok else "FAIL"
                print(f"  {r['method']:15s} {icon:4s}  status={r['status']}  size={r['size']:>8,}  time={r['time']}s"
                      + (f"  err={r['error'][:60]}" if r["error"] else "")
                      + ("  [CAPTCHA]" if r["captcha"] else ""))
                if ok:
                    row["best_method"] = r["method"]

            results.append(row)
    finally:
        browser.close()
        pw.stop()

    # Save report
    report = {
        "timestamp": datetime.now().isoformat(),
        "timeout": timeout,
        "total": len(results),
        "results": results,
    }
    report_dir = Path(__file__).resolve().parent.parent / "reports"
    report_dir.mkdir(exist_ok=True)
    report_path = report_dir / f"de_test_{datetime.now():%Y%m%d_%H%M%S}.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nReport saved: {report_path}")

    # Summary
    methods = {"requests": [], "cloudscraper": [], "browser": [], None: []}
    for r in results:
        methods[r["best_method"]].append(r["name"])

    print(f"\n{'='*60}")
    print(f"SUMMARY ({len(results)} URLs tested, timeout={timeout}s)")
    print(f"{'='*60}")
    for cat, label in [("requests", "A (requests)"), ("cloudscraper", "B (cloudscraper)"), ("browser", "C (browser)"), (None, "D (FAILED)")]:
        print(f"  Category {label}: {len(methods[cat])}")
        for n in methods[cat]:
            print(f"    - {n}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Test UK government/research URLs with multiple fetch methods.

Usage:
    PYTHONUNBUFFERED=1 python scripts/test_uk_sites.py [timeout_seconds]
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import cloudscraper
from playwright.sync_api import sync_playwright

UK_URLS = [
    ("gov.uk/research", "https://www.gov.uk/search/research-and-statistics?content_store_document_type=research&order=updated-newest"),
    ("gov.uk/statistics", "https://www.gov.uk/search/research-and-statistics?content_store_document_type=statistics_published&order=updated-newest"),
    ("data.gov.uk", "https://www.data.gov.uk/search?q=&filters%5Bpublisher%5D=&filters%5Btopic%5D=&filters%5Bformat%5D=PDF&sort=best"),
    ("gov.uk/official-docs", "https://www.gov.uk/official-documents"),
    ("ukdataservice", "https://ukdataservice.ac.uk/2026/03/20/latest-data-collections-and-new-editions-20-march-2026/"),
    ("ons.gov.uk/pubs", "https://www.ons.gov.uk/search?q=uk+wide&filter=publications&page=1"),
    ("ons.gov.uk/data", "https://www.ons.gov.uk/search?q=uk+wide&page=1&filter=time_series&filter=datasets&filter=user_requested_data"),
    ("naturalengland", "https://naturalengland-defra.opendata.arcgis.com/search?collection=dataset"),
    ("geoportal.stats", "https://geoportal.statistics.gov.uk/search?collection=document&sort=Date%20Updated%7Cmodified%7Cdesc"),
    ("gov.scot/stats", "https://www.gov.scot/statistics-and-research/"),
    ("gov.scot/pubs", "https://www.gov.scot/publications/"),
    ("publichealthscotland", "https://www.publichealthscotland.scot/publications/"),
    ("nrscotland", "https://www.nrscotland.gov.uk/publications/"),
    ("gov.wales/stats", "https://www.gov.wales/statistics-and-research"),
    ("gov.wales/pubs", "https://www.gov.wales/publications"),
    ("nisra", "https://www.nisra.gov.uk/publications"),
    ("education-ni/news", "https://www.education-ni.gov.uk/news"),
    ("education-ni/pubs", "https://www.education-ni.gov.uk/publications"),
    ("economy-ni/pubs", "https://www.economy-ni.gov.uk/publications"),
    ("economy-ni/news", "https://www.economy-ni.gov.uk/news"),
    ("finance-ni/news", "https://www.finance-ni.gov.uk/news"),
    ("finance-ni/pubs", "https://www.finance-ni.gov.uk/publications"),
    ("infrastructure-ni/news", "https://www.infrastructure-ni.gov.uk/news"),
    ("infrastructure-ni/pubs", "https://www.infrastructure-ni.gov.uk/publications"),
    ("health-ni/news", "https://www.health-ni.gov.uk/news"),
    ("health-ni/pubs", "https://www.health-ni.gov.uk/publications"),
    ("justice-ni/news", "https://www.justice-ni.gov.uk/news"),
    ("justice-ni/pubs", "https://www.justice-ni.gov.uk/publications"),
    ("data.london", "https://data.london.gov.uk/dataset/?format=pdf"),
    ("adruk/pubs", "https://www.adruk.org/news-publications/publications-reports/"),
    ("adruk/annual", "https://www.adruk.org/news-publications/annual-reports/"),
    ("adruk/impact", "https://www.adruk.org/news-publications/impact-case-studies/"),
    ("adruk/datacatalogue", "https://datacatalogue.adruk.org/browser/search?include=dataset::datastandard::terminology::dataclass::dataelement"),
    ("parliament/commons", "https://www.parliament.uk/business/publications/commons/house-of-commons-journal/"),
    ("parliament/oscepa", "https://www.parliament.uk/mps-lords-and-offices/offices/uk-parliamentary-assemblies/ocsepa-uk/oscepa-uk-publications/"),
    ("committees.parliament", "https://committees.parliament.uk/publications/"),
    ("post.parliament", "https://post.parliament.uk/research/all-research/"),
    ("lordslibrary", "https://lordslibrary.parliament.uk/research/all-research/"),
    ("babraham/report", "https://www.babraham.ac.uk/our-research/annual-research-report"),
    ("babraham/news", "https://www.babraham.ac.uk/news/category/news"),
    ("jic/pubs", "https://www.jic.ac.uk/research-impact/publications/"),
    ("jic/news", "https://www.jic.ac.uk/news-events/"),
    ("jic/annual", "https://www.jic.ac.uk/about-us/our-funding-annual-reports-accounts/archive-of-annual-reports-and-accounts/"),
    ("earlham/pubs", "https://www.earlham.ac.uk/publications"),
    ("earlham/news", "https://www.earlham.ac.uk/newsroom"),
    ("lboro.repository", "https://repository.lboro.ac.uk/"),
    ("mrc-lmb/pubs", "https://www2.mrc-lmb.cam.ac.uk/research/published-research/"),
    ("noc/brochures", "https://noc.ac.uk/about-us/literature-brochures"),
    ("noc/pubs", "https://noc.ac.uk/publications"),
    ("noc/news", "https://noc.ac.uk/news/news-archive"),
    ("eprints.soton", "https://eprints.soton.ac.uk/view/divisions/5d1c5ba6-6977-4d9d-9afe-fd1820201262/"),
    ("nora.nerc", "https://nora.nerc.ac.uk/view/division/noc/"),
    ("rfi/reports", "https://www.rfi.ac.uk/latest/?filter_nonce=7501d28615&filter=report-download"),
    ("rfi/news", "https://www.rfi.ac.uk/latest/?filter_nonce=7afe782d93&filter=news"),
    ("opendatacommunities", "https://opendatacommunities.org/data"),
    ("skillsdev.scot/pubs", "https://www.skillsdevelopmentscotland.co.uk/publications-statistics/publications"),
    ("skillsdev.scot/news", "https://www.skillsdevelopmentscotland.co.uk/news-events"),
    ("osr/pubs", "https://osr.statisticsauthority.gov.uk/publications-list/"),
    ("osr/news", "https://osr.statisticsauthority.gov.uk/news-list/"),
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
        "Accept-Language": "en-GB,en;q=0.9",
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
    timeout = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    print(f"Testing {len(UK_URLS)} UK URLs with {timeout}s timeout...\n")

    results = []
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)

    try:
        for name, url in UK_URLS:
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
    report_path = report_dir / f"uk_test_{datetime.now():%Y%m%d_%H%M%S}.json"
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

#!/usr/bin/env python3
"""Smart find all Australian URLs and report results."""
import json, sys, time
from datetime import datetime
sys.path.insert(0, ".")

def main():
    urls = [
        ("transparency.gov.au", "https://www.transparency.gov.au/publications"),
        ("data.gov.au", "https://data.gov.au/data/dataset/?res_format=PDF"),
        ("dataverse.ada.edu.au", "https://dataverse.ada.edu.au/dataverse/ada?q=&types=dataverses%3Adatasets%3Afiles&sort=dateSort&order=desc&page=1"),
        ("pmc.gov.au", "https://www.pmc.gov.au/resources?f%5B0%5D=db_r_publication_category%3A334"),
        ("treasury.gov.au", "https://treasury.gov.au/publication"),
        ("abs.gov.au", "https://dataexplorer.abs.gov.au/?tm=%20&pg=0&fc=Economy&snb=1213&isAvailabilityDisabled=false"),
        ("accc.gov.au", "https://www.accc.gov.au/about-us/publications"),
        ("apra.gov.au/stats", "https://www.apra.gov.au/statistics"),
        ("apra.gov.au/pubs", "https://www.apra.gov.au/news-and-publications/39"),
        ("anu.edu.au", "https://openresearch-repository.anu.edu.au/search?spc.page=1&spc.sf=dc.date.accessioned&spc.sd=DESC&view=list"),
        ("ga.gov.au/geonetwork", "https://ecat.ga.gov.au/geonetwork/srv/eng/catalog.search#/search?isTemplate=n&sortBy=relevance&from=1&to=30&any=PDF"),
        ("ga.gov.au/1940s", "https://www.ga.gov.au/data-pubs/library/legacy-publications/records/1940s"),
        ("ga.gov.au/1950s", "https://www.ga.gov.au/data-pubs/library/legacy-publications/records/1950s"),
        ("ga.gov.au/1960s", "https://www.ga.gov.au/data-pubs/library/legacy-publications/records/1960s"),
        ("ga.gov.au/1970s", "https://www.ga.gov.au/data-pubs/library/legacy-publications/records/1970s"),
        ("ga.gov.au/1980s", "https://www.ga.gov.au/data-pubs/library/legacy-publications/records/1980s"),
        ("ga.gov.au/1990s", "https://www.ga.gov.au/data-pubs/library/legacy-publications/records/1990s"),
        ("ga.gov.au/2000s", "https://www.ga.gov.au/data-pubs/library/legacy-publications/records/2000s"),
        ("ga.gov.au/2010s", "https://www.ga.gov.au/data-pubs/library/legacy-publications/records/2010s"),
        ("ga.gov.au/2020s", "https://www.ga.gov.au/data-pubs/library/legacy-publications/records/digitised-records-2020s"),
        ("alrc.gov.au", "https://www.alrc.gov.au/publications/final-report/"),
        ("homeaffairs.gov.au/search", "https://www.homeaffairs.gov.au/sitesearch?k=pdf"),
        ("treasury.gov.au/search", "https://treasury.gov.au/search?search_keyword=pdf"),
        ("abs.gov.au/search", "https://search.abs.gov.au/s/search.html?form=simple&collection=abs-search&query=pdf"),
        ("accc.gov.au/search", "https://www.accc.gov.au/search?query=pdf"),
        ("apra.gov.au/search", "https://www.apra.gov.au/search?query=pdf"),
        ("ansto.gov.au/search", "https://www.ansto.gov.au/search?query=pdf"),
        ("ga.gov.au/search", "https://www.ga.gov.au/search?from=0&query=pdf&index=geoscience_site_crawl"),
        ("alrc.gov.au/search", "https://www.alrc.gov.au/?s=pdf&type=all"),
        ("afp.gov.au", "https://www.afp.gov.au/news-centre"),
        ("afp.gov.au/search", "https://afp.gov.au/search?keys=pdf&content_type_id=All"),
        ("pmc.gov.au/search", "https://www.pmc.gov.au/search?term=pdf"),
    ]
    print(f"Smart finding {len(urls)} Australian URLs...\n")

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
    report_path = f"reports/au_smartfind_{datetime.now():%Y%m%d_%H%M%S}.json"
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

#!/usr/bin/env python3
"""Retry A (onclick) and G (timeout) failure sites with Playwright download interception."""
import json, sys, time, os, re
from datetime import datetime
from urllib.parse import urljoin
sys.path.insert(0, ".")

# Load env
with open("crawler/.env") as f:
    for line in f:
        if "=" in line:
            k, v = line.strip().split("=", 1)
            os.environ[k] = v

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"

FILE_EXTENSIONS = (
    '.pdf', '.xlsx', '.xls', '.csv', '.hwp', '.hwpx',
    '.docx', '.doc', '.pptx', '.ppt', '.zip',
)


def find_files_with_interception(url, pw_browser, timeout=20):
    """Use Playwright to find files via download interception and deep analysis."""
    found_files = []
    seen_urls = set()

    page = pw_browser.new_page(user_agent=USER_AGENT)
    page.set_default_timeout(timeout * 1000)

    try:
        # Navigate to page
        page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
        time.sleep(2)

        html = page.content()
        soup = BeautifulSoup(html, "html.parser")

        # Strategy 1: Find direct file links
        for a in soup.select("a[href]"):
            href = a.get("href", "").strip()
            if not href or href == "#" or href.startswith("javascript:"):
                continue
            abs_url = urljoin(url, href)
            href_lower = href.lower()

            # Check extensions
            is_file = any(href_lower.split("?")[0].endswith(ext) for ext in FILE_EXTENSIONS)
            # Check download patterns
            is_download = bool(re.search(r'download|attach|fileDown|fileSrc|atchFile|FileDown\.do|boardFile', href, re.I))
            # Check query params
            is_query_file = any(p in href_lower for p in ['atchfileid', 'fileid', 'filesn', 'downtype'])

            if (is_file or is_download or is_query_file) and abs_url not in seen_urls:
                seen_urls.add(abs_url)
                text = a.get_text(strip=True) or "Untitled"
                ftype = "unknown"
                for ext in FILE_EXTENSIONS:
                    if ext in href_lower or ext in text.lower():
                        ftype = ext[1:]
                        break
                found_files.append({"title": text[:200], "url": abs_url, "type": ftype, "method": "link"})

        # Strategy 2: Click download buttons and intercept
        downloads_captured = []

        def on_download(download):
            downloads_captured.append({
                "title": download.suggested_filename,
                "url": download.url,
                "type": download.suggested_filename.rsplit(".", 1)[-1].lower() if "." in download.suggested_filename else "unknown",
                "method": "intercept",
            })
            download.cancel()

        page.on("download", on_download)

        # Find clickable download elements
        download_selectors = [
            "a[onclick*=download]", "a[onclick*=Download]",
            "a[onclick*=fileDown]", "a[onclick*=fn_down]",
            "a[onclick*=goDown]", "a[onclick*=fnDown]",
            "button[onclick*=download]", "button[onclick*=fileDown]",
            "a[href='#'][onclick]",  # Links with # href and onclick
            "a.btn_down", "a.file_down", "a.download",
            "button.btn_down", "button.download",
        ]

        for selector in download_selectors:
            try:
                elements = page.query_selector_all(selector)
                for el in elements[:15]:
                    try:
                        with page.expect_download(timeout=5000) as dl_info:
                            el.click()
                        dl = dl_info.value
                        # Already captured by on_download handler
                        dl.cancel()
                    except Exception:
                        pass
            except Exception:
                pass

        # Add intercepted downloads
        for dl in downloads_captured:
            if dl["url"] not in seen_urls:
                seen_urls.add(dl["url"])
                found_files.append(dl)

        # Strategy 3: Visit detail pages (view.do pattern)
        detail_links = []
        for a in soup.select("a[href]"):
            href = a.get("href", "").strip()
            if not href or href == "#" or href.startswith("javascript:"):
                continue
            abs_url = urljoin(url, href)
            path_lower = abs_url.lower()
            if any(v in path_lower for v in ["view.do", "view.jsp", "subview.do", "detail"]):
                text = a.get_text(strip=True)
                if len(text) > 5:
                    detail_links.append({"url": abs_url, "text": text})

        # Visit up to 10 detail pages
        for dl in detail_links[:10]:
            if dl["url"] in seen_urls:
                continue
            seen_urls.add(dl["url"])

            try:
                page.goto(dl["url"], wait_until="networkidle", timeout=timeout * 1000)
                time.sleep(1)
                detail_html = page.content()
                detail_soup = BeautifulSoup(detail_html, "html.parser")

                # Find files on detail page
                for a in detail_soup.select("a[href]"):
                    href = a.get("href", "").strip()
                    if not href or href == "#" or href.startswith("javascript:"):
                        continue
                    abs_url2 = urljoin(dl["url"], href)
                    href_lower = href.lower()

                    is_file = any(href_lower.split("?")[0].endswith(ext) for ext in FILE_EXTENSIONS)
                    is_download = bool(re.search(r'download|attach|fileDown|fileSrc|atchFile|FileDown\.do|boardFile', href, re.I))
                    is_query_file = any(p in href_lower for p in ['atchfileid', 'fileid', 'filesn'])
                    has_ext_in_text = any(ext in a.get_text(strip=True).lower() for ext in FILE_EXTENSIONS)

                    if (is_file or is_download or is_query_file or has_ext_in_text) and abs_url2 not in seen_urls:
                        seen_urls.add(abs_url2)
                        text = a.get_text(strip=True) or dl["text"]
                        ftype = "unknown"
                        for ext in FILE_EXTENSIONS:
                            if ext in href_lower or ext in text.lower():
                                ftype = ext[1:]
                                break
                        found_files.append({"title": text[:200], "url": abs_url2, "type": ftype, "method": "detail"})

                # Also try clicking download buttons on detail page
                for selector in download_selectors[:5]:
                    try:
                        elements = page.query_selector_all(selector)
                        for el in elements[:5]:
                            try:
                                with page.expect_download(timeout=3000) as dl_info:
                                    el.click()
                                d = dl_info.value
                                d.cancel()
                            except Exception:
                                pass
                    except Exception:
                        pass

                for dl2 in downloads_captured:
                    if dl2["url"] not in seen_urls:
                        seen_urls.add(dl2["url"])
                        found_files.append(dl2)

            except Exception:
                pass

    except Exception as e:
        print(f"  Page error: {str(e)[:80]}")
    finally:
        page.close()

    return found_files


def main():
    with open("reports/kr_failure_analysis.json") as f:
        data = json.load(f)

    ag_sites = [(r["name"], r["url"], r["gpt_analysis"].get("cause", "?"))
                for r in data["results"]
                if r["gpt_analysis"].get("cause") in ("A", "G")]

    print(f"Retrying {len(ag_sites)} A/G sites with Playwright interception...\n")

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)

    results = {"success": [], "failed": [], "total_files": 0}

    for i, (name, url, cause) in enumerate(ag_sites):
        print(f"[{i+1}/{len(ag_sites)}] {name} (cause={cause})")

        files = find_files_with_interception(url, browser, timeout=20)

        if files:
            types = {}
            for f in files:
                types[f["type"]] = types.get(f["type"], 0) + 1
            type_str = ", ".join(f"{t}:{c}" for t, c in types.items())
            print(f"  OK: {len(files)}개 파일 ({type_str})")
            results["success"].append({"name": name, "url": url, "files": len(files), "types": types})
            results["total_files"] += len(files)
        else:
            print(f"  STILL FAIL")
            results["failed"].append({"name": name, "url": url})

    browser.close()
    pw.stop()

    report_path = f"reports/kr_ag_retry_{datetime.now():%Y%m%d_%H%M%S}.json"
    with open(report_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*60}")
    print(f"DONE: {len(results['success'])} 성공, {len(results['failed'])} 실패")
    print(f"추가 파일: {results['total_files']}개")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()

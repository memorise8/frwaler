# -*- coding: utf-8 -*-
"""CLI entry point for the multi-site crawler."""

import argparse
import os
import sys

from . import db as db_module
from .sites import CRAWLERS

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'papers.db')


def cmd_crawl(args, conn):
    site_id = args.site_id
    if site_id not in CRAWLERS:
        print(f"Unknown site: {site_id!r}. Available: {', '.join(CRAWLERS)}")
        sys.exit(1)

    crawler_cls = CRAWLERS[site_id]
    delay = 2.5 if site_id == "mohw" else 1.0
    crawler = crawler_cls(db_conn=conn, delay=delay)
    limit = args.limit
    search = getattr(args, 'search', None)
    doc_type = getattr(args, 'doc_type', None)
    date_from = getattr(args, 'date_from', None)
    date_to = getattr(args, 'date_to', None)

    info = f"Starting crawl for '{site_id}'"
    if limit:
        info += f" (limit={limit})"
    if search:
        info += f" (search={search})"
    if doc_type:
        info += f" (doc_type={doc_type})"
    if date_from or date_to:
        info += f" (date: {date_from or '...'} ~ {date_to or '...'})"
    incremental = getattr(args, 'incremental', False)
    if incremental:
        info += " (incremental)"
    print(info + " ...")

    # Pass extra params if the crawler supports them
    import inspect
    crawl_params = inspect.signature(crawler.crawl).parameters
    kwargs = {"limit": limit}
    if "search" in crawl_params and search:
        kwargs["search"] = search
    if "doc_type" in crawl_params and doc_type:
        kwargs["doc_type"] = doc_type
    if "date_from" in crawl_params and date_from:
        kwargs["date_from"] = date_from
    if "date_to" in crawl_params and date_to:
        kwargs["date_to"] = date_to
    if "incremental" in crawl_params and incremental:
        kwargs["incremental"] = incremental
    crawler.crawl(**kwargs)


def cmd_list_sites(args, conn):
    rows = db_module.get_stats(conn)
    if not rows:
        print("No sites registered yet.")
        return
    print(f"{'ID':<12} {'Name':<30} {'Papers':>8}")
    print("-" * 52)
    for row in rows:
        print(f"{row['id']:<12} {row['name']:<30} {row['paper_count']:>8}")


def cmd_stats(args, conn):
    rows = db_module.get_stats(conn)
    if not rows:
        print("No data yet.")
        return
    total = 0
    print(f"{'Site ID':<12} {'Site Name':<30} {'Papers':>8}")
    print("-" * 52)
    for row in rows:
        print(f"{row['id']:<12} {row['name']:<30} {row['paper_count']:>8}")
        total += row["paper_count"]
    print("-" * 52)
    print(f"{'TOTAL':<42} {total:>8}")

    # Download status
    dl_rows = db_module.get_download_stats(conn)
    if dl_rows:
        print(f"\n{'Site ID':<12} {'Downloaded':>10} {'Failed':>8} {'Pending':>8} {'No File':>8}")
        print("-" * 50)
        for r in dl_rows:
            print(f"{r['site_id']:<12} {r['downloaded']:>10} {r['failed']:>8} {r['pending']:>8} {r['no_file']:>8}")


def cmd_download(args, conn):
    """Download all files (HWP/PDF) for a site."""
    site_id = args.site_id if hasattr(args, 'site_id') and args.site_id else None
    limit = args.limit if hasattr(args, 'limit') else None
    retry = getattr(args, 'retry', False)

    import subprocess

    query = "SELECT id, site_id, external_id, title, pdf_url FROM papers WHERE pdf_url IS NOT NULL AND pdf_url != ''"
    params = []
    if site_id:
        query += " AND site_id = ?"
        params.append(site_id)
    if retry:
        query += " AND download_status = 'failed'"
    else:
        query += " AND (download_status IS NULL OR download_status = 'failed')"
    if limit:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    label = "retry failed" if retry else "to download"
    print(f"Found {len(rows)} papers {label}")

    downloaded = 0
    failed = 0
    for i, row in enumerate(rows, 1):
        pdf_url = row["pdf_url"]
        s_id = row["site_id"]
        ext_id = row["external_id"]
        title = row["title"] or ""
        paper_id = row["id"]

        # Determine extension
        if '.hwpx' in pdf_url.lower():
            ext = '.hwpx'
        elif '.hwp' in pdf_url.lower():
            ext = '.hwp'
        elif '.pdf' in pdf_url.lower():
            ext = '.pdf'
        else:
            ext = ''

        downloads_dir = os.path.join(os.path.dirname(__file__), '..', 'downloads', s_id)
        os.makedirs(downloads_dir, exist_ok=True)
        local_path = os.path.join(downloads_dir, f"{ext_id}{ext}")

        if os.path.exists(local_path):
            db_module.update_download_status(conn, paper_id, "downloaded", local_path)
            downloaded += 1
            continue

        print(f"  [{i}/{len(rows)}] {title[:50]}...")
        # Download via curl
        try:
            result = subprocess.run(
                ["curl", "-sL", "--max-time", "30", "-o", local_path, pdf_url],
                capture_output=True, timeout=35,
            )
            if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
                downloaded += 1
                db_module.update_download_status(conn, paper_id, "downloaded", local_path)
                print(f"    Saved: {os.path.basename(local_path)}")
            else:
                failed += 1
                db_module.update_download_status(conn, paper_id, "failed")
                if os.path.exists(local_path):
                    os.remove(local_path)
                print(f"    FAILED")
        except Exception:
            failed += 1
            db_module.update_download_status(conn, paper_id, "failed")
            print(f"    FAILED")

    print(f"\nDone. Downloaded: {downloaded}, Failed: {failed}, Total: {len(rows)}")


def cmd_test_config(args, conn):
    site_id = args.site_id
    limit = args.limit or 3
    if site_id not in CRAWLERS:
        from .sites import CRAWLERS as reloaded
        if site_id not in reloaded:
            print(f"Unknown site: {site_id!r}. Check configs/.")
            sys.exit(1)
        crawler_cls = reloaded[site_id]
    else:
        crawler_cls = CRAWLERS[site_id]

    print(f"Testing config '{site_id}' with {limit} items...")
    crawler = crawler_cls(db_conn=conn, delay=1.5)
    saved = crawler.crawl(limit=limit)
    if saved > 0:
        print(f"\nTest passed. {saved} items crawled successfully.")
    else:
        print(f"\nTest failed. No items could be crawled. Check the config.")


def cmd_convert(args, conn):
    """Convert downloaded files (HWP/PDF) to Markdown."""
    from .converter import convert_site_files
    site_id = getattr(args, "site_id", None)
    limit = getattr(args, "limit", None)
    convert_site_files(conn, site_id=site_id, limit=limit)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m crawler.main",
        description="Multi-site document crawler",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # crawl
    crawl_parser = subparsers.add_parser("crawl", help="Crawl a specific site")
    crawl_parser.add_argument("site_id", help="Site to crawl")
    crawl_parser.add_argument("--limit", type=int, default=None, help="Max number of papers to save")
    crawl_parser.add_argument("--search", type=str, default=None, help="Keyword search filter")
    crawl_parser.add_argument("--doc-type", type=str, default=None, help="Document type code (e.g. 001_02)")
    crawl_parser.add_argument("--date-from", type=str, default=None, help="Start date filter (YYYY-MM-DD)")
    crawl_parser.add_argument("--date-to", type=str, default=None, help="End date filter (YYYY-MM-DD)")
    crawl_parser.add_argument("--incremental", action="store_true", default=False,
                              help="Only crawl new documents not already in DB")

    # list-sites
    subparsers.add_parser("list-sites", help="List registered sites and paper counts")

    # stats
    subparsers.add_parser("stats", help="Show per-site paper counts")

    # test-config
    test_parser = subparsers.add_parser("test-config", help="Test a generated crawler config")
    test_parser.add_argument("site_id", help="Site ID to test")
    test_parser.add_argument("--limit", type=int, default=3, help="Number of items to test (default: 3)")

    # download
    download_parser = subparsers.add_parser("download", help="Download files (HWP/PDF) for papers")
    download_parser.add_argument("site_id", nargs="?", default=None, help="Site to download files for")
    download_parser.add_argument("--limit", type=int, default=None, help="Max files to download")
    download_parser.add_argument("--retry", action="store_true", help="Retry only previously failed downloads")

    # convert
    convert_parser = subparsers.add_parser("convert", help="Convert downloaded files (HWP/PDF) to Markdown")
    convert_parser.add_argument("site_id", nargs="?", default=None, help="Site to convert files for")
    convert_parser.add_argument("--limit", type=int, default=None, help="Max files to convert")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    conn = db_module.get_db(DB_PATH)
    db_module.init_db(conn)

    # Register all known sites
    for site_id, crawler_cls in CRAWLERS.items():
        instance = crawler_cls.__new__(crawler_cls)
        db_module.register_site(conn, site_id, crawler_cls.site_name, crawler_cls.base_url)

    dispatch = {
        "crawl": cmd_crawl,
        "list-sites": cmd_list_sites,
        "stats": cmd_stats,
        "test-config": cmd_test_config,
        "download": cmd_download,
        "convert": cmd_convert,
    }
    dispatch[args.command](args, conn)
    conn.close()


if __name__ == "__main__":
    main()

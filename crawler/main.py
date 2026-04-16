# -*- coding: utf-8 -*-
"""CLI entry point for the multi-site crawler."""

import argparse
import sys

from . import db as db_module
from .sites import CRAWLERS
from . import summarizer as summarizer_module

DB_PATH = "/data_raid/ruci_workspace/crawler-poc/data/papers.db"


def cmd_crawl(args, conn):
    site_id = args.site_id
    if site_id not in CRAWLERS:
        print(f"Unknown site: {site_id!r}. Available: {', '.join(CRAWLERS)}")
        sys.exit(1)

    crawler_cls = CRAWLERS[site_id]
    # MOHW (government site) needs longer delay to avoid being blocked
    delay = 2.5 if site_id == "mohw" else 1.0
    crawler = crawler_cls(db_conn=conn, delay=delay)
    limit = args.limit
    print(f"Starting crawl for '{site_id}'" + (f" (limit={limit})" if limit else "") + " ...")
    crawler.crawl(limit=limit)


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

    import os
    from .summarizer import download_file_curl

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
        if download_file_curl(pdf_url, local_path):
            downloaded += 1
            db_module.update_download_status(conn, paper_id, "downloaded", local_path)
            print(f"    Saved: {os.path.basename(local_path)}")
        else:
            failed += 1
            db_module.update_download_status(conn, paper_id, "failed")
            print(f"    FAILED")

    print(f"\nDone. Downloaded: {downloaded}, Failed: {failed}, Total: {len(rows)}")


def cmd_summarize(args, conn):
    site_id = getattr(args, "site_id", None)
    limit = getattr(args, "limit", None)
    summarizer_module.summarize_papers(conn, site_id=site_id, limit=limit)


def cmd_analyze(args, conn):
    from .analyzer import analyze_site
    url = args.url
    site_id = getattr(args, "site_id", None)
    site_name = getattr(args, "site_name", None)
    analyze_site(url, site_id=site_id, site_name=site_name)


def cmd_test_config(args, conn):
    site_id = args.site_id
    limit = args.limit or 3
    if site_id not in CRAWLERS:
        # Reload CRAWLERS in case config was just created
        from .sites import CRAWLERS as reloaded
        if site_id not in reloaded:
            print(f"Unknown site: {site_id!r}. Run 'analyze' first or check configs/.")
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


def cmd_product_stats(args, conn):
    """Show product crawling statistics."""
    site_id = getattr(args, "site_id", None)
    db_module.init_db(conn)
    rows = db_module.get_product_stats(conn, site_id)
    if not rows:
        print("No product data found.")
        return
    print(f"\n{'Site ID':<30} {'Total':>8} {'HTML Saved':>12}")
    print("-" * 52)
    total_products = 0
    total_html = 0
    for row in rows:
        print(f"{row['site_id']:<30} {row['total']:>8} {row['html_saved']:>12}")
        total_products += row['total']
        total_html += row['html_saved']
    print("-" * 52)
    print(f"{'TOTAL':<30} {total_products:>8} {total_html:>12}")


def cmd_promote_heritage(args, conn):
    """Promote qualifying crawler products into the heritage_parts table."""
    import os
    from pro_server.services.heritage_importer import promote_heritage

    papers_db = getattr(args, "papers_db", None) or DB_PATH
    screening_db = getattr(args, "screening_db", None) or os.path.join(
        os.path.dirname(DB_PATH), "screening.db"
    )
    print(f"Promoting heritage parts: {papers_db} → {screening_db}")
    count = promote_heritage(papers_db, screening_db)
    print(f"Promoted {count} parts.")


def cmd_batch_add(args, conn):
    """Batch analyze URLs and run auto-add."""
    from .batch import batch_analyze
    filepath = args.file
    check_only = getattr(args, "check_only", False)
    verbose = getattr(args, "verbose", False)

    # Read URLs from file (skip empty lines and comments)
    with open(filepath, encoding="utf-8") as f:
        urls = [line.strip() for line in f
                if line.strip() and not line.strip().startswith("#")]

    if not urls:
        print("URL이 없습니다.")
        return

    force_browser = getattr(args, "browser", False)
    batch_analyze(urls, check_only=check_only, verbose=verbose, force_browser=force_browser)


def cmd_auto_add(args, conn):
    from .agent import AutoAddAgent
    agent = AutoAddAgent(
        max_iterations=getattr(args, "max_iterations", 10),
        verbose=getattr(args, "verbose", False),
        force_browser=getattr(args, "browser", False),
    )
    result = agent.run(args.url, site_id=getattr(args, "site_id", None),
                       site_name=getattr(args, "site_name", None))
    if result.get("success"):
        site_id = result["site_id"]
        method = result.get("method", "unknown")
        print(f"\nSuccessfully created crawler for '{site_id}' (method: {method})")
        print(f"Run: python -m crawler.main crawl {site_id}")
    else:
        print(f"\nFailed: {result.get('reason', 'Unknown error')}")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m crawler.main",
        description="Multi-site academic paper crawler",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # crawl
    crawl_parser = subparsers.add_parser("crawl", help="Crawl a specific site")
    crawl_parser.add_argument("site_id", help="Site to crawl")
    crawl_parser.add_argument("--limit", type=int, default=None, help="Max number of papers to save")

    # list-sites
    subparsers.add_parser("list-sites", help="List registered sites and paper counts")

    # stats
    subparsers.add_parser("stats", help="Show per-site paper counts")

    # summarize
    summarize_parser = subparsers.add_parser("summarize", help="Summarize papers using AI")
    summarize_parser.add_argument("site_id", nargs="?", default=None, help="Filter by site ID (optional)")
    summarize_parser.add_argument("--limit", type=int, default=None, help="Max number of papers to summarize")

    # analyze
    analyze_parser = subparsers.add_parser("analyze", help="Analyze a website and generate crawler config")
    analyze_parser.add_argument("url", help="URL of the list/board page to analyze")
    analyze_parser.add_argument("--site-id", default=None, help="Custom site identifier")
    analyze_parser.add_argument("--site-name", default=None, help="Human-readable site name")

    # test-config
    test_parser = subparsers.add_parser("test-config", help="Test a generated crawler config")
    test_parser.add_argument("site_id", help="Site ID to test")
    test_parser.add_argument("--limit", type=int, default=3, help="Number of items to test (default: 3)")

    # auto-add
    auto_parser = subparsers.add_parser("auto-add", help="Autonomously analyze a site and create a working crawler using AI agent")
    auto_parser.add_argument("url", help="URL of the list/board page")
    auto_parser.add_argument("--site-id", default=None, help="Custom site identifier")
    auto_parser.add_argument("--site-name", default=None, help="Human-readable site name")
    auto_parser.add_argument("--max-iterations", type=int, default=10, help="Max agent iterations (default: 10)")
    auto_parser.add_argument("--verbose", action="store_true", help="Show full GPT reasoning")
    auto_parser.add_argument("--browser", action="store_true", help="Force browser_fetch first (for SPA/React/Angular sites)")

    # download
    download_parser = subparsers.add_parser("download", help="Download files (HWP/PDF) for papers")
    download_parser.add_argument("site_id", nargs="?", default=None, help="Site to download files for")
    download_parser.add_argument("--limit", type=int, default=None, help="Max files to download")
    download_parser.add_argument("--retry", action="store_true", help="Retry only previously failed downloads")

    # convert
    convert_parser = subparsers.add_parser("convert", help="Convert downloaded files (HWP/PDF) to Markdown")
    convert_parser.add_argument("site_id", nargs="?", default=None, help="Site to convert files for")
    convert_parser.add_argument("--limit", type=int, default=None, help="Max files to convert")

    # product-stats
    product_stats_parser = subparsers.add_parser("product-stats", help="Show product crawling statistics")
    product_stats_parser.add_argument("site_id", nargs="?", default=None, help="Filter by site ID (optional)")

    # promote-heritage
    ph_parser = subparsers.add_parser(
        "promote-heritage",
        help="Promote qualifying crawler products into the heritage_parts screening table",
    )
    ph_parser.add_argument(
        "--papers-db", default=None,
        help="Path to crawler SQLite DB (default: same as crawl DB_PATH)",
    )
    ph_parser.add_argument(
        "--screening-db", default=None,
        help="Path to screening SQLite DB (default: <papers-db-dir>/screening.db)",
    )

    # batch-add
    batch_parser = subparsers.add_parser("batch-add", help="Batch analyze URLs and auto-add crawlers")
    batch_parser.add_argument("file", help="Text file with URLs (one per line)")
    batch_parser.add_argument("--check-only", action="store_true", help="Only check accessibility and structure (no GPT cost)")
    batch_parser.add_argument("--verbose", action="store_true", help="Show detailed GPT agent output")
    batch_parser.add_argument("--browser", action="store_true", help="Force browser_fetch for all sites")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    conn = db_module.get_db(DB_PATH)
    db_module.init_db(conn)

    # Register all known sites
    for site_id, crawler_cls in CRAWLERS.items():
        # Instantiate briefly to read properties (no DB writes yet)
        instance = crawler_cls.__new__(crawler_cls)
        db_module.register_site(conn, site_id, crawler_cls.site_name, crawler_cls.base_url)

    dispatch = {
        "crawl": cmd_crawl,
        "list-sites": cmd_list_sites,
        "stats": cmd_stats,
        "summarize": cmd_summarize,
        "analyze": cmd_analyze,
        "test-config": cmd_test_config,
        "auto-add": cmd_auto_add,
        "download": cmd_download,
        "convert": cmd_convert,
        "batch-add": cmd_batch_add,
        "product-stats": cmd_product_stats,
        "promote-heritage": cmd_promote_heritage,
    }
    dispatch[args.command](args, conn)
    conn.close()


if __name__ == "__main__":
    main()

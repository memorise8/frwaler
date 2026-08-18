# -*- coding: utf-8 -*-
"""CLI entry point for the multi-site crawler."""

import argparse
import os
import sys

from . import db as db_module
from . import storage as storage_module
from .base_crawler import CrawlUpToDate
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
    gap_fill = getattr(args, 'gap_fill', False)
    if gap_fill:
        info += " (gap-fill)"
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
    try:
        if gap_fill and hasattr(crawler, 'gap_fill'):
            gf_kwargs = {}
            if doc_type:
                gf_kwargs["doc_type"] = doc_type
            crawler.gap_fill(**gf_kwargs)
        else:
            crawler.crawl(**kwargs)
    except CrawlUpToDate:
        # CrawlControl(BaseException) 이라 그냥 두면 트레이스백을 찍고 죽는다.
        # newest_first 크롤러(12개) 가 이미 아는 최신 문서에 닿았다는 정상
        # 신호이므로, 실패가 아니라 조기 종료로 보고한다.
        print(f"[{site_id}] 신규분 소진 — 조기 종료(정상)")


def cmd_scan_index(args, conn):
    """Build/refresh doc_index by scanning the list API only (no detail fetch).

    Only supported on crawlers that define a ``scan_index`` method.
    """
    site_id = args.site_id
    if site_id not in CRAWLERS:
        print(f"Unknown site: {site_id!r}. Available: {', '.join(CRAWLERS)}")
        sys.exit(1)

    crawler_cls = CRAWLERS[site_id]
    delay = 2.5 if site_id == "mohw" else 1.0
    crawler = crawler_cls(db_conn=conn, delay=delay)
    if not hasattr(crawler, "scan_index"):
        print(f"Site '{site_id}' does not support scan-index.")
        sys.exit(1)

    doc_type = getattr(args, "doc_type", None)
    no_mark_deleted = getattr(args, "no_mark_deleted", False)

    print(f"Starting scan-index for '{site_id}'"
          f"{' (doc_type=' + doc_type + ')' if doc_type else ''}"
          f"{' (no-mark-deleted)' if no_mark_deleted else ''} ...")
    crawler.scan_index(doc_type=doc_type, mark_deleted=not no_mark_deleted)


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
    """Download attachment files for documents into the 12-digit libertree layout.

    파일명은 항상 ``{12자리 ID}.pdf`` (또는 .hwp/.hwpx)이며 경로는
    ``data/AAAA/BBBB/AAAABBBBCCCC.pdf`` 이다. 원본 파일명은
    ``documents.original_filename`` 컬럼에 그대로 보존된다.
    """
    site_id = args.site_id if hasattr(args, 'site_id') and args.site_id else None
    limit = args.limit if hasattr(args, 'limit') else None
    retry = getattr(args, 'retry', False)

    import subprocess

    query = "SELECT id, site_id, external_id, title, pdf_url, original_filename FROM documents WHERE pdf_url IS NOT NULL AND pdf_url != ''"
    params = []
    if site_id:
        query += " AND site_id = ?"
        params.append(site_id)
    if retry:
        query += " AND download_status = 'failed'"
    else:
        query += " AND (download_status IS NULL OR download_status = 'pending' OR download_status = 'failed')"
    query += " ORDER BY id"
    if limit:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    label = "retry failed" if retry else "to download"
    print(f"Found {len(rows)} documents {label}")

    def _ext_from_url(url: str) -> str | None:
        """Return ``.pdf``/``.hwp``/``.hwpx`` if the URL has an obvious extension, else None."""
        u = url.lower()
        if ".hwpx" in u:
            return ".hwpx"
        if ".hwp" in u:
            return ".hwp"
        if ".pdf" in u:
            return ".pdf"
        return None

    downloaded = 0
    failed = 0
    for i, row in enumerate(rows, 1):
        pdf_url = row["pdf_url"]
        doc_id = row["id"]
        title = row["title"] or ""

        # Original filename: prefer existing column, fall back to URL last segment.
        original_filename = row["original_filename"]
        if not original_filename:
            tail = pdf_url.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
            if "." in tail and len(tail) <= 200:
                original_filename = tail

        # If the URL declares an extension we trust it; otherwise sniff after download.
        url_ext = _ext_from_url(pdf_url)

        # Idempotent: if any of the canonical paths already has content, skip.
        existing_ext = None
        for candidate in (".pdf", ".hwp", ".hwpx", ".bin"):
            cand_path = storage_module.doc_id_to_path(doc_id, candidate)
            if cand_path.exists() and cand_path.stat().st_size > 0:
                existing_ext = candidate
                break
        if existing_ext is not None:
            db_module.update_document_paths(
                conn, doc_id,
                pdf_path=storage_module.doc_id_to_relative(doc_id, existing_ext),
                download_status="downloaded",
                original_filename=original_filename,
            )
            downloaded += 1
            continue

        print(f"  [{i}/{len(rows)}] #{doc_id} {title[:50]}...")

        # Stage download to .tmp so a partial transfer can't corrupt the canonical name.
        tmp_path = storage_module.doc_id_to_path(doc_id, ".tmp")
        storage_module.ensure_parent(tmp_path)
        try:
            subprocess.run(
                ["curl", "-sL", "--max-time", "30", "-o", str(tmp_path), pdf_url],
                capture_output=True, timeout=35,
            )
            if not (tmp_path.exists() and tmp_path.stat().st_size > 0):
                failed += 1
                db_module.update_document_paths(conn, doc_id, download_status="failed")
                if tmp_path.exists():
                    tmp_path.unlink()
                print("    FAILED (empty)")
                continue

            # Decide extension: URL hint > magic-byte sniff.
            if url_ext is not None:
                ext = url_ext
            else:
                with open(tmp_path, "rb") as f:
                    head = f.read(8)
                ext = storage_module.detect_extension(head)

            final_path = storage_module.doc_id_to_path(doc_id, ext)
            storage_module.ensure_parent(final_path)
            os.replace(tmp_path, final_path)

            rel_path = storage_module.doc_id_to_relative(doc_id, ext)
            downloaded += 1
            db_module.update_document_paths(
                conn, doc_id,
                pdf_path=str(rel_path),
                download_status="downloaded",
                original_filename=original_filename,
            )
            print(f"    Saved: {final_path.name} ({final_path.stat().st_size} bytes)")
        except Exception as exc:
            failed += 1
            db_module.update_document_paths(conn, doc_id, download_status="failed")
            if tmp_path.exists():
                try: tmp_path.unlink()
                except OSError: pass
            print(f"    FAILED ({type(exc).__name__})")

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


def cmd_summarize(args, conn):
    """Generate Korean 3~5 sentence summaries for documents missing one.

    Source text precedence per row:
      1. ``documents.abstract`` (if non-empty)
      2. The on-disk text at ``documents.txt_path`` (if available)
      3. ``documents.title`` only (last resort)
    """
    from . import summarizer
    from . import storage as storage_module

    site_id = getattr(args, "site_id", None)
    limit = getattr(args, "limit", None)
    provider = getattr(args, "provider", None)
    doc_id_arg = getattr(args, "doc_id", None)

    if doc_id_arg is not None:
        # Targeted single-row mode (used by the UI's "재요약" button).
        rows = conn.execute(
            "SELECT id, site_id, title, abstract, txt_path "
            "FROM documents WHERE id = ?",
            (doc_id_arg,),
        ).fetchall()
        if not rows:
            print(f"No document with id={doc_id_arg}")
            return
    else:
        rows = db_module.get_documents_without_summary(
            conn, site_id=site_id, limit=limit,
        )

    print(f"Found {len(rows)} documents to summarize "
          f"(provider={provider or os.environ.get('LLM_PROVIDER') or 'gpt'})")

    done = 0
    skipped = 0
    failed = 0
    for i, row in enumerate(rows, 1):
        doc_id = row["id"]
        title = (row["title"] or "").strip() or None
        abstract = (row["abstract"] or "").strip() if "abstract" in row.keys() else ""
        txt_rel = row["txt_path"] if "txt_path" in row.keys() else None

        text = abstract
        if not text and txt_rel:
            try:
                # txt_path is a relative path like ``data/AAAA/BBBB/N.txt``.
                # Honour LIBERTREE_DATA_ROOT by deriving the absolute path
                # from the doc id rather than treating the stored path as
                # absolute.
                abs_path = storage_module.doc_id_to_txt_path(doc_id)
                if abs_path.exists():
                    with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
            except Exception as exc:
                print(f"  [{i}/{len(rows)}] #{doc_id} txt read failed: {exc}")
        if not text:
            text = title or ""

        if not text.strip():
            skipped += 1
            print(f"  [{i}/{len(rows)}] #{doc_id} SKIP (no source text)")
            continue

        summary = summarizer.summarize_text(text, title=title, provider=provider)
        if not summary:
            failed += 1
            if i <= 10 or i % 50 == 0:
                print(f"  [{i}/{len(rows)}] #{doc_id} FAILED (provider returned none)")
            continue

        db_module.update_document_summary(conn, doc_id, summary)
        done += 1
        if i <= 10 or i % 50 == 0:
            preview = summary.replace("\n", " ")[:60]
            print(f"  [{i}/{len(rows)}] #{doc_id} OK ({len(summary)} chars) {preview}…")

    print(f"\nDone. Summarized: {done}, Skipped: {skipped}, Failed: {failed}, "
          f"Total: {len(rows)}")


def cmd_smart_find(args, conn):
    """Find downloadable documents from any URL using smart detection + optional LLM.

    Streams NDJSON events to stdout:
      {"type":"progress","message":"..."}
      {"type":"result","url":"...","documents":[...],"pages_scanned":N,...}
      {"type":"db_saved","count":N,"site_id":"sf-..."}  (when --save-db)
      {"type":"error","message":"..."}
    """
    import json as _json
    import uuid
    from urllib.parse import urlparse
    from .smart_finder import SmartDocumentFinder

    def emit(event_type, **data):
        print(_json.dumps({"type": event_type, **data}, ensure_ascii=False), flush=True)

    def on_progress(msg):
        emit("progress", message=msg)

    try:
        finder = SmartDocumentFinder(
            delay=args.delay,
            callback=on_progress,
            llm_provider=args.provider,
        )
        result = finder.find(
            args.url,
            max_pages=args.max_pages,
            max_depth=args.max_depth,
            use_ai=not args.no_ai,
        )
    except Exception as e:
        emit("error", message=f"find failed: {e}")
        sys.exit(1)

    docs = [
        {
            "id": d.id,
            "title": d.title,
            "file_url": d.file_url,
            "file_type": d.file_type,
            "source_page": d.source_page,
            "date": getattr(d, "date", None),
        }
        for d in result.documents
    ]
    emit(
        "result",
        url=result.url,
        documents=docs,
        pages_scanned=result.pages_scanned,
        detail_pages_visited=result.detail_pages_visited,
        fetch_method=result.fetch_method,
        errors=result.errors,
    )

    if args.save_db and result.documents:
        parsed = urlparse(args.url)
        site_id = f"sf-{parsed.netloc.replace('www.', '').replace('.', '-')}"
        site_name = f"Smart Find: {parsed.netloc}"
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        db_module.register_site(conn, site_id, site_name, base_url)

        from .libertree_adapter import paper_to_document
        saved = 0
        for d in result.documents:
            paper = {
                "site_id": site_id,
                "external_id": d.id,
                "title": d.title,
                "authors": "[]",
                "abstract": "",
                "category": d.file_type,
                "keywords": "[]",
                "published_date": getattr(d, "date", "") or "",
                "url": d.source_page,
                "pdf_url": d.file_url,
                "doi": "",
                "department": "",
                "metadata": _json.dumps({"finder": "smart_find"}, ensure_ascii=False),
            }
            try:
                doc = paper_to_document(paper)
                db_module.upsert_document(conn, doc)
                # Do NOT fill pdf_path/txt_path here. cmd_download fills pdf_path on
                # successful download, convert_site_files fills txt_path on successful
                # conversion. Setting txt_path upfront would break the
                # ``txt_path IS NULL`` marker used by get_documents_pending_convert.
                saved += 1
            except Exception as exc:
                emit("error", message=f"db save error: {exc}")
        emit("db_saved", count=saved, site_id=site_id)


def cmd_auto_add(args, conn):
    """Autonomous GPT agent: analyze a URL and generate a GenericCrawler JSON config.

    Streams NDJSON events to stdout:
      {"type":"progress","message":"..."}
      {"type":"result","success":true|false,"site_id":"...","items_found":N,"config_path":"...","reason":"..."}
      {"type":"error","message":"..."}
    """
    import json as _json

    def emit(event_type, **data):
        print(_json.dumps({"type": event_type, **data}, ensure_ascii=False), flush=True)

    # Guard: require OPENAI_API_KEY before importing agent (which calls load_dotenv)
    import os as _os
    # Load .env first so the key can come from there
    try:
        from dotenv import load_dotenv as _load_dotenv
        _env_path = _os.path.join(_os.path.dirname(__file__), ".env")
        _load_dotenv(_env_path)
    except Exception:
        pass

    if not _os.environ.get("OPENAI_API_KEY"):
        emit("error", message="OPENAI_API_KEY not set in environment or crawler/.env")
        sys.exit(1)

    try:
        from .agent import AutoAddAgent
    except Exception as e:
        emit("error", message=f"Failed to import AutoAddAgent: {e}")
        sys.exit(1)

    url = args.url
    site_id = getattr(args, "site_id", None) or None
    dry_run = getattr(args, "dry_run", False)
    force_browser = getattr(args, "browser", False)

    if dry_run:
        emit("progress", message=f"[dry-run] Would analyze: {url}")
        emit("result", success=True, site_id=site_id or "dry-run", items_found=0,
             config_path=None, reason="dry-run mode — no API calls made")
        return

    def on_progress(msg):
        emit("progress", message=msg)

    max_iter = getattr(args, "max_iterations", None)
    try:
        agent = AutoAddAgent(
            max_iterations=max_iter,
            verbose=True,
            force_browser=force_browser,
        )
    except RuntimeError as e:
        emit("error", message=str(e))
        sys.exit(1)

    # Monkey-patch _report_progress to also emit NDJSON progress events
    _orig_report = agent._report_progress

    def _patched_report(name, a, result):
        _orig_report(name, a, result)
        # Derive a human-readable status message for the NDJSON stream
        if name in ("fetch_page", "curl_fetch", "cloudscraper_fetch", "browser_fetch"):
            status = "ok" if result.get("success") else "failed"
            emit("progress", message=f"[{name}] {a.get('url','')[:80]} -> {status}")
        elif name == "save_config":
            sid = (a.get("config") or {}).get("site_id", "?") if isinstance(a.get("config"), dict) else "?"
            emit("progress", message=f"[save_config] site_id={sid}")
        elif name == "test_crawl":
            count = result.get("count", 0)
            emit("progress", message=f"[test_crawl] {count} items crawled")

    agent._report_progress = _patched_report

    try:
        result = agent.run(url, site_id=site_id)
    except Exception as e:
        emit("error", message=f"Agent error: {e}")
        sys.exit(1)

    import os as _os2
    config_path = None
    out_site_id = result.get("site_id", site_id or "unknown")
    if result.get("success"):
        candidate = _os2.path.join(_os2.path.dirname(__file__), "sites", "configs", f"{out_site_id}.json")
        if _os2.path.exists(candidate):
            config_path = candidate

    emit(
        "result",
        success=result.get("success", False),
        site_id=out_site_id,
        items_found=result.get("items_found", 0),
        config_path=config_path,
        reason=result.get("reason", ""),
    )

    if not result.get("success"):
        sys.exit(1)


def cmd_auto_add_codex(args, conn):
    """Tier 2: Escalate to Codex CLI for autonomous custom crawler generation.

    Streams NDJSON events to stdout:
      {"type":"progress","message":"..."}
      {"type":"result","success":bool,"site_id":"...","file_path":"...","test_crawl":{...},"error":"..."}
    """
    import json as _json

    def emit(event_type, **data):
        print(_json.dumps({"type": event_type, **data}, ensure_ascii=False), flush=True)

    # Guard key presence
    import os as _os
    try:
        from dotenv import load_dotenv as _load_dotenv
        _load_dotenv(_os.path.join(_os.path.dirname(__file__), ".env"))
    except Exception:
        pass
    if not _os.environ.get("OPENAI_API_KEY"):
        emit("error", message="OPENAI_API_KEY not set in environment or crawler/.env")
        sys.exit(1)

    from .codex_runner import run_codex_crawler_build

    def on_stream(line: str):
        emit("progress", message=line.rstrip())

    try:
        result = run_codex_crawler_build(
            url=args.url,
            site_id=getattr(args, "site_id", None) or None,
            site_name=getattr(args, "site_name", None) or None,
            max_timeout_seconds=getattr(args, "timeout_seconds", 1200),
            stream_cb=on_stream,
        )
    except Exception as e:
        emit("error", message=f"codex_runner failed: {e}")
        sys.exit(1)

    # Emit final result (redact test_crawl samples length only)
    tc = result.get("test_crawl")
    emit(
        "result",
        success=result.get("success", False),
        site_id=result.get("site_id"),
        file_path=result.get("file_path"),
        elapsed_seconds=result.get("elapsed_seconds"),
        codex_log_path=result.get("codex_log_path"),
        quality=(tc or {}).get("quality"),
        count=(tc or {}).get("count"),
        error=result.get("error"),
    )


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
    crawl_parser.add_argument("--gap-fill", action="store_true", default=False,
                              help="Fast gap fill: scan for missing DOC_IDs then fetch only those")

    # scan-index
    scan_parser = subparsers.add_parser(
        "scan-index",
        help="Scan list API only to build/refresh doc_index (no detail fetch)",
    )
    scan_parser.add_argument("site_id", help="Site to scan")
    scan_parser.add_argument("--doc-type", type=str, default=None,
                             help="Document type code filter (e.g. 001_02)")
    scan_parser.add_argument("--no-mark-deleted", action="store_true", default=False,
                             help="Do not mark unseen rows as deleted "
                                  "(useful for partial/filtered scans)")

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
    sum_parser = subparsers.add_parser(
        "summarize", help="Generate Korean summaries for documents missing one")
    sum_parser.add_argument(
        "site_id", nargs="?", default=None,
        help="Limit to one site (default: all sites)")
    sum_parser.add_argument(
        "--limit", type=int, default=None,
        help="Maximum rows to summarize this run")
    sum_parser.add_argument(
        "--provider", choices=["gpt", "openai", "gemini", "google"], default=None,
        help="LLM provider override (default: $LLM_PROVIDER or 'gpt')")
    sum_parser.add_argument(
        "--doc-id", dest="doc_id", type=int, default=None,
        help="Summarize a single document by id (e.g. for a UI re-trigger)")

    convert_parser = subparsers.add_parser("convert", help="Convert downloaded files (HWP/PDF) to Markdown")
    convert_parser.add_argument("site_id", nargs="?", default=None, help="Site to convert files for")
    convert_parser.add_argument("--limit", type=int, default=None, help="Max files to convert")

    # smart-find
    sf_parser = subparsers.add_parser(
        "smart-find",
        help="Find downloadable documents from any URL (emits NDJSON events)",
    )
    sf_parser.add_argument("url", help="Starting URL to scan")
    sf_parser.add_argument("--max-pages", type=int, default=20, help="Max list pages to scan (default: 20)")
    sf_parser.add_argument("--max-depth", type=int, default=3, help="Detail-link follow depth (default: 3)")
    sf_parser.add_argument("--delay", type=float, default=1.0, help="Inter-request delay seconds (default: 1.0)")
    sf_parser.add_argument("--provider", choices=["gpt", "gemini"], default=None,
                           help="LLM provider for AI fallback (default: env LLM_PROVIDER or gpt)")
    sf_parser.add_argument("--no-ai", action="store_true", help="Disable LLM fallback analysis")
    sf_parser.add_argument("--save-db", action="store_true", help="Save found documents to papers table")

    # auto-add
    aa_parser = subparsers.add_parser(
        "auto-add",
        help="Autonomous GPT agent: analyze a URL and generate a crawler config (emits NDJSON events)",
    )
    aa_parser.add_argument("url", help="URL of the list/board page to analyze")
    aa_parser.add_argument("--site-id", dest="site_id", default=None,
                           help="Override auto-generated site_id")
    aa_parser.add_argument("--browser", action="store_true",
                           help="Force headless browser fetch (for SPA sites)")
    aa_parser.add_argument("--dry-run", action="store_true",
                           help="Print what would happen without calling OpenAI")
    aa_parser.add_argument("--max-iterations", dest="max_iterations", type=int, default=None,
                           help="Max agent iterations (default: 15). Raise (e.g. 30) for complex sites")

    # auto-add-codex
    aac_parser = subparsers.add_parser(
        "auto-add-codex",
        help="Tier 2: autonomous crawler generation via Codex CLI",
    )
    aac_parser.add_argument("url")
    aac_parser.add_argument("--site-id", dest="site_id", default=None)
    aac_parser.add_argument("--site-name", dest="site_name", default=None)
    aac_parser.add_argument("--timeout-seconds", dest="timeout_seconds", type=int, default=1200)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    conn = db_module.get_db(DB_PATH)
    db_module.init_db(conn)

    # Register all known sites
    import inspect as _inspect
    for site_id, crawler_cls in CRAWLERS.items():
        try:
            db_module.register_site(conn, site_id, crawler_cls.site_name, crawler_cls.base_url)
        except Exception:
            pass

    dispatch = {
        "crawl": cmd_crawl,
        "scan-index": cmd_scan_index,
        "list-sites": cmd_list_sites,
        "stats": cmd_stats,
        "test-config": cmd_test_config,
        "download": cmd_download,
        "convert": cmd_convert,
        "summarize": cmd_summarize,
        "smart-find": cmd_smart_find,
        "auto-add": cmd_auto_add,
        "auto-add-codex": cmd_auto_add_codex,
    }
    dispatch[args.command](args, conn)
    conn.close()


if __name__ == "__main__":
    main()

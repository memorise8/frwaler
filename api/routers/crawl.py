import threading
from fastapi import APIRouter
from ..schemas import CrawlRequest, JobOut
from ..services.job_manager import create_job, update_job, get_job

router = APIRouter(prefix="/api", tags=["crawl"])


def _run_crawl(site_id: str, limit, job_id: str):
    """Run crawl in background thread (crawler is synchronous)."""
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

    from crawler.sites import CRAWLERS
    from crawler.db import get_db

    if site_id not in CRAWLERS:
        update_job(job_id, "failed", result=f"Unknown site: {site_id}")
        return

    try:
        conn = get_db()
        crawler_cls = CRAWLERS[site_id]
        crawler = crawler_cls(db_conn=conn)
        update_job(job_id, "running", progress="Crawling...")
        saved = crawler.crawl(limit=limit)
        conn.close()
        diagnosis = getattr(crawler, '_last_diagnosis', [])
        if saved == 0 and diagnosis:
            update_job(job_id, "completed", result=f"Saved 0 papers", diagnosis=diagnosis)
        else:
            update_job(job_id, "completed", result=f"Saved {saved} papers")
    except Exception as e:
        update_job(job_id, "failed", result=str(e)[:200])


@router.post("/crawl/{site_id}", response_model=JobOut)
async def start_crawl(site_id: str, req: CrawlRequest = CrawlRequest()):
    job_id = create_job("crawl", site_id)
    thread = threading.Thread(target=_run_crawl, args=(site_id, req.limit, job_id), daemon=True)
    thread.start()
    return get_job(job_id)

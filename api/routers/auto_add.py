import os
import threading
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from ..services.job_manager import create_job, update_job, get_job

router = APIRouter(prefix="/api", tags=["auto-add"])


class AutoAddRequest(BaseModel):
    url: str
    site_id: Optional[str] = None
    browser: bool = False


@router.post("/auto-add")
async def auto_add(req: AutoAddRequest):
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=400, detail="OPENAI_API_KEY not configured. Set the environment variable to use Auto-Add.")

    job_id = create_job("auto-add", req.url)
    thread = threading.Thread(target=_run_auto_add, args=(job_id, req.url, req.site_id, req.browser), daemon=True)
    thread.start()
    return get_job(job_id)


def _run_auto_add(job_id: str, url: str, site_id: str | None, browser: bool):
    try:
        from crawler.agent import AutoAddAgent
        agent = AutoAddAgent(force_browser=browser)
        result = agent.run(url, site_id=site_id)
        if result.get("success"):
            update_job(job_id, status="completed", result=f"Added site: {result.get('site_id', 'unknown')} ({result.get('items_found', 0)} items found)")
        else:
            update_job(job_id, status="failed", result=f"Failed: {result.get('reason', 'unknown')}")
    except Exception as e:
        update_job(job_id, status="failed", result=str(e)[:200])

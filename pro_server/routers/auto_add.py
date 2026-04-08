from fastapi import APIRouter, Depends
from ..auth import verify_license, log_usage
from ..schemas import AutoAddRequest, AutoAddResponse

router = APIRouter(prefix="/pro/api", tags=["auto-add"])


@router.post("/auto-add", response_model=AutoAddResponse)
async def auto_add(req: AutoAddRequest, license_info: dict = Depends(verify_license)):
    import sys
    import os

    # Import the agent from crawler module
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from crawler.agent import AutoAddAgent

    agent = AutoAddAgent(
        max_iterations=req.max_iterations or 10,
        verbose=True,
        force_browser=req.browser,
    )

    result = agent.run(req.url, site_id=req.site_id, site_name=req.site_name)

    log_usage(license_info["key"], "auto-add")

    return AutoAddResponse(
        success=result.get("success", False),
        site_id=result.get("site_id", ""),
        method=result.get("method", ""),
        reason=result.get("reason", ""),
        config=result.get("config"),
    )

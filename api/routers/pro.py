from fastapi import APIRouter, HTTPException
from ..services.pro_client import pro_client
from ..schemas import AutoAddRequest

router = APIRouter(prefix="/api/pro", tags=["pro"])


@router.get("/status")
async def pro_status():
    """Check if PRO mode is configured and valid."""
    if not pro_client.is_configured:
        return {"pro_enabled": False, "reason": "Not configured"}
    try:
        result = await pro_client.verify_key()
        return {"pro_enabled": True, "plan": result.get("plan", "basic")}
    except Exception as e:
        return {"pro_enabled": False, "reason": str(e)}


@router.get("/usage")
async def pro_usage():
    if not pro_client.is_configured:
        raise HTTPException(status_code=400, detail="PRO mode not configured")
    return await pro_client.get_usage()


@router.post("/auto-add")
async def pro_auto_add(req: AutoAddRequest):
    if not pro_client.is_configured:
        raise HTTPException(status_code=400, detail="PRO mode not configured")
    return await pro_client.auto_add(
        url=req.url, site_id=req.site_id, site_name=req.site_name, browser=req.browser,
    )


@router.post("/summarize")
async def pro_summarize(req: dict):
    if not pro_client.is_configured:
        raise HTTPException(status_code=400, detail="PRO mode not configured")
    return await pro_client.summarize(text=req.get("text", ""), title=req.get("title"))
